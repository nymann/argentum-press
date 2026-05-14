# pyright: basic
"""L6 — libcst-based edit application for the lower-gap playbook.

Two anchors are supported, matching the two patterns L5a selects between:

* ``register-handler``: append a new ``@<dispatcher>.register`` clause at the
  end of the ``KotlinLowerer`` class body. The L5b plan provides the full
  function source as ``body_python``.
* ``isinstance-branch``: splice a new ``if isinstance(stmt, ast.X): ...``
  block into a named helper, immediately before its final ``raise EmitterGap``
  statement (or at the end of the function if there's no raise). The L5b
  plan provides the full ``if`` block as ``body_python``.

If libcst can't locate the anchor (the function doesn't exist, the file
isn't well-formed Python, …) we raise :class:`AnchorNotFoundError` so the
playbook driver can abort cleanly without leaving a half-edited file.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import libcst as cst


class AnchorNotFoundError(RuntimeError):
    """libcst couldn't find the anchor described in the plan."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EditResult:
    """The outcome of applying an L6 edit. ``original`` is the file source
    before the edit (so the driver can restore on retry without shelling out
    to git)."""

    path: Path
    original: str
    new_source: str


def apply_plan(plan: dict[str, Any], lowerer_path: Path) -> EditResult:
    """Apply an L5b plan dict to ``lowerer_path`` in place.

    Reads the file, applies the libcst transform, writes the new source.
    Returns an :class:`EditResult` carrying the original source so callers
    can revert on a failed pytest. Raises :class:`AnchorNotFoundError` when
    the anchor doesn't resolve.
    """
    original = lowerer_path.read_text(encoding="utf-8")
    module = cst.parse_module(original)

    anchor = plan["anchor"]
    pattern = anchor["pattern"]
    body_python = plan["body_python"]
    new_imports = plan.get("new_imports", []) or []

    if pattern == "register-handler":
        dispatcher = anchor.get("dispatcher") or "ability"
        new_module = _insert_register_handler(module, dispatcher, body_python)
    elif pattern == "isinstance-branch":
        function = anchor.get("function")
        if not function:
            raise AnchorNotFoundError(
                "isinstance-branch plan missing anchor.function"
            )
        new_module = _insert_isinstance_branch(module, function, body_python)
    else:
        raise AnchorNotFoundError(f"unknown anchor pattern: {pattern!r}")

    if new_imports:
        new_module = _add_imports(new_module, new_imports)

    lowerer_path.write_text(new_module.code, encoding="utf-8")
    return EditResult(path=lowerer_path, original=original, new_source=new_module.code)


def revert(result: EditResult) -> None:
    """Write the saved ``original`` source back to ``result.path``."""
    result.path.write_text(result.original, encoding="utf-8")


# ---------------------------------------------------------------------------
# register-handler insertion
# ---------------------------------------------------------------------------


def _parse_function_def(source: str) -> cst.FunctionDef:
    """Parse a free-floating decorated def into a single FunctionDef node.

    ``body_python`` for register-handler plans should contain a complete
    decorated function, e.g.::

        @ability.register
        def _(self, ability: ast.X) -> str:
            raise EmitterGap(ability)

    We wrap it inside a placeholder class so libcst can parse the decorator
    correctly, then return the inner FunctionDef. The wrapper class is
    discarded — only the function survives.
    """
    dedented = textwrap.dedent(source).strip("\n")
    wrapper_src = "class _W:\n" + textwrap.indent(dedented, "    ") + "\n"
    try:
        wrapper = cst.parse_module(wrapper_src)
    except cst.ParserSyntaxError as e:
        raise AnchorNotFoundError(
            f"register-handler body_python doesn't parse as a class method: {e}"
        ) from e
    cls = next((s for s in wrapper.body if isinstance(s, cst.ClassDef)), None)
    if cls is None:
        raise AnchorNotFoundError("register-handler wrapper produced no ClassDef")
    fn = next((s for s in cls.body.body if isinstance(s, cst.FunctionDef)), None)
    if fn is None:
        raise AnchorNotFoundError("register-handler body_python contained no def")
    return fn


def _insert_register_handler(
    module: cst.Module, dispatcher: str, body_python: str
) -> cst.Module:
    """Append a new ``@dispatcher.register`` def at the tail of KotlinLowerer.

    We don't try to slot the handler next to other handlers for the same
    dispatcher — the lowerer already groups them by convention but the
    @singledispatchmethod registration is order-independent. Appending at the
    tail keeps the libcst transform trivial and merge-friendly.
    """
    new_fn = _parse_function_def(body_python)

    class _Inserter(cst.CSTTransformer):
        def __init__(self) -> None:
            super().__init__()
            self.inserted = False

        def leave_ClassDef(
            self, original: cst.ClassDef, updated: cst.ClassDef
        ) -> cst.ClassDef:
            if original.name.value != "KotlinLowerer":
                return updated
            self.inserted = True
            new_body = list(updated.body.body) + [new_fn]
            return updated.with_changes(
                body=updated.body.with_changes(body=tuple(new_body))
            )

    ins = _Inserter()
    out = module.visit(ins)
    if not ins.inserted:
        raise AnchorNotFoundError(
            "register-handler insertion failed: KotlinLowerer class not found"
        )
    return out


