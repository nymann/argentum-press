# pyright: basic
"""Edit application across the three playbooks.

Lower-gap (L6): libcst over ``lowerer.py`` supporting two anchor patterns
(``register-handler`` / ``isinstance-branch``). See :func:`apply_plan`.

Parse-error (P5): text-level splice into ``grammar.py``'s triple-quoted
string. The grammar source is one big string literal — libcst gives no
purchase on its content, so we slice on line ranges. See
:func:`apply_grammar_alternative`.

Unmodeled-rule (U5): libcst trio — write the new AST dataclass file (or
append to an existing module), update ``parser/ast/__init__.py`` to
re-export, and append a ``CardTransformer`` method to
``parser/transformer.py``. See :func:`apply_unmodeled_rule`.

Every entry point raises :class:`AnchorNotFoundError` on a structural
mismatch so the playbook driver can abort cleanly without leaving a
half-edited file.
"""
from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
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


def _collect_singledispatch_names(module: cst.Module) -> set[str]:
    """Names of methods in KotlinLowerer decorated with @singledispatchmethod.

    The L5b/L9 LLM has hallucinated dispatcher names before (`value`,
    `value_expression`) that look plausible from the AST class name but
    don't exist in the class — the resulting `@<name>.register` decorator
    parses fine but `NameError`s at import. Validating here makes that
    surface as a clean L6/L10 abort instead of a poisoned pytest run.
    """
    names: set[str] = set()
    for stmt in module.body:
        if not isinstance(stmt, cst.ClassDef) or stmt.name.value != "KotlinLowerer":
            continue
        for member in stmt.body.body:
            if not isinstance(member, cst.FunctionDef):
                continue
            for dec in member.decorators:
                d = dec.decorator
                if isinstance(d, cst.Name) and d.value == "singledispatchmethod":
                    names.add(member.name.value)
                    break
    return names


def _insert_register_handler(
    module: cst.Module, dispatcher: str, body_python: str
) -> cst.Module:
    """Append a new ``@dispatcher.register`` def at the tail of KotlinLowerer.

    We don't try to slot the handler next to other handlers for the same
    dispatcher — the lowerer already groups them by convention but the
    @singledispatchmethod registration is order-independent. Appending at the
    tail keeps the libcst transform trivial and merge-friendly.
    """
    valid = _collect_singledispatch_names(module)
    if valid and dispatcher not in valid:
        raise AnchorNotFoundError(
            f"register-handler dispatcher {dispatcher!r} is not a "
            f"@singledispatchmethod on KotlinLowerer; "
            f"valid: {sorted(valid)}"
        )

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


# ---------------------------------------------------------------------------
# Parse-error (P5) — grammar alternative splice
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GrammarEditResult:
    """Snapshot for a grammar.py edit so the driver can revert on red pytest."""

    path: Path
    original: str
    new_source: str
    inserted_line: int
    inserted_text: str


def normalize_branch_body(text: str) -> str:
    """Canonicalize a Lark branch for equality comparison.

    Strips a leading ``|``, drops a trailing ``-> labelname`` (labels don't
    change the production, just the Tree name Lark emits), and collapses
    runs of whitespace. Used by ``alternative_already_exists`` to detect
    when an LLM-proposed branch duplicates one already in the parent rule.
    """
    s = text.strip()
    if s.startswith("|"):
        s = s[1:].lstrip()
    if "->" in s:
        s = s.split("->", 1)[0].rstrip()
    return " ".join(s.split())


def alternative_already_exists(parent_rule_source: str, alternative_text: str) -> bool:
    """True when ``alternative_text``'s body already appears in the parent rule.

    The parent rule source is the full block emitted by
    ``context.dump_rule_definitions`` (one ``parentname: body0`` line, then
    one ``| bodyN`` per continuation). We strip the optional ``<line>: ``
    prefix dump format puts on each line, normalize each branch body, and
    compare against the normalized incoming alternative.
    """
    needle = normalize_branch_body(alternative_text)
    if not needle:
        return False
    for raw_line in parent_rule_source.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip "<N>: " prefix added by dump_rule_definitions.
        import re as _re
        m = _re.match(r"^\d+:\s+(.*)$", line)
        if m:
            line = m.group(1).strip()
        if "|" in line:
            body = line[line.index("|") + 1:]
        elif ":" in line:
            body = line.split(":", 1)[1]
        else:
            continue
        if normalize_branch_body(body) == needle:
            return True
    return False


