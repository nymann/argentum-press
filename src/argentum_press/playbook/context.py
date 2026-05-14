# pyright: basic
"""Deterministic context gathering for the three playbooks.

Lower-gap (L0/L1/L2): :class:`AstClassInfo`, :class:`LowererExemplars`,
:func:`engine_dsl_hints`, :func:`gather`.

Parse-error (P0/P1/P2): :func:`grammar_rule_index`,
:func:`grammar_rule_literals`, :func:`rank_candidate_parent_rules`,
:func:`dump_rule_definitions`.

Unmodeled-rule (U0/U1/U2): :func:`list_ast_modules`,
:func:`recent_transformer_exemplars`.

All run every iteration at zero LLM cost. They read files from the repo via
libcst / text scanning and shell out to git for recent commit diffs.
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import libcst as cst


# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    # The playbook module lives at src/argentum_press/playbook/context.py.
    # Two parents up from src/ → repo root.
    return Path(__file__).resolve().parents[3]


REPO = _repo_root()
AST_DIR = REPO / "src/argentum_press/parser/ast"
LOWERER = REPO / "src/argentum_press/lowerer.py"
GRAMMAR = REPO / "src/argentum_press/parser/grammar/grammar.py"
TRANSFORMER = REPO / "src/argentum_press/parser/transformer.py"
AST_INIT = AST_DIR / "__init__.py"


# ---------------------------------------------------------------------------
# L0 — extract AST class def
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AstClassInfo:
    """The result of L0: the AST class's source span and field shape."""

    path: Path
    classname: str
    source: str  # The full ``class X(...):`` definition as written.
    fields: tuple[tuple[str, str], ...]  # (name, annotation-as-source) pairs.
    parent_module: str  # e.g. "statements", "expressions", "abilities".


class _ClassFinder(cst.CSTVisitor):
    """Locate a top-level ClassDef by name and snapshot its node."""

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.found: cst.ClassDef | None = None

    def visit_ClassDef(self, node: cst.ClassDef) -> bool:
        if node.name.value == self.name:
            self.found = node
            return False
        return True


def _class_source(module: cst.Module, node: cst.ClassDef) -> str:
    """Return the verbatim source for ``node`` from within ``module``."""
    return module.code_for_node(node)


def _class_fields(module: cst.Module, node: cst.ClassDef) -> tuple[tuple[str, str], ...]:
    """Walk the class body and collect annotated assignments as fields.

    Fields without an annotation are skipped — these dataclasses use
    ``@dataclass(frozen=True, slots=True)`` and every real field carries one.
    """
    out: list[tuple[str, str]] = []
    for stmt in node.body.body:
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue
        for sub in stmt.body:
            if not isinstance(sub, cst.AnnAssign):
                continue
            if not isinstance(sub.target, cst.Name):
                continue
            anno = module.code_for_node(sub.annotation.annotation)
            out.append((sub.target.value, anno.strip()))
    return tuple(out)