# ---------------------------------------------------------------------------
# isinstance-branch insertion
# ---------------------------------------------------------------------------


def _parse_if_statement(source: str) -> cst.If:
    """Parse a free-floating ``if isinstance(...): ...`` block.

    The block must include the ``if`` keyword and its body. We wrap inside
    a dummy function so libcst sees a complete statement list.
    """
    dedented = textwrap.dedent(source).strip("\n")
    wrapper_src = "def _w():\n" + textwrap.indent(dedented, "    ") + "\n"
    try:
        wrapper = cst.parse_module(wrapper_src)
    except cst.ParserSyntaxError as e:
        raise AnchorNotFoundError(
            f"isinstance-branch body_python doesn't parse: {e}"
        ) from e
    fn = next((s for s in wrapper.body if isinstance(s, cst.FunctionDef)), None)
    if fn is None:
        raise AnchorNotFoundError("isinstance-branch wrapper produced no FunctionDef")
    for stmt in fn.body.body:
        if isinstance(stmt, cst.If):
            return stmt
    raise AnchorNotFoundError("isinstance-branch body_python contained no `if` statement")


def _insert_isinstance_branch(
    module: cst.Module, function: str, body_python: str
) -> cst.Module:
    """Splice a new ``if isinstance(...)`` block into ``function``'s body.

    Inserts immediately before the final ``raise EmitterGap(...)`` line if one
    exists; otherwise appends at the end of the function body. Searching for
    the raise keeps the catch-all path last, matching the file's existing
    convention.
    """
    new_if = _parse_if_statement(body_python)

    class _Inserter(cst.CSTTransformer):
        def __init__(self) -> None:
            super().__init__()
            self.inserted = False

        def leave_FunctionDef(
            self, original: cst.FunctionDef, updated: cst.FunctionDef
        ) -> cst.FunctionDef:
            if original.name.value != function:
                return updated
            body_list = list(updated.body.body)
            insert_at = len(body_list)
            for idx, stmt in enumerate(body_list):
                if _is_emitter_gap_raise(stmt):
                    insert_at = idx
                    break
            body_list.insert(insert_at, new_if)
            self.inserted = True
            return updated.with_changes(
                body=updated.body.with_changes(body=tuple(body_list))
            )

    ins = _Inserter()
    out = module.visit(ins)
    if not ins.inserted:
        raise AnchorNotFoundError(
            f"isinstance-branch insertion failed: function {function!r} not found"
        )
    return out


def _is_emitter_gap_raise(stmt: cst.BaseStatement) -> bool:
    """True iff ``stmt`` is a top-level ``raise EmitterGap(...)`` line."""
    if not isinstance(stmt, cst.SimpleStatementLine):
        return False
    for sub in stmt.body:
        if not isinstance(sub, cst.Raise):
            continue
        exc = sub.exc
        if isinstance(exc, cst.Call):
            func = exc.func
            if isinstance(func, cst.Name) and func.value == "EmitterGap":
                return True
        if isinstance(exc, cst.Name) and exc.value == "EmitterGap":
            return True
    return False


# ---------------------------------------------------------------------------
# Import management
# ---------------------------------------------------------------------------


def _add_imports(module: cst.Module, new_imports: list[str]) -> cst.Module:
    """Append each ``import …`` / ``from … import …`` line after the
    existing imports block.

    We don't try to merge imports into existing ``from`` lines — fewer
    edge cases. Duplicate lines are silently skipped.
    """
    existing = set()
    last_import_idx = -1
    for idx, stmt in enumerate(module.body):
        if isinstance(stmt, cst.SimpleStatementLine):
            for sub in stmt.body:
                if isinstance(sub, (cst.Import, cst.ImportFrom)):
                    existing.add(module.code_for_node(sub).strip())
                    last_import_idx = idx
    new_nodes: list[cst.BaseStatement] = []
    for line in new_imports:
        stripped = line.strip()
        if not stripped or stripped in existing:
            continue
        try:
            mini = cst.parse_module(stripped + "\n")
        except cst.ParserSyntaxError as e:
            raise AnchorNotFoundError(
                f"new_imports entry {stripped!r} doesn't parse: {e}"
            ) from e
        for s in mini.body:
            if isinstance(s, cst.SimpleStatementLine):
                new_nodes.append(s)
    if not new_nodes:
        return module
    body = list(module.body)
    insert_at = last_import_idx + 1 if last_import_idx >= 0 else 0
    body[insert_at:insert_at] = new_nodes
    return module.with_changes(body=tuple(body))