def apply_grammar_alternative(
    *,
    grammar_path: Path,
    parent_rule: str,
    alternative_text: str,
    label: str | None = None,
) -> GrammarEditResult:
    """Splice ``alternative_text`` as a new ``| …`` branch of ``parent_rule``.

    The grammar source is one big triple-quoted string whose lines all start
    with 8 spaces. We locate the rule by name, find the last continuation
    line of its body (where another alternative would attach), and insert
    a new line of the shape ``        | <alt>`` (optionally trailed with
    ``-> <label>``). The edit is validated by re-importing the grammar
    string via ``lark.Lark(...)`` — if Lark rejects the new grammar we raise
    and let the driver retry rather than commit a broken file.
    """
    original = grammar_path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    import re
    rule_re = re.compile(rf"^(\s+)!?{re.escape(parent_rule)}\s*:")
    next_rule = re.compile(r"^\s+!?[a-z][a-z0-9_]*\s*:")
    start_idx: int | None = None
    indent = ""
    for i, line in enumerate(lines):
        m = rule_re.match(line)
        if m:
            start_idx = i
            indent = m.group(1)
            break
    if start_idx is None:
        raise AnchorNotFoundError(
            f"grammar-alternative insertion failed: rule {parent_rule!r} not found in {grammar_path}"
        )

    # Walk past continuation lines (alternatives or wrapped text) until we
    # hit either the next rule declaration or a blank line.
    end_idx = start_idx
    for j in range(start_idx + 1, len(lines)):
        if next_rule.match(lines[j]):
            break
        if not lines[j].strip():
            break
        end_idx = j

    alt_body = alternative_text.strip()
    if alt_body.startswith("|"):
        alt_body = alt_body[1:].lstrip()
    branch_line = f"{indent}| {alt_body}"
    if label and "->" not in alt_body:
        branch_line += f" -> {label}"
    branch_line += "\n"

    # Insert right after the rule's last existing body line.
    new_lines = lines[: end_idx + 1] + [branch_line] + lines[end_idx + 1 :]
    new_source = "".join(new_lines)

    # Validation: re-execute getGrammar() and compile via Lark. If the new
    # grammar doesn't parse as a Lark grammar at all, revert in memory and
    # raise.
    grammar_path.write_text(new_source, encoding="utf-8")
    try:
        _validate_grammar_compiles(grammar_path)
    except Exception as e:
        grammar_path.write_text(original, encoding="utf-8")
        raise AnchorNotFoundError(
            f"grammar-alternative insertion produced an invalid grammar: {e}"
        ) from e

    return GrammarEditResult(
        path=grammar_path,
        original=original,
        new_source=new_source,
        inserted_line=end_idx + 2,  # 1-based
        inserted_text=branch_line,
    )


def revert_grammar(result: GrammarEditResult) -> None:
    """Write the saved ``original`` source back to ``result.path``."""
    result.path.write_text(result.original, encoding="utf-8")