def extract_ast_class(label: str) -> AstClassInfo | None:
    """L0 — locate the AST class named in ``label`` and return its shape.

    ``label`` is the qualified class name the lowerer emits, e.g.
    ``argentum_press.parser.ast.statements.AtStatement``. We split off the
    bare class name and scan ``parser/ast/*.py`` for the first matching
    ``class X(...):`` definition. Returns None if the class can't be found.
    """
    bare = label.split(".")[-1]
    for path in sorted(AST_DIR.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        try:
            module = cst.parse_module(text)
        except cst.ParserSyntaxError:
            continue
        finder = _ClassFinder(bare)
        module.visit(finder)
        if finder.found is None:
            continue
        return AstClassInfo(
            path=path,
            classname=bare,
            source=_class_source(module, finder.found),
            fields=_class_fields(module, finder.found),
            parent_module=path.stem,
        )
    return None


# ---------------------------------------------------------------------------
# L1 — lowerer exemplars (register handlers + isinstance branches)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegisterHandler:
    """One ``@<dispatcher>.register`` clause from lowerer.py."""

    dispatcher: str  # "ability" or "effect"
    ast_class: str  # The single parameter's type annotation, e.g. "ast.AtStatement".
    body: str  # The function body as source, with original indentation.
    line: int  # 1-based line number of the ``def`` keyword.


@dataclass(frozen=True, slots=True)
class IsinstanceBranch:
    """One ``isinstance(stmt, ast.X)`` branch inside a helper like
    ``_effects_from_statement``. Captured verbatim from the function body."""

    function: str  # The enclosing function/method name.
    ast_class: str  # The full ``ast.X`` reference as source.
    branch_source: str  # The whole ``if isinstance(...): ...`` block.
    line: int  # 1-based line of the ``if`` statement.


@dataclass(frozen=True, slots=True)
class LowererExemplars:
    """L1 result: every handler the LLM needs to see in one bundle."""

    register_handlers: tuple[RegisterHandler, ...]
    isinstance_branches: tuple[IsinstanceBranch, ...]


def _extract_register_handlers(module: cst.Module) -> list[RegisterHandler]:
    """Walk the lowerer module top-level for ``@<dispatcher>.register`` defs.

    The pattern we match: a FunctionDef whose first decorator is an Attribute
    access ``<dispatcher>.register``. The handler's single non-``self``
    parameter carries the AST class as its annotation. We cap body length at
    30 lines so the prompt stays small even when one handler is unusually
    verbose.
    """
    out: list[RegisterHandler] = []
    for cls in module.body:
        if not isinstance(cls, cst.ClassDef):
            continue
        # Walk into the class body to find @dispatcher.register handlers.
        for stmt in cls.body.body:
            if not isinstance(stmt, cst.FunctionDef):
                continue
            for dec in stmt.decorators:
                d = dec.decorator
                if not isinstance(d, cst.Attribute):
                    continue
                if not isinstance(d.value, cst.Name):
                    continue
                if d.attr.value != "register":
                    continue
                dispatcher = d.value.value
                # Find the first non-self parameter annotation.
                params = stmt.params.params
                if len(params) < 2:
                    continue
                arg = params[1]
                if arg.annotation is None:
                    continue
                ast_class = module.code_for_node(arg.annotation.annotation).strip()
                body_src = module.code_for_node(stmt.body)
                body_lines = body_src.splitlines()
                if len(body_lines) > 30:
                    body_src = "\n".join(body_lines[:30]) + "\n        # ... (truncated)"
                # Approximate line number: count lines preceding this node.
                pos = _approx_line(module, stmt)
                out.append(
                    RegisterHandler(
                        dispatcher=dispatcher,
                        ast_class=ast_class,
                        body=body_src,
                        line=pos,
                    )
                )
                break
    return out


def _approx_line(module: cst.Module, target: cst.CSTNode) -> int:
    """Approximate 1-based line number of ``target`` within ``module``.

    libcst exposes positions via metadata; rather than pull in the
    PositionProvider for every call, we re-emit source up to the node and
    count newlines. Cheap and good enough for prompt hints.
    """
    code = module.code
    fragment = module.code_for_node(target).split("\n", 1)[0]
    idx = code.find(fragment)
    if idx < 0:
        return 0
    return code.count("\n", 0, idx) + 1


class _IsinstanceCollector(cst.CSTVisitor):
    """Inside each function body, record every top-level ``if isinstance(...)``.

    "Top-level" here means a direct child of the function body's
    IndentedBlock — we don't recurse into nested if/else, because the
    pattern we're modelling (``_effects_from_statement``-style dispatch) is
    one flat if-chain.
    """

    def __init__(self, module: cst.Module) -> None:
        super().__init__()
        self.module = module
        self.current_fn: list[str] = []
        self.found: list[IsinstanceBranch] = []

    def visit_FunctionDef(self, node: cst.FunctionDef) -> bool:
        self.current_fn.append(node.name.value)
        return True

    def leave_FunctionDef(self, original_node: cst.FunctionDef) -> None:
        self.current_fn.pop()

    def visit_If(self, node: cst.If) -> bool:
        if not self.current_fn:
            return True
        fn = self.current_fn[-1]
        test = node.test
        if not isinstance(test, cst.Call):
            return True
        if not isinstance(test.func, cst.Name) or test.func.value != "isinstance":
            return True
        if len(test.args) < 2:
            return True
        cls_arg = test.args[1].value
        # ast.X or (ast.X, ast.Y) — capture either as source.
        ast_class = self.module.code_for_node(cls_arg).strip()
        branch_src = self.module.code_for_node(node)
        self.found.append(
            IsinstanceBranch(
                function=fn,
                ast_class=ast_class,
                branch_source=branch_src,
                line=_approx_line(self.module, node),
            )
        )
        return True


def collect_lowerer_exemplars() -> LowererExemplars:
    """L1 — collect every register handler and isinstance branch in lowerer.py."""
    text = LOWERER.read_text(encoding="utf-8")
    module = cst.parse_module(text)
    register = _extract_register_handlers(module)
    coll = _IsinstanceCollector(module)
    module.visit(coll)
    return LowererExemplars(
        register_handlers=tuple(register),
        isinstance_branches=tuple(coll.found),
    )


# ---------------------------------------------------------------------------
# L2 — engine DSL hints (delegates to scripts/fix_parser_gaps.py:engine_dsl_hints)
# ---------------------------------------------------------------------------


def engine_dsl_hints(label: str, project_dir: Path) -> str | None:
    """L2 — ripgrep the engine repo for the bare AST class name.

    Strips common suffixes (Expression / Statement / Ability) to widen the
    hit window — argentum-engine names map onto MTG concepts, not onto AST
    class names. Returns up to 50 lines of context. ``None`` when ripgrep
    isn't available and grep also doesn't yield output.
    """
    keyword = label.split(".")[-1]
    for suffix in ("Expression", "Statement", "Ability"):
        if keyword.endswith(suffix):
            keyword = keyword[: -len(suffix)]
            break
    if not keyword or not project_dir.is_dir():
        return None
    rg = ["rg", "-n", "--no-heading", "-tkotlin", "-B", "1", "-A", "3", keyword, str(project_dir)]
    grep = ["grep", "-rn", "--include=*.kt", "-B", "1", "-A", "3", keyword, str(project_dir)]
    for cmd in (rg, grep):
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except FileNotFoundError:
            continue
        if r.stdout:
            lines = r.stdout.splitlines()[:50]
            return f"grep '{keyword}' in {project_dir} (top 50 lines):\n\n" + "\n".join(lines)
        if r.returncode in (0, 1):
            return f"(no occurrences of '{keyword}' in {project_dir}/**/*.kt)"
    return None


# ---------------------------------------------------------------------------
# Combined context bundle
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LowerContext:
    """All deterministic inputs to the lower-gap LLM calls."""

    label: str
    ast_class: AstClassInfo
    exemplars: LowererExemplars
    engine_hints: str | None
    project_dir: Path
    card_name: str = ""
    oracle_text: str = ""
    ast_text: str | None = None


def gather(
    *,
    label: str,
    project_dir: Path,
    card_name: str = "",
    oracle_text: str = "",
    ast_text: str | None = None,
) -> LowerContext:
    """Run L0 + L1 + L2 and bundle the results.

    Raises :class:`ValueError` when L0 can't locate the AST class — that's a
    structural mismatch (label refers to something not under parser/ast/) and
    the caller should bail rather than feed garbage to the LLM.
    """
    info = extract_ast_class(label)
    if info is None:
        raise ValueError(f"playbook: AST class for label {label!r} not found under {AST_DIR}")
    exemplars = collect_lowerer_exemplars()
    hints = engine_dsl_hints(label, project_dir)
    return LowerContext(
        label=label,
        ast_class=info,
        exemplars=exemplars,
        engine_hints=hints,
        project_dir=project_dir,
        card_name=card_name,
        oracle_text=oracle_text,
        ast_text=ast_text,
    )


# Exemplar filtering helper used by the LLM-prompt builder. Kept here next to
# the gatherer so changes to LowererExemplars stay co-located.


def filter_exemplars_for_pattern(
    exemplars: LowererExemplars, pattern: str
) -> Iterable[str]:
    """Yield exemplar source blocks relevant to ``pattern``.

    ``pattern`` is one of ``"register-handler"`` or ``"isinstance-branch"``.
    For register-handler we yield each ``@dispatcher.register`` body; for
    isinstance-branch we yield each captured branch. The caller joins these
    with blank lines.
    """
    if pattern == "register-handler":
        for h in exemplars.register_handlers:
            yield f"@{h.dispatcher}.register  # ast={h.ast_class}\n{h.body}"
        return
    if pattern == "isinstance-branch":
        for b in exemplars.isinstance_branches:
            yield f"# in {b.function}\n{b.branch_source}"
        return
    raise ValueError(f"unknown exemplar pattern: {pattern!r}")


# ---------------------------------------------------------------------------
# Parse-error context (P1, P2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GrammarRule:
    """One rule definition extracted from the grammar's triple-quoted string."""

    name: str
    start_line: int  # 1-based, line carrying the rule declaration
    end_line: int    # 1-based, last line of the rule body (inclusive)
    source: str      # the full block text, joined with newlines
    literals: tuple[str, ...]  # quoted terminals appearing in the rule body


_RULE_DECL_RE = re.compile(r"^\s+!?([a-z][a-z0-9_]*)\s*:")
# Continuation lines start with leading whitespace + "|" (alternative branch).
# Anything else with whitespace + identifier-colon is a fresh rule declaration.
_LITERAL_RE = re.compile(r'"([^"\\]+)"')


def _parse_grammar_rules(text: str) -> list[GrammarRule]:
    """Walk the grammar source and return every rule definition.

    Each rule starts at the first match of ``^\\s+!?<name>\\s*:`` and runs
    until the next rule declaration or a blank line. Continuation lines
    (alternatives ``| …``) are absorbed into the same rule. Literals are
    pulled out as a denormalised tuple per rule so the ranker can intersect
    against an oracle-text vocabulary in O(1) per rule.
    """
    lines = text.splitlines()
    rules: dict[str, GrammarRule] = {}
    i = 0
    while i < len(lines):
        m = _RULE_DECL_RE.match(lines[i])
        if not m:
            i += 1
            continue
        name = m.group(1)
        start = i
        end = i
        # Collect continuation lines.
        for j in range(i + 1, len(lines)):
            nxt = lines[j]
            if _RULE_DECL_RE.match(nxt):
                break
            if not nxt.strip():
                break
            end = j
        block = "\n".join(lines[start:end + 1])
        literals = tuple(m.group(1).lower() for m in _LITERAL_RE.finditer(block))
        # Re-declarations win on first-match for the index but we record the
        # first occurrence's literals; later branches typically share the
        # head literals so the ranker isn't sensitive to this.
        rules.setdefault(
            name,
            GrammarRule(
                name=name,
                start_line=start + 1,
                end_line=end + 1,
                source=block,
                literals=literals,
            ),
        )
        i = end + 1
    return list(rules.values())


def grammar_rule_index() -> list[GrammarRule]:
    """Parse ``grammar.py`` once and return every rule definition.

    Output ordering follows file order so callers that need a deterministic
    iteration sequence (the ranker) don't need a secondary sort. Cheap on
    cold start (<100ms) since the grammar fits in memory and we just regex
    the string.
    """
    return _parse_grammar_rules(GRAMMAR.read_text(encoding="utf-8"))


@dataclass(frozen=True, slots=True)
class RankedRule:
    """One scored candidate parent rule for the parse-error LLM step."""

    rule: GrammarRule
    score: int
    overlap: tuple[str, ...]


_WORD_RE = re.compile(r"[a-z][a-z']*")


def _tokenize_oracle(text: str) -> set[str]:
    """Lowercase-word set from the failing oracle/context text."""
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) > 1}


