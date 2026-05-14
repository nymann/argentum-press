#!/usr/bin/env -S uv run python
# pyright: basic
"""Fix-loop for argentum-press parser/lowerer gaps.

Walks a Scryfall set, hands the first parse/lower gap to a fresh ``claude -p``
agent with rich pre-computed context, runs pytest as a verification gate,
and commits per-iteration progress. Replaces the older
``scripts/fix-parser-gaps.sh``.

Usage::

    uv run scripts/fix_parser_gaps.py <set-code> <project-dir> [options]

Options:
    --dry-run         Render the prompt for the next gap and exit. No claude call.
    --max-iter N      Cap iterations (default: unbounded; loop ends on no-gap).
    --no-commit       Skip per-iteration commits (debugging).
    --allow-dirty     Allow the loop to start with a dirty working tree.

The script invokes pytest from its own process between iterations; the
agent is told to run pytest too so it can fix its own regressions, but our
post-edit run is authoritative (loop aborts on red).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
GRAMMAR = REPO / "src/argentum_press/parser/grammar/grammar.py"
TRANSFORMER = REPO / "src/argentum_press/parser/transformer.py"
LOWERER = REPO / "src/argentum_press/lowerer.py"
AST_DIR = REPO / "src/argentum_press/parser/ast"

# Opt into the disk parse cache for the whole orchestrator process (and any
# subprocesses we spawn that inherit env). Each card costs 1-40s through the
# Earley parser; re-scanning ~170 candidates per iteration is the wall-clock
# pain. The cache turns iteration N>1 (or a restart of iteration 1) into
# microseconds for unchanged cards. Invalidation happens label-by-label after
# each successful parse-kind fix; see argentum_press.parse_cache.
os.environ.setdefault("ARGENTUM_PARSE_CACHE", "1")

PYTEST_TARGETS = (
    "tests/test_diagnose.py",
    "tests/test_pipeline.py",
    "tests/test_lowerer.py",
    "tests/test_classify.py",
)


# ---------------------------------------------------------------------------
# colors / output
# ---------------------------------------------------------------------------


def _ansi(code: str) -> str:
    return f"\033[{code}m" if sys.stdout.isatty() and not os.environ.get("NO_COLOR") else ""


GRAY = _ansi("90")
GREEN = _ansi("32")
RED = _ansi("31")
CYAN = _ansi("36")
YELLOW = _ansi("33")
MAGENTA = _ansi("35")
DIM = _ansi("2")
BOLD = _ansi("1")
RESET = _ansi("0")


def stamp(line: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"{GRAY}[{ts}]{RESET} {line}", flush=True)


# ---------------------------------------------------------------------------
# git / pytest helpers
# ---------------------------------------------------------------------------


def git(*args: str, capture: bool = True, check: bool = True) -> str:
    r = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=capture, text=True, check=check
    )
    return (r.stdout or "").rstrip("\n")


def git_dirty() -> bool:
    return bool(git("status", "--porcelain"))


def git_recent_commits(path: Path, n: int = 5) -> str:
    try:
        rel = path.relative_to(REPO)
    except ValueError:
        rel = path
    out = git("log", f"-{n}", "--oneline", "--", str(rel), check=False)
    return out or "(no recent commits)"


def run_pytest() -> tuple[int, str]:
    proc = subprocess.run(
        ["uv", "run", "pytest", *PYTEST_TARGETS, "-x", "-q", "-n", "auto"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# deterministic context blocks
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GapContext:
    set_code: str
    project_dir: Path
    kind: str
    label: str
    card_name: str
    oracle_text: str
    preprocessed_text: str | None
    parse_error_block: str | None
    ast_block: str | None
    gap_class_def: str | None
    rule_def: str | None
    rule_uses: str | None
    grammar_index_excerpt: str | None
    handler_map: str | None
    engine_hints: str | None
    file_sizes: str
    recent_commits: str


def file_sizes() -> str:
    items = [
        ("grammar.py", GRAMMAR),
        ("transformer.py", TRANSFORMER),
        ("lowerer.py", LOWERER),
    ]
    lines: list[str] = []
    for name, p in items:
        try:
            n = sum(1 for _ in p.open(encoding="utf-8"))
            lines.append(f"  {name}: {n} lines")
        except OSError:
            lines.append(f"  {name}: (unreadable)")
    return "\n".join(lines)


def grammar_index() -> dict[str, int]:
    """Map grammar rule name -> 1-based line number.

    The grammar lives in a single triple-quoted string returned from
    getGrammar(). Each rule declaration starts with whitespace + optional !
    + name + colon. We don't track re-declarations or continuations - the
    index is just a hint table for the agent.
    """
    rules: dict[str, int] = {}
    pat = re.compile(r"^\s+!?(\w+)\s*:")
    with GRAMMAR.open(encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            m = pat.match(line)
            if m and m.group(1) not in rules:
                rules[m.group(1)] = i
    return rules


def grammar_rule_block(rule: str) -> str | None:
    """Return the grammar definition of ``rule`` starting at its declaration.

    Reads up to the next blank line or next rule declaration. Returns None
    if not found.
    """
    pat = re.compile(rf"^\s+!?{re.escape(rule)}\s*:")
    next_rule = re.compile(r"^\s+!?\w+\s*:")
    with GRAMMAR.open(encoding="utf-8") as f:
        lines = f.readlines()
    for i, line in enumerate(lines):
        if pat.match(line):
            block = [f"{i + 1}: {line.rstrip()}"]
            for j in range(i + 1, min(i + 20, len(lines))):
                nxt = lines[j]
                if next_rule.match(nxt) or not nxt.strip():
                    break
                # Continuation line (alternative `| ...` or comment).
                block.append(f"{j + 1}: {nxt.rstrip()}")
            return "\n".join(block)
    return None


def grammar_rule_uses(rule: str, limit: int = 20) -> str | None:
    """Lines in grammar.py mentioning ``rule`` outside its own definition."""
    pat = re.compile(rf"\b{re.escape(rule)}\b")
    own = re.compile(rf"^\s+!?{re.escape(rule)}\s*:")
    out: list[str] = []
    with GRAMMAR.open(encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            if own.match(line):
                continue
            if pat.search(line):
                out.append(f"{i}: {line.rstrip()}")
                if len(out) >= limit:
                    break
    return "\n".join(out) if out else None


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def gap_class_definition(bare_label: str) -> str | None:
    pat = re.compile(rf"^class {re.escape(bare_label)}[(:]")
    for ast_file in sorted(AST_DIR.glob("*.py")):
        with ast_file.open(encoding="utf-8") as f:
            lines = f.readlines()
        for i, line in enumerate(lines):
            if pat.match(line):
                start = max(i - 2, 0)
                end = min(i + 20, len(lines))
                rel = ast_file.relative_to(REPO)
                excerpt = "".join(lines[start:end])
                return f"from {rel}:\n\n{excerpt.rstrip()}"
    return None


def handler_map() -> str:
    """All ``@<dispatcher>.register`` lines in lowerer.py with line numbers."""
    pat = re.compile(r"^\s*@[A-Za-z_]+\.register\b")
    out: list[str] = []
    with LOWERER.open(encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            if pat.match(line):
                # The @register decorator's argument is the AST class for
                # this handler - exactly what the agent needs to match
                # against the gap class.
                out.append(f"{i}: {line.rstrip()}")
    return "\n".join(out) if out else "(no @register lines found)"


def engine_dsl_hints(bare_label: str, project_dir: Path) -> str | None:
    keyword = bare_label
    for suffix in ("Expression", "Statement", "Ability"):
        if keyword.endswith(suffix):
            keyword = keyword[: -len(suffix)]
            break
    if not keyword or not project_dir.is_dir():
        return None
    # rg is faster + respects .gitignore; fall back to grep -r if missing.
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


def grammar_index_excerpt(near_rule: str | None = None, limit: int = 30) -> str:
    rules = grammar_index()
    items = sorted(rules.items(), key=lambda kv: kv[1])
    if near_rule and near_rule in rules:
        # Show a window around the target rule.
        target = rules[near_rule]
        items = [it for it in items if abs(it[1] - target) <= 200][:limit]
    else:
        items = items[:limit]
    return "\n".join(f"  line {ln}: {name}" for name, ln in items)


# ---------------------------------------------------------------------------
# context assembly
# ---------------------------------------------------------------------------


def gather_context(
    *,
    set_code: str,
    project_dir: Path,
    kind: str,
    label: str,
    card_name: str,
    oracle_text: str,
    ast_text: str | None,
    parse_error_block: str | None,
) -> GapContext:
    bare_label = label.split(":", 1)[-1] if ":" in label else label
    bare_label = bare_label.split(".")[-1]

    is_unmodeled = label.startswith("unmodeled-rule:")
    is_parse_error = label.startswith("parse-error:")

    primary_file = (
        LOWERER if kind == "lower"
        else GRAMMAR if is_parse_error
        else TRANSFORMER
    )

    rule_def = rule_uses = grammar_excerpt = None
    if is_unmodeled:
        rule_def = grammar_rule_block(bare_label)
        rule_uses = grammar_rule_uses(bare_label)
        grammar_excerpt = grammar_index_excerpt(near_rule=bare_label)
    elif is_parse_error:
        # No specific rule name to anchor to; show the full index.
        grammar_excerpt = grammar_index_excerpt()

    class_def = (
        gap_class_definition(bare_label) if kind == "lower" else None
    )

    h_map = handler_map() if kind == "lower" else None
    hints = engine_dsl_hints(bare_label, project_dir) if kind == "lower" else None

    return GapContext(
        set_code=set_code,
        project_dir=project_dir,
        kind=kind,
        label=label,
        card_name=card_name,
        oracle_text=oracle_text,
        preprocessed_text=None,
        parse_error_block=parse_error_block,
        ast_block=ast_text,
        gap_class_def=class_def,
        rule_def=rule_def,
        rule_uses=rule_uses,
        grammar_index_excerpt=grammar_excerpt,
        handler_map=h_map,
        engine_hints=hints,
        file_sizes=file_sizes(),
        recent_commits=git_recent_commits(primary_file),
    )


# ---------------------------------------------------------------------------
# prompt rendering (one per gap kind)
# ---------------------------------------------------------------------------


_COMMON_TAIL = """
FILES YOU MAY EDIT
  src/argentum_press/parser/transformer.py   (~1900 lines)
  src/argentum_press/parser/ast/*.py         (frozen-dataclass AST nodes)
  src/argentum_press/parser/grammar/grammar.py (~940 lines; only for parse-error)
  src/argentum_press/lowerer.py              (AST -> Kotlin DSL)

DISCIPLINE
  - All needed signal is above. Don't re-run diagnose; the orchestrator runs
    it again before the next iteration.
  - Don't run pytest more than once unless you've made a follow-up edit.
  - Don't commit; the orchestrator owns commits.
  - For lowerer.py / transformer.py: grep before Read - they're 1k+ lines.
  - The minimum edit to move this gap is the goal. No refactors, no
    drive-by cleanup, no unrelated rule changes.

WORKFLOW
  1. Make the minimum edit.
  2. Run pytest:
     uv run pytest tests/test_diagnose.py tests/test_pipeline.py \\
       tests/test_lowerer.py tests/test_classify.py -x -q -n auto
  3. If pytest red, fix and re-run.
"""


def render_prompt_lower(ctx: GapContext) -> str:
    parts = [
        "Fix one lowerer gap in argentum-press.",
        "",
        "CARD",
        f"  name: {ctx.card_name}",
        "  oracle text:",
        _indent(ctx.oracle_text, "    "),
        "",
        f"GAP  kind=lower  label={ctx.label}",
        "  An AST node parsed cleanly but lowerer.py has no @register handler",
        "  for it. Add one handler. Do NOT change the AST or transformer.",
        "",
        "GAP AST CLASS DEFINITION (the fields the new handler will receive)",
        ctx.gap_class_def or "  (not found in parser/ast/; grep src/ for it)",
        "",
        "PARSED AST FOR THIS CARD (where the gap node sits in the tree)",
        _indent(ctx.ast_block or "(no AST)", "  "),
        "",
        "HANDLER MAP (every @<dispatcher>.register line in lowerer.py).",
        "Pick a handler whose AST class is structurally similar to the GAP",
        "AST CLASS above and mirror its body.",
        _indent(ctx.handler_map or "(none)", "  "),
        "",
        "ENGINE DSL HINTS (Kotlin DSL surface in argentum-engine that already",
        "exists for this kind of effect). Mirror existing DSL - do NOT invent.",
        _indent(ctx.engine_hints or "(no matches)", "  "),
        "",
        "FILE SIZES",
        ctx.file_sizes,
        "",
        "RECENT COMMITS TOUCHING lowerer.py",
        _indent(ctx.recent_commits, "  "),
        _COMMON_TAIL,
    ]
    return "\n".join(parts)


def render_prompt_parse_error(ctx: GapContext) -> str:
    parts = [
        "Fix one grammar gap in argentum-press.",
        "",
        "CARD",
        f"  name: {ctx.card_name}",
        "  oracle text (raw):",
        _indent(ctx.oracle_text, "    "),
        "",
        f"GAP  kind=parse  label={ctx.label}",
        "  Lark itself rejected the preprocessed text. Either the grammar is",
        "  missing a rule branch, or an existing rule needs a new alternative.",
        "",
        "PARSE ERROR DETAIL (extracted from the Lark exception; no need to",
        "re-run the parser)",
        ctx.parse_error_block or "  (details unavailable)",
        "",
        "GRAMMAR RULE INDEX (top of grammar.py; rule name -> 1-based line)",
        ctx.grammar_index_excerpt or "  (none)",
        "",
        "FILE SIZES",
        ctx.file_sizes,
        "",
        "RECENT COMMITS TOUCHING grammar.py",
        _indent(ctx.recent_commits, "  "),
        _COMMON_TAIL,
    ]
    return "\n".join(parts)


def render_prompt_unmodeled(ctx: GapContext) -> str:
    parts = [
        "Fix one transformer gap in argentum-press.",
        "",
        "CARD",
        f"  name: {ctx.card_name}",
        "  oracle text (raw):",
        _indent(ctx.oracle_text, "    "),
        "",
        f"GAP  kind=parse  label={ctx.label}",
        "  Lark parsed the text fine, but the transformer has no method for",
        "  the named rule (raised via __default__ -> LoweringIncomplete).",
        "  Add a transformer method; if a new AST dataclass is needed, add it",
        "  to parser/ast/<file>.py and mirror its frozen/slots neighbors.",
        "",
        "GRAMMAR RULE DEFINITION (for the failing rule)",
        _indent(ctx.rule_def or "(not found)", "  "),
        "",
        "WHERE THIS RULE IS USED in grammar.py (parent rules - their",
        "transformer methods are the natural analogs to mirror)",
        _indent(ctx.rule_uses or "(no other references)", "  "),
        "",
        "GRAMMAR RULE INDEX (rules near the target; rule name -> line)",
        ctx.grammar_index_excerpt or "  (none)",
        "",
        "FILE SIZES",
        ctx.file_sizes,
        "",
        "RECENT COMMITS TOUCHING transformer.py",
        _indent(ctx.recent_commits, "  "),
        _COMMON_TAIL,
    ]
    return "\n".join(parts)


def render_prompt(ctx: GapContext) -> str:
    if ctx.kind == "lower":
        return render_prompt_lower(ctx)
    if ctx.label.startswith("parse-error:"):
        return render_prompt_parse_error(ctx)
    return render_prompt_unmodeled(ctx)


# ---------------------------------------------------------------------------
# claude streaming
# ---------------------------------------------------------------------------


def stream_claude(prompt: str) -> tuple[int, str]:
    """Pipe ``prompt`` to ``claude -p`` and render its stream-json output.

    Returns (exit_code, last_assistant_text). The last assistant text is the
    agent's final summary, used as the body of the per-iteration commit.
    """
    proc = subprocess.Popen(
        [
            "claude", "-p",
            "--dangerously-skip-permissions",
            "--output-format", "stream-json",
            "--verbose",
        ],
        cwd=REPO,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdin and proc.stdout
    proc.stdin.write(prompt)
    proc.stdin.close()

    last_text = ""
    for raw in proc.stdout:
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            stamp(f"{DIM}{line[:240]}{RESET}")
            continue
        text = _render_event(ev)
        if text is not None:
            stamp(text)
        if ev.get("type") == "assistant":
            for c in ev.get("message", {}).get("content", []) or []:
                if c.get("type") == "text":
                    last_text = c.get("text", "") or last_text
    proc.wait()
    return proc.returncode, last_text


# --- syntax highlighting for Read tool_results ------------------------------
#
# Whole-file highlighting is the only correct approach: Python's triple-quoted
# strings, parenthesized expressions and f-string nesting cross line boundaries,
# so pygments has to see the full file to color them right. We cache the result
# keyed by (path, mtime_ns, size) — stat'ing is microseconds, re-highlighting
# is milliseconds even for the 940-line grammar, and the agent's edits bump
# mtime so the cache auto-invalidates.

_highlight_cache: dict[tuple[str, int, int], list[str]] = {}

# Map of tool_use id -> {name, input}, populated as we see assistant tool_use
# events. When the matching tool_result arrives we look up by tool_use_id so
# Read results get routed through the highlighter.
_tool_uses_by_id: dict[str, dict[str, Any]] = {}


def _highlight_file_lines(path: str) -> list[str] | None:
    """Return ANSI-highlighted lines of ``path``, or None if pygments can't
    handle the file (no lexer, file unreadable, etc.). Cached by stat tuple."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime_ns, st.st_size)
    cached = _highlight_cache.get(key)
    if cached is not None:
        return cached
    try:
        import pygments
        from pygments.formatters import TerminalFormatter
        from pygments.lexers import get_lexer_for_filename
        from pygments.util import ClassNotFound
    except ImportError:
        return None
    try:
        content = Path(path).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        lexer = get_lexer_for_filename(path)
    except ClassNotFound:
        return None
    highlighted = pygments.highlight(content, lexer, TerminalFormatter())
    lines = highlighted.splitlines()
    _highlight_cache[key] = lines
    return lines


def _render_read_result(file_path: str, offset: int | None, limit: int | None) -> str | None:
    """Render a Read tool_result by slicing the cached highlighted file.

    Returns a multi-line string (with line-number prefixes mirroring the agent's
    Read tool format), or None if highlighting wasn't possible — caller falls
    back to the raw tool_result content in that case.
    """
    lines = _highlight_file_lines(file_path)
    if lines is None:
        return None
    start = max((offset or 1) - 1, 0)
    # Read tool's default limit is ~2000 lines; mirror so we don't show the
    # whole 10k-line file when the agent issued a no-limit read.
    end = start + (limit if limit is not None else 2000)
    end = min(end, len(lines))
    if start >= len(lines):
        return None
    return "\n".join(
        f"{DIM}{(start + 1 + i):>4}{RESET}  {ln}"
        for i, ln in enumerate(lines[start:end])
    )


# ----------------------------------------------------------------------------


def _indent(text: str, prefix: str = "  ") -> str:
    """Indent every line of ``text`` by ``prefix``."""
    return "\n".join(prefix + ln for ln in text.splitlines())


def _truncate(text: str, n: int) -> str:
    """One-line truncate with ellipsis marker (keeps strings short for diff preview)."""
    text = text.replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


def _cap_lines(text: str, max_lines: int, indent: str = "  ") -> str:
    """Cap a multi-line block to ``max_lines``, appending a ``… (N more lines)`` marker.

    Each line is prefixed with ``indent``; the marker is also indented.
    """
    lines = text.splitlines() or [""]
    if len(lines) <= max_lines:
        return "\n".join(indent + ln for ln in lines)
    kept = lines[:max_lines]
    extra = len(lines) - max_lines
    body = "\n".join(indent + ln for ln in kept)
    return f"{body}\n{indent}{DIM}… ({extra} more lines){RESET}"


def _render_tool_use(name: str, inp: dict[str, Any]) -> str:
    """Render a single ``tool_use`` event into a (possibly multi-line) string."""
    if name == "Bash":
        cmd = str(inp.get("command", ""))
        desc = inp.get("description")
        head = f"{GREEN}$ {cmd}{RESET}"
        if desc:
            head += f"\n  {DIM}# {desc}{RESET}"
        return head
    if name == "Read":
        path = inp.get("file_path", "?")
        extras = []
        if "offset" in inp:
            extras.append(f"offset={inp['offset']}")
        if "limit" in inp:
            extras.append(f"limit={inp['limit']}")
        tail = f" [{', '.join(extras)}]" if extras else ""
        return f"{CYAN}read{RESET} {DIM}{path}{tail}{RESET}"
    if name == "Edit":
        path = inp.get("file_path", "?")
        old = _truncate(str(inp.get("old_string", "")), 100)
        new = _truncate(str(inp.get("new_string", "")), 100)
        replace_all = inp.get("replace_all")
        flag = f" {DIM}(replace_all){RESET}" if replace_all else ""
        return (
            f"{CYAN}edit{RESET} {path}{flag}\n"
            f"  {DIM}- {old}{RESET}\n"
            f"  {DIM}+ {new}{RESET}"
        )
    if name == "Write":
        path = inp.get("file_path", "?")
        return f"{CYAN}write{RESET} {path}"
    if name == "Grep":
        pattern = inp.get("pattern", "?")
        path = inp.get("path", ".")
        return f"{CYAN}grep{RESET} {pattern} {DIM}in {path}{RESET}"
    if name == "Glob":
        pattern = inp.get("pattern", "?")
        return f"{CYAN}glob{RESET} {pattern}"
    # Fallback: pretty-print JSON over multiple lines if it has >1 key, capped.
    inp = inp or {}
    if len(inp) > 1:
        pretty = json.dumps(inp, indent=2, ensure_ascii=False)
        capped = _cap_lines(pretty, 8, indent="  ")
        return f"{GREEN}-> {name}{RESET}\n{capped}"
    payload = json.dumps(inp, ensure_ascii=False)
    return f"{GREEN}-> {name}{RESET} {payload}"


def _render_event(ev: dict[str, Any]) -> str | None:
    t = ev.get("type")
    if t == "system" and ev.get("subtype") == "init":
        return f"{MAGENTA}[claude init]{RESET} model={ev.get('model', '?')}"
    if t == "assistant":
        out: list[str] = []
        for c in ev.get("message", {}).get("content", []) or []:
            if c.get("type") == "text":
                text = c.get("text", "") or ""
                # Preserve newlines; color only the marker so multi-line text
                # reads naturally.
                first, _, rest = text.partition("\n")
                if rest:
                    out.append(f"{CYAN}>>{RESET} {first}\n{rest}")
                else:
                    out.append(f"{CYAN}>>{RESET} {first}")
            elif c.get("type") == "tool_use":
                out.append(_render_tool_use(c.get("name") or "?", c.get("input") or {}))
        return "\n".join(out) if out else None
    if t == "user":
        out: list[str] = []
        for c in ev.get("message", {}).get("content", []) or []:
            if c.get("type") == "tool_result":
                raw = c.get("content")
                # Tool results sometimes arrive as a list of content blocks
                # ([{"type":"text","text":"..."}]). Normalize to a string.
                if isinstance(raw, list):
                    parts: list[str] = []
                    for blk in raw:
                        if isinstance(blk, dict) and blk.get("type") == "text":
                            parts.append(str(blk.get("text", "")))
                        elif isinstance(blk, dict):
                            parts.append(json.dumps(blk, ensure_ascii=False))
                        else:
                            parts.append(str(blk))
                    body = "\n".join(parts)
                else:
                    body = str(raw or "")
                is_err = bool(c.get("is_error"))
                color = RED if is_err else CYAN
                marker = "→ ERROR" if is_err else "→"
                if not body.strip():
                    out.append(f"  {color}{marker}{RESET} {DIM}(no output){RESET}")
                    continue
                capped = _cap_lines(body, 20, indent="  ")
                # Recolor the leading "  " on the first line into a marker.
                # We replace only the first occurrence so the rest of the
                # block stays indented but uncolored.
                if capped.startswith("  "):
                    first_line, nl, rest = capped[2:].partition("\n")
                    head = f"  {color}{marker}{RESET} {first_line}"
                    capped = head + (nl + rest if nl else "")
                out.append(capped)
        return "\n".join(out) if out else None
    if t == "result":
        return (
            f"{MAGENTA}[claude done]{RESET} subtype={ev.get('subtype', '?')}  "
            f"turns={ev.get('num_turns', 0)}  cost=${ev.get('total_cost_usd', 0)}"
        )
    return None


# ---------------------------------------------------------------------------
# commit
# ---------------------------------------------------------------------------


def commit_iteration(
    *, iteration: int, set_code: str, gap_kind: str, label: str, card: str, summary: str
) -> None:
    if not git_dirty():
        stamp(
            f"{YELLOW}no working-tree changes after iteration {iteration}; "
            f"skipping commit{RESET}"
        )
        return
    # Stage only files under the press repo (the agent shouldn't be editing
    # outside, but be defensive). `git add -u` picks up modifications;
    # `git add src/` picks up new AST files.
    git("add", "-u")
    git("add", "src/", check=False)
    subject = _commit_subject(gap_kind, label, card)
    body = summary.strip() or f"set={set_code}, iteration {iteration}"
    body = "\n".join(body.splitlines()[:20])  # cap for sanity
    msg = f"{subject}\n\n{body}\n"
    subprocess.run(["git", "commit", "-m", msg], cwd=REPO, check=True)
    _push_or_warn()


def _push_or_warn() -> None:
    """Push HEAD to its upstream after a successful commit. Warn but don't
    abort on push failure — the commit is preserved locally and the user can
    retry with a manual ``git push``. Aborting the fix-loop on a transient
    network error would forfeit the iteration's progress.
    """
    try:
        subprocess.run(
            ["git", "push"], cwd=REPO, check=True,
            capture_output=True, text=True,
        )
        stamp(f"{DIM}git push: ok{RESET}")
    except subprocess.CalledProcessError as e:
        snippet = (e.stderr or e.stdout or "").strip()[:300]
        stamp(
            f"{YELLOW}git push failed (commit preserved locally): "
            f"{snippet}{RESET}"
        )


_LABEL_BARE = re.compile(r"^(parse-error|unmodeled-rule|lark-error):")


def _commit_subject(kind: str, label: str, card: str) -> str:
    scope = "parser" if kind == "parse" else "lowerer"
    # Cut a short, human label out of the gap label.
    bare = _LABEL_BARE.sub("", label).split(":", 1)[-1]
    bare = bare.split(".")[-1]
    bare = bare.strip()[:60] or label[:60]
    return f"{scope}: handle {bare} ({card})"[:100]


# ---------------------------------------------------------------------------
# main loop
# ---------------------------------------------------------------------------


class GapSubprocessError(RuntimeError):
    """Raised when the gap-finding subprocess fails (nonzero exit, no result
    event, malformed output). Distinct from "no gaps remaining" — the main
    loop catches this and aborts instead of misreporting a clean scan."""


def _find_gap_subprocess(
    set_code: str, project_dir: Path
) -> tuple[Any, str | None, str | None]:
    """Run gap-finding in a fresh subprocess so the agent's last edits take
    effect. Returns ``(gap, ast_text, parse_error_block)`` — ``gap`` is None
    for a clean set.

    The orchestrator's main loop has been calling ``argentum_press.*`` modules
    in-process. Python caches imported modules in ``sys.modules`` for the
    lifetime of the process; the agent edits ``grammar.py`` / ``transformer.py``
    / ``lowerer.py`` between iterations, but those edits are invisible to
    already-imported modules. Spawning a fresh ``python -m argentum_press
    ._fix_loop_gap`` subprocess sidesteps that entirely — each iteration's
    scan re-imports from disk.

    Inherits ``ARGENTUM_PARSE_CACHE`` (and any sibling env) so the worker
    populates the same parse cache the orchestrator's invalidate-after-fix
    pruning operates on.
    """
    cmd = [
        "uv", "run", "python", "-m", "argentum_press._fix_loop_gap",
        set_code, str(project_dir),
    ]
    proc = subprocess.Popen(
        cmd,
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdout and proc.stderr

    result_event: dict[str, Any] | None = None
    for raw in proc.stdout:
        line = raw.rstrip("\n").strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            # Non-JSON output (e.g. a stray print) — surface it but don't
            # treat it as fatal.
            stamp(f"{DIM}{line[:240]}{RESET}")
            continue
        et = event.get("type")
        if et == "log":
            # Map the worker's named color → local ANSI constant; default
            # is DIM. _ansi() already gates on TTY/NO_COLOR upstream, so
            # color names safely degrade to empty strings off-tty.
            color_name = event.get("color")
            color_map = {"green": GREEN, "red": RED, "yellow": YELLOW, "cyan": CYAN}
            prefix = color_map.get(color_name, DIM)
            stamp(f"{prefix}{event.get('msg', '')}{RESET}")
        elif et == "result":
            result_event = event

    stderr_output = proc.stderr.read()
    proc.wait()
    if proc.returncode != 0:
        # Subprocess crashed (e.g. an uncaught exception in the parser). The
        # main loop must distinguish this from a clean set — returning a
        # None-tuple here used to silently produce "no gaps remaining. done."
        # and mask the real failure.
        msg = f"gap subprocess exited {proc.returncode}"
        if stderr_output.strip():
            msg += f"\n{stderr_output.strip()[:1500]}"
        raise GapSubprocessError(msg)
    if result_event is None:
        raise GapSubprocessError("gap subprocess produced no result event")

    gap_data = result_event.get("gap")
    if gap_data is None:
        return None, None, None

    from argentum_press.diagnose import Gap
    gap = Gap(
        kind=gap_data["kind"],
        label=gap_data["label"],
        card_name=gap_data["card_name"],
        oracle_text=gap_data["oracle_text"],
        parse_details=None,
    )
    return gap, result_event.get("ast"), result_event.get("parse_error_block")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("set_code")
    ap.add_argument("project_dir", type=Path)
    ap.add_argument("--max-iter", type=int, default=0, help="0 = unbounded")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument("--allow-dirty", action="store_true")
    args = ap.parse_args()

    if not args.allow_dirty and git_dirty():
        print(
            f"{RED}working tree is dirty; commit or stash before running.{RESET}\n"
            f"  (pass --allow-dirty to override)",
            file=sys.stderr,
        )
        return 2

    prev_label = ""
    i = 0
    while True:
        i += 1
        if args.max_iter and i > args.max_iter:
            stamp(f"{YELLOW}reached --max-iter={args.max_iter}{RESET}")
            return 0
        print(f"\n{BOLD}=== iteration {i} ==={RESET}", flush=True)

        try:
            gap, ast_text, pe_block = _find_gap_subprocess(
                args.set_code, args.project_dir
            )
        except GapSubprocessError as e:
            stamp(f"{RED}{e}{RESET}")
            return 2
        if gap is None:
            stamp(f"{GREEN}no gaps remaining. done.{RESET}")
            return 0
        if gap.label == prev_label:
            stamp(f"{RED}no progress: label '{gap.label}' twice in a row. abort.{RESET}")
            return 2

        ctx = gather_context(
            set_code=args.set_code,
            project_dir=args.project_dir,
            kind=gap.kind,
            label=gap.label,
            card_name=gap.card_name,
            oracle_text=gap.oracle_text,
            ast_text=ast_text,
            parse_error_block=pe_block,
        )

        stamp(f"{YELLOW}gap{RESET} kind={gap.kind}  card={gap.card_name}  "
              f"label={gap.label.splitlines()[0]}")

        prompt = render_prompt(ctx)
        if args.dry_run:
            print(prompt)
            return 0

        rc, summary = stream_claude(prompt)
        if rc != 0:
            stamp(f"{RED}claude exited {rc}; aborting loop.{RESET}")
            return rc

        stamp(f"{DIM}running pytest...{RESET}")
        rc, output = run_pytest()
        if rc != 0:
            stamp(f"{RED}pytest red after agent edit; abort.{RESET}")
            print(output[-2000:], file=sys.stderr)
            return rc
        stamp(f"{GREEN}pytest green.{RESET}")

        if not args.no_commit:
            commit_iteration(
                iteration=i,
                set_code=args.set_code,
                gap_kind=gap.kind,
                label=gap.label,
                card=gap.card_name,
                summary=summary,
            )

        # Cache invalidation: parse-kind fixes change what the parser produces
        # for cards previously labeled with this gap, so drop those entries.
        # Lower-kind fixes don't change parse output (the AST is unchanged);
        # classify runs fresh each iteration and picks up the new lowerer
        # naturally, so the parse cache stays valid.
        if gap.kind == "parse":
            from argentum_press.parse_cache import invalidate_label
            removed = invalidate_label(gap.label)
            stamp(f"{DIM}parse-cache: invalidated {removed} entr(ies) for '{gap.label}'{RESET}")

        prev_label = gap.label


if __name__ == "__main__":
    raise SystemExit(main())