def _validate_grammar_compiles(grammar_path: Path) -> None:
    """Re-exec the grammar module and compile its string with Lark.

    Imports the module via importlib so we pick up the freshly-written file
    (rather than the cached ``sys.modules`` entry). Lark instantiation is
    the only validation we run — semantic correctness of the new branch is
    left for pytest.
    """
    import importlib.util
    import sys
    # Drop any cached version so the freshly-written file is re-read.
    for modname in list(sys.modules):
        if modname.endswith(".grammar.grammar") or modname.endswith(".grammar"):
            del sys.modules[modname]
    spec = importlib.util.spec_from_file_location("_pb_grammar_check", grammar_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {grammar_path} for validation")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    grammar_str = mod.getGrammar()
    import lark
    lark.Lark(grammar_str, start="cardtext", parser="earley")


# ---------------------------------------------------------------------------
# Unmodeled-rule (U5) — AST dataclass + transformer method
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnmodeledRuleEditResult:
    """Snapshot for the U5 multi-file edit so the driver can revert all of it.

    ``originals`` maps path -> original source text. The driver writes each
    file back from this map on revert; nothing else is needed because U5
    only touches three known files (AST module, AST __init__, transformer).
    """

    originals: dict[Path, str]
    paths: tuple[Path, ...]


def apply_unmodeled_rule(
    *,
    ast_dir: Path,
    ast_init_path: Path,
    transformer_path: Path,
    parent_module: str,
    classname: str,
    ast_class_source: str,
    transformer_method_source: str,
    extra_imports: list[str] | None = None,
) -> UnmodeledRuleEditResult:
    """Apply U3 + U4 + U5: add dataclass, export it, add transformer method.

    ``ast_class_source`` is the full ``@dataclass(...)\\nclass X(...):\\n    …``
    block as written by the LLM. ``transformer_method_source`` is the full
    ``def <rule>(self, items): …`` body, indented at class scope.

    All three edits run inside a try; if any of them raises we revert every
    file we've already modified and re-raise. That avoids leaving the repo
    in a half-written state when the transformer method is malformed.
    """
    extra_imports = list(extra_imports or [])
    module_path = ast_dir / f"{parent_module}.py"
    if not module_path.is_file():
        raise AnchorNotFoundError(
            f"unmodeled-rule: parent module {parent_module!r} not found at {module_path}"
        )

    originals: dict[Path, str] = {
        module_path: module_path.read_text(encoding="utf-8"),
        ast_init_path: ast_init_path.read_text(encoding="utf-8"),
        transformer_path: transformer_path.read_text(encoding="utf-8"),
    }
    paths = (module_path, ast_init_path, transformer_path)

    try:
        # 1. Append the dataclass to parser/ast/<parent_module>.py.
        new_module_src = _append_ast_class(originals[module_path], ast_class_source)
        module_path.write_text(new_module_src, encoding="utf-8")

        # 2. Add the export to parser/ast/__init__.py: both the `from
        # .<module> import (..., X)` block and the __all__ list.
        new_init_src = _add_ast_export(
            originals[ast_init_path], parent_module, classname
        )
        ast_init_path.write_text(new_init_src, encoding="utf-8")

        # 3. Append the method onto the CardTransformer class in
        # parser/transformer.py, plus add the import.
        new_transformer_src = _append_transformer_method(
            originals[transformer_path],
            classname,
            transformer_method_source,
            extra_imports,
        )
        transformer_path.write_text(new_transformer_src, encoding="utf-8")
    except Exception:
        # Roll back every write so the repo is unchanged.
        for p, original in originals.items():
            p.write_text(original, encoding="utf-8")
        raise

    return UnmodeledRuleEditResult(originals=originals, paths=paths)


def revert_unmodeled_rule(result: UnmodeledRuleEditResult) -> None:
    for p, original in result.originals.items():
        p.write_text(original, encoding="utf-8")


def _append_ast_class(module_src: str, class_source: str) -> str:
    """Append ``class_source`` to ``module_src`` and re-validate as Python.

    We don't try to slot the class in alphabetical order; appending at the
    tail keeps the libcst transform trivial and matches the human pattern
    (each new dataclass lands at the file's end). The result is libcst-
    parsed once as a sanity check so a malformed dataclass surfaces here
    rather than at import time.
    """
    new_src = module_src.rstrip("\n") + "\n\n\n" + class_source.strip("\n") + "\n"
    try:
        cst.parse_module(new_src)
    except cst.ParserSyntaxError as e:
        raise AnchorNotFoundError(f"new AST class doesn't parse: {e}") from e
    return new_src


def _add_ast_export(init_src: str, parent_module: str, classname: str) -> str:
    """Insert ``classname`` into the ``from .<parent_module> import (…)``
    block and the alphabetic ``__all__`` list inside ``init_src``.

    Both inserts use libcst, but they're shaped enough (only Name children)
    that we can simply rewrite the relevant collections. If the parent
    module isn't already imported we add a fresh ``from`` block at the end
    of the import section.
    """
    module = cst.parse_module(init_src)
    new_module = _InsertExport(parent_module, classname).run(module)
    return new_module.code


class _InsertExport:
    """libcst pass that injects ``classname`` into both surfaces.

    Two collections to update inside ``__init__.py``:

    * ``from argentum_press.parser.ast.<parent> import (A, B, X, …)`` — sorted
      block, one name per line.
    * ``__all__ = ["A", "B", "X", …]`` — alphabetic list of bare strings.

    We treat both as sorted-by-name and re-emit. Inserting a name that
    already exists is a no-op.
    """

    def __init__(self, parent_module: str, classname: str) -> None:
        self.parent_module = parent_module
        self.classname = classname

    def run(self, module: cst.Module) -> cst.Module:
        return module.visit(_InsertExportTransformer(self.parent_module, self.classname))


class _InsertExportTransformer(cst.CSTTransformer):
    """Implements the two collection updates for :class:`_InsertExport`."""

    def __init__(self, parent_module: str, classname: str) -> None:
        super().__init__()
        self.parent_module = parent_module
        self.classname = classname
        self.import_done = False
        self.all_done = False

    def leave_ImportFrom(
        self, original: cst.ImportFrom, updated: cst.ImportFrom
    ) -> cst.ImportFrom:
        # Match `from argentum_press.parser.ast.<parent_module> import (...)`.
        mod = updated.module
        if not isinstance(mod, cst.Attribute):
            return updated
        # Walk attribute chain to get the dotted path.
        parts: list[str] = []
        cur: cst.BaseExpression = mod
        while isinstance(cur, cst.Attribute):
            parts.insert(0, cur.attr.value)
            cur = cur.value
        if isinstance(cur, cst.Name):
            parts.insert(0, cur.value)
        dotted = ".".join(parts)
        if dotted != f"argentum_press.parser.ast.{self.parent_module}":
            return updated
        names = updated.names
        if isinstance(names, cst.ImportStar):
            return updated
        existing = [alias.name.value for alias in names if isinstance(alias.name, cst.Name)]
        if self.classname in existing:
            self.import_done = True
            return updated
        new_names = sorted(existing + [self.classname])
        aliases = tuple(
            cst.ImportAlias(name=cst.Name(value=n)) for n in new_names
        )
        self.import_done = True
        return updated.with_changes(names=aliases)

    def leave_Assign(self, original: cst.Assign, updated: cst.Assign) -> cst.Assign:
        # Match the __all__ = [...] assignment at module top level.
        targets = updated.targets
        if len(targets) != 1:
            return updated
        target = targets[0].target
        if not isinstance(target, cst.Name) or target.value != "__all__":
            return updated
        if not isinstance(updated.value, cst.List):
            return updated
        existing = []
        for elt in updated.value.elements:
            if isinstance(elt.value, cst.SimpleString):
                existing.append(elt.value.evaluated_value)
        if self.classname in existing:
            self.all_done = True
            return updated
        names = sorted(set(existing + [self.classname]))
        new_elements = tuple(
            cst.Element(
                value=cst.SimpleString(value=f'"{n}"'),
                comma=cst.Comma(whitespace_after=cst.ParenthesizedWhitespace(
                    first_line=cst.TrailingWhitespace(whitespace=cst.SimpleWhitespace("")),
                    empty_lines=[],
                    indent=True,
                    last_line=cst.SimpleWhitespace("    "),
                )),
            )
            for n in names
        )
        self.all_done = True
        return updated.with_changes(value=updated.value.with_changes(elements=new_elements))


def _append_transformer_method(
    transformer_src: str,
    classname: str,
    method_source: str,
    extra_imports: list[str],
) -> str:
    """Append ``method_source`` onto the ``CardTransformer`` class.

    ``method_source`` is a full ``def <name>(self, items): …`` block at
    class-scope indentation (4 spaces). The libcst transformer parses it
    once, slots it as the final method on the class, and updates the
    ``from argentum_press.parser.ast import (…)`` block to include the
    new dataclass name (passed via ``extra_imports``).
    """
    module = cst.parse_module(transformer_src)
    method_def = _parse_method_def(method_source)

    class _Inserter(cst.CSTTransformer):
        def __init__(self) -> None:
            super().__init__()
            self.inserted = False

        def leave_ClassDef(
            self, original: cst.ClassDef, updated: cst.ClassDef
        ) -> cst.ClassDef:
            if original.name.value != "CardTransformer":
                return updated
            self.inserted = True
            body_list = list(updated.body.body)
            body_list.append(method_def)
            return updated.with_changes(
                body=updated.body.with_changes(body=tuple(body_list))
            )

    ins = _Inserter()
    out = module.visit(ins)
    if not ins.inserted:
        raise AnchorNotFoundError(
            "transformer-method insertion failed: CardTransformer class not found"
        )

    # Imports: each entry is just the bare AST class name to add to the
    # `from argentum_press.parser.ast import (…)` block. Plus the
    # transformer adds the new dataclass name automatically.
    full_imports = list(dict.fromkeys([classname, *extra_imports]))
    if full_imports:
        out = _add_ast_imports_to_transformer(out, full_imports)
    return out.code


def _parse_method_def(source: str) -> cst.FunctionDef:
    """Parse a free-floating ``def ...`` block into a FunctionDef.

    Wraps inside a placeholder class so the parser sees a complete method
    context. The wrapper class is discarded.
    """
    dedented = textwrap.dedent(source).strip("\n")
    wrapper_src = "class _W:\n" + textwrap.indent(dedented, "    ") + "\n"
    try:
        wrapper = cst.parse_module(wrapper_src)
    except cst.ParserSyntaxError as e:
        raise AnchorNotFoundError(
            f"transformer-method source doesn't parse: {e}"
        ) from e
    cls = next((s for s in wrapper.body if isinstance(s, cst.ClassDef)), None)
    if cls is None:
        raise AnchorNotFoundError("transformer-method wrapper produced no ClassDef")
    fn = next((s for s in cls.body.body if isinstance(s, cst.FunctionDef)), None)
    if fn is None:
        raise AnchorNotFoundError("transformer-method source contained no def")
    return fn


def _add_ast_imports_to_transformer(
    module: cst.Module, names_to_add: list[str]
) -> cst.Module:
    """Add ``names_to_add`` to the ``from argentum_press.parser.ast import (…)``
    block in ``module``.

    Idempotent — names already imported are silently skipped. We sort the
    final list alphabetically because that's the existing file convention.
    """
    class _Adder(cst.CSTTransformer):
        def __init__(self) -> None:
            super().__init__()
            self.done = False

        def leave_ImportFrom(
            self, original: cst.ImportFrom, updated: cst.ImportFrom
        ) -> cst.ImportFrom:
            mod = updated.module
            parts: list[str] = []
            cur: cst.BaseExpression | None = mod
            while isinstance(cur, cst.Attribute):
                parts.insert(0, cur.attr.value)
                cur = cur.value
            if isinstance(cur, cst.Name):
                parts.insert(0, cur.value)
            if ".".join(parts) != "argentum_press.parser.ast":
                return updated
            names = updated.names
            if isinstance(names, cst.ImportStar):
                return updated
            existing = [a.name.value for a in names if isinstance(a.name, cst.Name)]
            merged = sorted(set(existing) | set(names_to_add))
            aliases = tuple(cst.ImportAlias(name=cst.Name(value=n)) for n in merged)
            self.done = True
            return updated.with_changes(names=aliases)

    return module.visit(_Adder())


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