def rank_candidate_parent_rules(
    failing_text: str, rules: list[GrammarRule] | None = None, top_n: int = 3
) -> list[RankedRule]:
    """Score grammar rules by literal-token overlap with ``failing_text``.

    Tokenizes the failing oracle / preprocessed context, intersects with each
    rule's literal terminals, scores by the size of the intersection. Returns
    the top ``top_n`` rules by score (ties broken by rule name for stability).
    Rules with score 0 are excluded — they have no overlap and can't justify
    a new alternative.
    """
    rules = rules if rules is not None else grammar_rule_index()
    vocab = _tokenize_oracle(failing_text)
    ranked: list[RankedRule] = []
    for r in rules:
        overlap = tuple(sorted(set(r.literals) & vocab))
        if not overlap:
            continue
        ranked.append(RankedRule(rule=r, score=len(overlap), overlap=overlap))
    ranked.sort(key=lambda x: (-x.score, x.rule.name))
    return ranked[:top_n]


def dump_rule_definitions(rules: Iterable[GrammarRule]) -> str:
    """Format a sequence of rules as ``<line>: <source>`` blocks.

    Mirrors the orchestrator's ``grammar_rule_block`` shape (1-based line
    prefix per line) so the LLM sees the same anchor format as the freeform
    prompt path. Multi-rule output is separated by blank lines.
    """
    parts: list[str] = []
    for r in rules:
        body_lines = r.source.splitlines()
        numbered = "\n".join(
            f"{r.start_line + i}: {line.rstrip()}" for i, line in enumerate(body_lines)
        )
        parts.append(numbered)
    return "\n\n".join(parts)


@dataclass(frozen=True, slots=True)
class ParseErrorContext:
    """All deterministic inputs to the parse-error LLM calls."""

    label: str
    failing_text: str           # preprocessed text + raw oracle text concat
    pe_block: str | None         # the orchestrator's pre-formatted summary
    rules: list[GrammarRule]
    candidates: list[RankedRule]
    candidates_dump: str
    project_dir: Path
    card_name: str = ""
    oracle_text: str = ""


def gather_parse_error(
    *,
    label: str,
    project_dir: Path,
    oracle_text: str = "",
    pe_block: str | None = None,
    card_name: str = "",
) -> ParseErrorContext:
    """Run P0 + P1 + P2 and bundle the results.

    The orchestrator already formats ``pe_block`` (the
    ``ParseErrorDetails``-derived prompt body). We feed both the raw oracle
    text and the pe_block into the tokenizer so the ranker matches against
    everything Lark saw at the failure point, not just the card text.
    """
    rules = grammar_rule_index()
    failing_text = " ".join(filter(None, [oracle_text, pe_block or ""]))
    candidates = rank_candidate_parent_rules(failing_text, rules=rules, top_n=3)
    dump = dump_rule_definitions(rc.rule for rc in candidates)
    return ParseErrorContext(
        label=label,
        failing_text=failing_text,
        pe_block=pe_block,
        rules=rules,
        candidates=candidates,
        candidates_dump=dump,
        project_dir=project_dir,
        card_name=card_name,
        oracle_text=oracle_text,
    )


# ---------------------------------------------------------------------------
# Unmodeled-rule context (U0, U1, U2)
# ---------------------------------------------------------------------------


def grammar_rule_block(rule_name: str) -> GrammarRule | None:
    """Locate one rule by name (U0)."""
    for r in grammar_rule_index():
        if r.name == rule_name:
            return r
    return None


@dataclass(frozen=True, slots=True)
class AstModuleSummary:
    """One ``parser/ast/<module>.py`` slice: the class names it defines."""

    module: str  # filename stem, e.g. "statements"
    path: Path
    class_names: tuple[str, ...]


def list_ast_modules() -> list[AstModuleSummary]:
    """U1 — libcst over ``parser/ast/*.py`` returning each module's class list.

    The LLM needs to pick which file to add a new dataclass to; the per-module
    class list is the smallest representation that still discloses the
    semantic split (Ability subclasses in abilities.py, Statement subclasses
    in statements.py, etc.).
    """
    out: list[AstModuleSummary] = []
    for path in sorted(AST_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        try:
            module = cst.parse_module(text)
        except cst.ParserSyntaxError:
            continue
        names: list[str] = []
        for stmt in module.body:
            if isinstance(stmt, cst.ClassDef):
                names.append(stmt.name.value)
        out.append(
            AstModuleSummary(
                module=path.stem,
                path=path,
                class_names=tuple(names),
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class TransformerExemplar:
    """One ``parser: handle <rule>`` commit summary surfaced as an exemplar."""

    commit: str
    rule_name: str
    diff: str  # the per-file diff text (transformer + ast file slices)


_COMMIT_SUBJECT_RE = re.compile(r"^parser: handle ([a-z][a-z0-9_]*)\b")


def recent_transformer_exemplars(limit: int = 5) -> list[TransformerExemplar]:
    """U2 — git log for recent ``parser: handle <rule>`` commits + diffs.

    Each exemplar pairs (a) the AST dataclass that was added and (b) the
    transformer method that returns it. The LLM uses these to mimic both
    shape and style. We cap each diff at ~6 KB so the prompt stays bounded
    even for verbose commits.
    """
    try:
        log = subprocess.run(
            [
                "git", "log", "--all", "--pretty=%H%x09%s",
                "--grep=^parser: handle ", "-n", str(limit * 4),
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    if log.returncode != 0:
        return []
    out: list[TransformerExemplar] = []
    seen: set[str] = set()
    for line in log.stdout.splitlines():
        if "\t" not in line:
            continue
        sha, subject = line.split("\t", 1)
        m = _COMMIT_SUBJECT_RE.match(subject)
        if not m:
            continue
        rule = m.group(1)
        if rule in seen:
            continue
        seen.add(rule)
        diff = subprocess.run(
            [
                "git", "show", sha,
                "--",
                "src/argentum_press/parser/transformer.py",
                "src/argentum_press/parser/ast/",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        if not diff.strip():
            continue
        # Trim each exemplar so a verbose dataclass doesn't blow the prompt.
        out.append(
            TransformerExemplar(
                commit=sha[:12], rule_name=rule, diff=diff[:6000]
            )
        )
        if len(out) >= limit:
            break
    return out


@dataclass(frozen=True, slots=True)
class UnmodeledRuleContext:
    """All deterministic inputs to the unmodeled-rule LLM calls."""

    label: str
    rule_name: str
    rule: GrammarRule | None
    ast_modules: list[AstModuleSummary]
    exemplars: list[TransformerExemplar]
    project_dir: Path
    card_name: str = ""
    oracle_text: str = ""


def gather_unmodeled_rule(
    *,
    label: str,
    project_dir: Path,
    oracle_text: str = "",
    card_name: str = "",
) -> UnmodeledRuleContext:
    """Run U0 + U1 + U2 and bundle the results."""
    rule_name = label.split(":", 1)[-1] if ":" in label else label
    rule = grammar_rule_block(rule_name)
    return UnmodeledRuleContext(
        label=label,
        rule_name=rule_name,
        rule=rule,
        ast_modules=list_ast_modules(),
        exemplars=recent_transformer_exemplars(),
        project_dir=project_dir,
        card_name=card_name,
        oracle_text=oracle_text,
    )
