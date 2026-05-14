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
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from argentum_press.fix_strategy import (
    FreeformFixer,
    GapFixer,
    IterationContext,
    LowerPlaybookFixer,
    ParseErrorPlaybookFixer,
    UnmodeledRulePlaybookFixer,
)

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


def render_prompt(ctx: GapContext) -> str:
    """Backwards-compatible default: render with the baseline variant.

    The main loop calls this when --prompt-variant is unset. ``--dry-run``
    keeps the same shape too. New code should call _render_prompt_variant
    directly so we have one place to add A/B logic.
    """
    return _render_prompt_variant("baseline", ctx)


# ---------------------------------------------------------------------------
# template-driven prompt variants (Phase 3)
# ---------------------------------------------------------------------------
#
# Each variant lives under prompts/<variant>/ with three files:
#   - lower.md          (lowerer-gap prompt)
#   - parse-error.md    (lark rejected the preprocessed text)
#   - unmodeled.md      (lark parsed but transformer has no rule method)
#   - _common_tail.md   (boilerplate appended to every prompt)
#
# Placeholders use a minimal {{name}} syntax. We don't pull in jinja2 or
# similar — the substitution domain is fixed and small, dependency-free
# keeps the orchestrator easy to debug. Unknown placeholders raise so a
# typo in a hand-edited variant surfaces immediately instead of silently
# rendering as a literal `{{rule_def}}`.

PROMPTS_DIR = REPO / "prompts"


def _placeholders_for(ctx: GapContext) -> dict[str, str]:
    """Build the substitution table for a single gap context.

    All values are strings; multi-line blocks already include their internal
    newlines. The ``_indented_N`` variants pre-bake the per-line indent
    that the inline renderer used to compute via ``_indent(text, '  ' * N)``.
    """
    rule_def = ctx.rule_def or "(not found)"
    rule_uses = ctx.rule_uses or "(no other references)"
    gap_class_def = ctx.gap_class_def or "  (not found in parser/ast/; grep src/ for it)"
    ast_block = ctx.ast_block or "(no AST)"
    handler_map = ctx.handler_map or "(none)"
    engine_hints = ctx.engine_hints or "(no matches)"
    parse_error_block = ctx.parse_error_block or "  (details unavailable)"
    grammar_index_excerpt = ctx.grammar_index_excerpt or "  (none)"
    return {
        "card_name": ctx.card_name,
        "label": ctx.label,
        "oracle_text": ctx.oracle_text,
        "oracle_text_indented_4": _indent(ctx.oracle_text, "    "),
        "gap_class_def": gap_class_def,
        "ast_block_indented_2": _indent(ast_block, "  "),
        "handler_map_indented_2": _indent(handler_map, "  "),
        "engine_hints_indented_2": _indent(engine_hints, "  "),
        "parse_error_block": parse_error_block,
        "grammar_index_excerpt": grammar_index_excerpt,
        "rule_def_indented_2": _indent(rule_def, "  "),
        "rule_uses_indented_2": _indent(rule_uses, "  "),
        "file_sizes": ctx.file_sizes,
        "recent_commits_indented_2": _indent(ctx.recent_commits, "  "),
    }


_PLACEHOLDER_RE = re.compile(r"\{\{(\w+)\}\}")


def _apply(template: str, values: dict[str, str]) -> str:
    """Substitute every ``{{name}}`` in ``template`` with ``values[name]``.

    Missing keys raise ``KeyError`` so a typo in a hand-edited variant
    surfaces loudly instead of pasting ``{{rule_def}}`` into the agent's
    prompt verbatim. (We don't escape ``{{`` — none of the prompt text
    contains literal double braces.)
    """
    def repl(m: "re.Match[str]") -> str:
        name = m.group(1)
        if name not in values:
            raise KeyError(f"unknown prompt placeholder: {{{{ {name} }}}}")
        return values[name]
    return _PLACEHOLDER_RE.sub(repl, template)


def _template_for(variant: str, kind: str) -> tuple[str, str]:
    """Return ``(body_template, common_tail)`` for ``variant`` + ``kind``.

    ``kind`` is one of ``lower``, ``parse-error``, ``unmodeled``. Trailing
    newlines from the .md files are stripped so the join behaviour matches
    the pre-refactor inline rendering byte-for-byte (baseline parity is a
    test invariant).
    """
    base = PROMPTS_DIR / variant
    if not base.is_dir():
        raise FileNotFoundError(f"prompt variant '{variant}' has no directory under {PROMPTS_DIR}")
    body_path = base / f"{kind}.md"
    if not body_path.is_file():
        raise FileNotFoundError(
            f"prompt variant '{variant}' is missing {kind}.md (looked at {body_path})"
        )
    tail_path = base / "_common_tail.md"
    body = body_path.read_text(encoding="utf-8").rstrip("\n")
    tail = tail_path.read_text(encoding="utf-8").rstrip("\n") if tail_path.is_file() else ""
    return body, tail


def _render_prompt_variant(variant: str, ctx: GapContext) -> str:
    """Dispatch to a prompt variant by name.

    Templates live in ``prompts/<variant>/<kind>.md`` with ``{{name}}``
    placeholders; the common tail (FILES YOU MAY EDIT, DISCIPLINE, WORKFLOW)
    is shared across kinds via ``_common_tail.md``. Variant 'baseline' is
    the in-repo default and reproduces the pre-Phase-3 inline rendering
    byte-for-byte.
    """
    if ctx.kind == "lower":
        kind = "lower"
    elif ctx.label.startswith("parse-error:"):
        kind = "parse-error"
    else:
        kind = "unmodeled"
    body, tail = _template_for(variant, kind)
    values = _placeholders_for(ctx)
    values["common_tail"] = tail
    return _apply(body, values)


# ---------------------------------------------------------------------------
# recording (Phase 0 instrumentation)
# ---------------------------------------------------------------------------


# Tool-use names we tally per iteration. Anything outside this set lands in the
# generic "other" bucket — but we don't report "other" in the tsv columns,
# which are explicitly named (n_reads, n_greps, …). Extending the tsv schema
# is a breaking change for downstream summary tools, so add columns judiciously.
_TRACKED_TOOLS = {"Read", "Grep", "Edit", "Write", "Bash"}


RUNS_TSV_HEADER = (
    "started_at\tcommit_before\tcommit_after\tgap_kind\tgap_label\tcard_name\t"
    "scanned\tcost_usd\tnum_turns\twall_s\tn_reads\tn_greps\tn_edits\tn_writes\t"
    "n_bash\toutcome\tdescription"
)


def _gap_slug(label: str) -> str:
    """File-safe encoding of a gap label.

    Labels are things like ``unmodeled-rule:colorandexpr`` or
    ``parse-error:no terminal matches '@' …``. Slashes, colons, spaces, and
    other punctuation become single underscores so the slug works as a path
    component and roundtrips cleanly through TSVs.
    """
    if not label:
        return "no-gap"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_")
    return (slug[:80] or "no-gap")


def _iso_now() -> str:
    """ISO-8601 UTC timestamp with no colons, safe as a path component."""
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


@dataclass(slots=True)
class IterationRecord:
    """One row's worth of data, accumulated across an iteration.

    The Recorder builds these and writes them as a tsv row after the iteration
    settles (pass/abort/etc.). Counts default to 0 so a row is still well-formed
    even when claude exits early before emitting any tool_use events.
    """

    started_at: str
    commit_before: str
    iter_n: int
    gap_kind: str = ""
    gap_label: str = ""
    card_name: str = ""
    scanned: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    wall_s: float = 0.0
    tool_counts: dict[str, int] = field(default_factory=dict)
    commit_after: str = ""
    outcome: str = ""
    description: str = ""

    def as_tsv_row(self) -> str:
        tc = self.tool_counts
        cells: list[str] = [
            self.started_at,
            self.commit_before,
            self.commit_after,
            self.gap_kind,
            self.gap_label,
            self.card_name,
            str(self.scanned),
            f"{self.cost_usd:.6f}",
            str(self.num_turns),
            f"{self.wall_s:.3f}",
            str(tc.get("Read", 0)),
            str(tc.get("Grep", 0)),
            str(tc.get("Edit", 0)),
            str(tc.get("Write", 0)),
            str(tc.get("Bash", 0)),
            self.outcome,
            self.description.replace("\t", " ").replace("\n", " "),
        ]
        return "\t".join(cells)


class Recorder:
    """Per-record-dir state for Phase 0 instrumentation.

    Lifecycle: ``start_iteration()`` returns an ``IterationRecord``; the caller
    populates it as the iteration progresses, then calls ``finish_iteration``.
    The recorder writes the TSV header on first use and appends one row per
    finished iteration. Transcript and scan jsonl files are managed by helpers
    (``scan_jsonl_path``, ``transcript_jsonl_path``) so the rest of the
    orchestrator can stream directly to them.
    """

    def __init__(self, record_dir: Path) -> None:
        self.record_dir = record_dir
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.runs_tsv = self.record_dir / "runs.tsv"
        if not self.runs_tsv.exists():
            self.runs_tsv.write_text(RUNS_TSV_HEADER + "\n", encoding="utf-8")

    def start_iteration(self, iter_n: int) -> IterationRecord:
        return IterationRecord(
            started_at=_iso_now(),
            commit_before=git("rev-parse", "HEAD", check=False) or "",
            iter_n=iter_n,
        )

    def scan_jsonl_path(self, rec: IterationRecord, slug: str | None = None) -> Path:
        # Slug is unknown at scan-time (the gap hasn't been found yet), so
        # callers pass "scan" as a sentinel until a real label is available.
        # Append "-scan" so the scan file and the transcript file never collide.
        s = slug or "scan"
        return self.record_dir / f"{rec.started_at}-{rec.iter_n:03d}-{s}.scan.jsonl"

    def transcript_jsonl_path(self, rec: IterationRecord, slug: str) -> Path:
        return self.record_dir / f"{rec.started_at}-{rec.iter_n:03d}-{slug}.jsonl"

    def finish_iteration(self, rec: IterationRecord) -> None:
        rec.commit_after = git("rev-parse", "HEAD", check=False) or rec.commit_before
        with self.runs_tsv.open("a", encoding="utf-8") as f:
            f.write(rec.as_tsv_row() + "\n")


# ---------------------------------------------------------------------------
# claude streaming
# ---------------------------------------------------------------------------


def stream_claude(
    prompt: str,
    *,
    transcript_path: Path | None = None,
    record: IterationRecord | None = None,
    claude_cmd: list[str] | None = None,
) -> tuple[int, str]:
    """Pipe ``prompt`` to ``claude -p`` and render its stream-json output.

    Returns (exit_code, last_assistant_text). The last assistant text is the
    agent's final summary, used as the body of the per-iteration commit.

    When ``transcript_path`` is set, every raw NDJSON line received from claude
    is appended verbatim — preserving the exact wire format so replay analysis
    can re-render it identically. When ``record`` is set, per-event metrics
    (tool-use counts, cost, num_turns, wall_s) are folded into the record in
    place. ``claude_cmd`` lets tests swap in a shim binary; production code
    passes None and gets the real ``claude -p`` invocation.
    """
    cmd = claude_cmd or [
        "claude", "-p",
        "--dangerously-skip-permissions",
        "--output-format", "stream-json",
        "--verbose",
    ]
    t_start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
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

    transcript_fh = transcript_path.open("a", encoding="utf-8") if transcript_path else None
    last_text = ""
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if transcript_fh is not None and line.strip():
                # Mirror raw bytes (one event per line) so downstream replay
                # tooling sees the same wire format the orchestrator saw.
                transcript_fh.write(line + "\n")
                transcript_fh.flush()
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                stamp(f"{DIM}{line[:240]}{RESET}")
                continue
            text = _render_event(ev)
            if text is not None:
                # Tool results are visually attached to the preceding tool_use
                # (often multi-line file content, sometimes 20+ lines). A second
                # [HH:MM:SS] on the first line of that block reads as a separate
                # event, fights with the highlighting, and adds nothing — the
                # tool_use stamp already carries the time. Print bare so the
                # output sits cleanly under its command.
                if ev.get("type") == "user":
                    print(text, flush=True)
                else:
                    stamp(text)
            if ev.get("type") == "assistant":
                for c in ev.get("message", {}).get("content", []) or []:
                    if c.get("type") == "text":
                        last_text = c.get("text", "") or last_text
                    elif c.get("type") == "tool_use" and record is not None:
                        name = c.get("name") or ""
                        if name in _TRACKED_TOOLS:
                            record.tool_counts[name] = (
                                record.tool_counts.get(name, 0) + 1
                            )
            elif ev.get("type") == "result" and record is not None:
                record.cost_usd = float(ev.get("total_cost_usd", 0) or 0)
                record.num_turns = int(ev.get("num_turns", 0) or 0)
        proc.wait()
    finally:
        if transcript_fh is not None:
            transcript_fh.close()
    if record is not None:
        record.wall_s = time.monotonic() - t_start
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


# Grep results carry a line number and snippet per match. Two shapes:
#   <line>:<content>              (Grep on a single file)
#   <path>:<line>:<content>       (Grep on a directory; ripgrep prepends path)
# Context lines from -A/-B use `-` instead of `:` as the separator.
_GREP_PATH_LINE_RE = re.compile(r"^([^:]+):(\d+)([-:])(.*)$")
_GREP_LINE_RE = re.compile(r"^(\d+)([-:])(.*)$")


def _highlight_grep_snippet(snippet: str, hint_path: str) -> str:
    """Highlight a single grep snippet using a lexer guessed from ``hint_path``.
    Returns the snippet unchanged if pygments isn't available or can't pick a
    lexer — better than rendering broken color codes."""
    try:
        import pygments
        from pygments.formatters import TerminalFormatter
        from pygments.lexers import get_lexer_for_filename
        from pygments.util import ClassNotFound
    except ImportError:
        return snippet
    try:
        lexer = get_lexer_for_filename(hint_path)
    except ClassNotFound:
        return snippet
    # pygments emits a trailing newline we don't want here.
    return pygments.highlight(snippet, lexer, TerminalFormatter()).rstrip("\n")


def _render_grep_result(default_path: str, body: str) -> str:
    """Walk a grep result body line-by-line, dimming the path/lineno prefix
    and syntax-highlighting the snippet. Lines that don't match either grep
    shape pass through untouched (e.g. ripgrep's blank separator between
    files, or summary lines)."""
    out: list[str] = []
    for line in body.splitlines():
        m = _GREP_PATH_LINE_RE.match(line)
        if m:
            path, lineno, sep, snippet = m.groups()
            highlighted = _highlight_grep_snippet(snippet, path)
            out.append(f"{DIM}{path}:{lineno}{sep}{RESET}{highlighted}")
            continue
        m = _GREP_LINE_RE.match(line)
        if m:
            lineno, sep, snippet = m.groups()
            highlighted = _highlight_grep_snippet(snippet, default_path) if default_path else snippet
            out.append(f"{DIM}{lineno}{sep}{RESET}{highlighted}")
            continue
        out.append(line)
    return "\n".join(out)


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
                name = c.get("name") or "?"
                inp = c.get("input") or {}
                # Stash by id so the matching tool_result event can look up
                # what was called (used by the Read-result syntax highlighter).
                tu_id = c.get("id")
                if tu_id:
                    _tool_uses_by_id[tu_id] = {"name": name, "input": inp}
                out.append(_render_tool_use(name, inp))
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

                # If this result corresponds to a Read or Grep tool_use,
                # route the body through the matching syntax-highlighting
                # helper. Whole-file caching covers both (Grep snippets
                # share the cache with Read via the path argument).
                if not is_err:
                    use_id = c.get("tool_use_id")
                    original = _tool_uses_by_id.get(use_id) if use_id else None
                    if original and original.get("name") == "Read":
                        inp = original.get("input") or {}
                        rendered = _render_read_result(
                            str(inp.get("file_path", "")),
                            inp.get("offset"),
                            inp.get("limit"),
                        )
                        if rendered is not None:
                            body = rendered
                    elif original and original.get("name") == "Grep":
                        inp = original.get("input") or {}
                        body = _render_grep_result(str(inp.get("path", "")), body)

                if not body.strip():
                    if is_err:
                        out.append(f"  {RED}→ ERROR{RESET} {DIM}(no output){RESET}")
                    else:
                        out.append(f"  {DIM}(no output){RESET}")
                    continue
                capped = _cap_lines(body, 20, indent="  ")
                # Errors get a "→ ERROR" badge on the first line because they
                # need to be visible at a glance. Successful results sit
                # cleanly under the tool_use line with just the indent —
                # adding a "→" marker fights with the highlighted content
                # underneath, which the user explicitly didn't want.
                if is_err and capped.startswith("  "):
                    first_line, nl, rest = capped[2:].partition("\n")
                    head = f"  {RED}→ ERROR{RESET} {first_line}"
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
    *, iteration: int, set_code: str, gap_kind: str, label: str, card: str,
    summary: str, push: bool = True,
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
    # `git_dirty()` returns True for untracked files too (e.g. experiments/runs/
    # from --record), but the stages above only pick up tracked changes and
    # src/ additions. If the agent's "fix" was actually a no-op (stale-cache
    # diagnosis, doc-only change outside src/, etc.), nothing is staged and
    # `git commit` would crash with "nothing to commit". Detect that here so
    # the loop continues to the next iteration instead of dying — and the
    # caller's invalidate_label still runs, dropping the stale entry.
    diff_check = subprocess.run(
        ["git", "diff", "--cached", "--quiet"],
        cwd=REPO, capture_output=True, text=True,
    )
    if diff_check.returncode == 0:
        stamp(
            f"{YELLOW}no staged changes after agent run (likely stale-cache "
            f"diagnosis); skipping commit{RESET}"
        )
        return
    subject = _commit_subject(gap_kind, label, card)
    body = summary.strip() or f"set={set_code}, iteration {iteration}"
    body = "\n".join(body.splitlines()[:20])  # cap for sanity
    msg = f"{subject}\n\n{body}\n"
    subprocess.run(["git", "commit", "-m", msg], cwd=REPO, check=True)
    if push:
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
    set_code: str, project_dir: Path,
    *,
    scan_jsonl_path: Path | None = None,
    skip_cards: set[str] | None = None,
    only_cards: set[str] | None = None,
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

    When ``scan_jsonl_path`` is set, every NDJSON line emitted by the worker
    is mirrored verbatim into that file — separate from the agent transcript
    so the scan timeline can be reconstructed independently.

    ``skip_cards`` is piped to the worker as ``{"skip_cards": [...]}`` on
    stdin so the next scan surfaces a different card — the capture-batch
    hook. ``only_cards`` is the symmetric --card-A/B hook (restrict the scan
    to listed names). Both can be set; their composition is intersection
    (skip wins over only). None / empty means "no filter".
    """
    cmd = [
        "uv", "run", "python", "-m", "argentum_press._fix_loop_gap",
        set_code, str(project_dir),
    ]
    payload: dict[str, Any] = {}
    if skip_cards:
        payload["skip_cards"] = sorted(skip_cards)
    if only_cards is not None:
        payload["only_cards"] = sorted(only_cards)
    stdin_payload = json.dumps(payload) if payload else ""
    proc = subprocess.Popen(
        cmd,
        cwd=REPO,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    proc.stdin.write(stdin_payload)
    proc.stdin.close()

    scan_fh = scan_jsonl_path.open("a", encoding="utf-8") if scan_jsonl_path else None
    result_event: dict[str, Any] | None = None
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n").strip()
            if not line:
                continue
            if scan_fh is not None:
                scan_fh.write(line + "\n")
                scan_fh.flush()
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
    finally:
        if scan_fh is not None:
            scan_fh.close()

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


# ---------------------------------------------------------------------------
# captured-gap library (Phase 1: --capture-gap / --replay)
# ---------------------------------------------------------------------------

# Captured gaps live here so the experiment runner can iterate over the
# library without hard-coding paths. ``experiments/gaps/<slug>.json`` is the
# canonical layout; ``experiments/runs/<tag>/`` holds --record output. Tests
# override via ARGENTUM_GAPS_DIR so the unit suite doesn't pollute the
# real library.
def _gaps_dir() -> Path:
    override = os.environ.get("ARGENTUM_GAPS_DIR")
    return Path(override) if override else REPO / "experiments" / "gaps"


# Read at module import time for backwards-compat in places that import the
# constant directly. Tests that need to redirect call sites should both set
# the env var AND use ``_gaps_dir()`` rather than this attribute.
GAPS_DIR = _gaps_dir()


def _auto_slug(gap: Any) -> str:
    """Derive a filename-safe slug from ``gap.label``.

    Lower-kind labels are dotted classpaths (e.g.
    ``argentum_press.parser.ast.expressions.ChooseExpression``); we keep the
    final component and prefix with ``lower-``. Other kinds keep the full
    label (``parse-error:<EOF>@t...``, ``unmodeled-rule:abilityword``).
    The result is lowercased and non-``[a-z0-9-]`` runs collapse to a single
    dash so the slug is safe as a path component.
    """
    if gap.kind == "lower":
        bare = gap.label.split(".")[-1]
        raw = f"lower-{bare}"
    else:
        raw = gap.label
    slug = re.sub(r"[^a-z0-9-]+", "-", raw.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "gap"


def _unique_slug(base: str, gaps_dir: Path) -> str:
    """Return ``base`` if ``<base>.json`` is free, else the first
    ``<base>-N.json`` (N starting at 2) that isn't taken."""
    if not (gaps_dir / f"{base}.json").exists():
        return base
    n = 2
    while (gaps_dir / f"{base}-{n}.json").exists():
        n += 1
    return f"{base}-{n}"


def _save_captured_gap(
    *,
    set_code: str,
    gap: Any,
    ast_text: str | None,
    pe_block: str | None,
    slug: str,
    gaps_dir: Path,
) -> Path:
    """Persist a captured-gap JSON payload under ``gaps_dir/<slug>.json`` and
    return the path. Shared by ``--capture-gap`` (single, user-named slug)
    and ``--capture-batch`` (auto-named per-iteration slugs)."""
    gaps_dir.mkdir(parents=True, exist_ok=True)
    out = gaps_dir / f"{slug}.json"
    payload: dict[str, Any] = {
        "set_code": set_code,
        "gap": {
            "kind": gap.kind,
            "label": gap.label,
            "card_name": gap.card_name,
            "oracle_text": gap.oracle_text,
        },
        "ast_text": ast_text,
        "parse_error_block": pe_block,
        "ref_commit": git("rev-parse", "HEAD", check=False) or "",
    }
    out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return out


def _capture_gap(set_code: str, project_dir: Path, slug: str) -> int:
    """Run the gap finder once and persist the result under
    ``experiments/gaps/<slug>.json``.

    No claude call, no commit. The captured JSON carries everything the
    replay path needs to reconstruct the same iteration input: gap kind +
    label, card name + oracle text, optional AST text + parse-error block,
    and the commit HEAD was at when the capture ran (so replay can refuse to
    run against a drifted parser state).
    """
    print(f"{BOLD}=== capture-gap {slug} ==={RESET}", flush=True)
    try:
        gap, ast_text, pe_block = _find_gap_subprocess(set_code, project_dir)
    except GapSubprocessError as e:
        stamp(f"{RED}{e}{RESET}")
        return 2
    if gap is None:
        stamp(f"{YELLOW}no gap found; nothing to capture.{RESET}")
        return 1

    out = _save_captured_gap(
        set_code=set_code, gap=gap, ast_text=ast_text, pe_block=pe_block,
        slug=slug, gaps_dir=_gaps_dir(),
    )
    stamp(f"{GREEN}captured gap '{slug}' → {out.relative_to(REPO)}{RESET}")
    stamp(f"  kind={gap.kind}  card={gap.card_name}  label={gap.label.splitlines()[0]}")
    return 0


def _capture_batch(set_code: str, project_dir: Path, n: int) -> int:
    """Capture ``n`` distinct gaps in one command.

    Each iteration scans the set with the previously-captured card names
    blacklisted, picks the auto-slug from the gap label, and writes the
    JSON payload via :func:`_save_captured_gap`. Stops early when the set
    is exhausted (every remaining card parses + lowers, or every gap has
    already been captured)."""
    print(f"{BOLD}=== capture-batch n={n} ==={RESET}", flush=True)
    gaps_dir = _gaps_dir()
    skip: set[str] = set()
    captured: list[str] = []
    for i in range(1, n + 1):
        print(f"\n{BOLD}--- batch {i}/{n} ---{RESET}", flush=True)
        try:
            gap, ast_text, pe_block = _find_gap_subprocess(
                set_code, project_dir, skip_cards=skip,
            )
        except GapSubprocessError as e:
            stamp(f"{RED}{e}{RESET}")
            return 2
        if gap is None:
            stamp(f"{YELLOW}set clean after {len(captured)} capture(s); stopping.{RESET}")
            break
        base = _auto_slug(gap)
        slug = _unique_slug(base, gaps_dir)
        out = _save_captured_gap(
            set_code=set_code, gap=gap, ast_text=ast_text, pe_block=pe_block,
            slug=slug, gaps_dir=gaps_dir,
        )
        stamp(f"{GREEN}captured '{slug}' → {out.relative_to(REPO)}{RESET}")
        stamp(f"  kind={gap.kind}  card={gap.card_name}  label={gap.label.splitlines()[0]}")
        captured.append(slug)
        skip.add(gap.card_name)
    print(f"\n{BOLD}captured {len(captured)} gap(s):{RESET}", flush=True)
    for slug in captured:
        print(f"  - {slug}")
    return 0 if captured else 1


def _load_gap(slug: str) -> dict[str, Any]:
    path = _gaps_dir() / f"{slug}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no captured gap at {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _snapshot_worktree() -> tuple[str, str]:
    """Capture HEAD + the current ``git status --porcelain`` so the replay
    cleanup can detect any working-tree change the agent introduced and
    restore the pre-replay state.

    Returns ``(head_sha, porcelain_text)``. Replay restores by hard-resetting
    to HEAD and dropping any new untracked files the agent created.
    """
    head = git("rev-parse", "HEAD")
    porcelain = git("status", "--porcelain")
    return head, porcelain


def _restore_worktree(head: str) -> None:
    """Hard-reset to ``head`` and wipe untracked files.

    Replay is supposed to leave no trace; the agent may have edited tracked
    files (caught by ``reset --hard``) and/or created new ones (caught by
    ``clean -fd``). We don't touch the parse cache or anything outside the
    repo. The user accepted this explicitly: replay is destructive on
    purpose, and only runs on the dedicated experiment branch.

    Honors ``ARGENTUM_FIX_LOOP_NO_RESTORE=1`` as a kill-switch for tests:
    the integration test invokes a real subprocess where monkeypatching
    can't reach, and we explicitly don't want it nuking work-in-progress
    code on a dev checkout.
    """
    if os.environ.get("ARGENTUM_FIX_LOOP_NO_RESTORE") == "1":
        return
    git("reset", "--hard", head, check=False)
    git("clean", "-fd", check=False)


def _run_replay(
    *,
    slug: str,
    recorder: Recorder,
    description: str,
    prompt_variant: str,
    dry_run: bool,
    claude_cmd: list[str] | None,
    skip_pytest: bool = False,
) -> int:
    """Run one fix-loop iteration against a saved gap and restore worktree.

    The replay path deliberately skips: (a) the gap subprocess (we use the
    saved input verbatim), (b) the commit (replay is throwaway), and (c) the
    parse-cache invalidation (any cache state we touched gets blown away by
    the worktree restore anyway). Pytest still runs as a verification gate
    so a replay row in runs.tsv has a real outcome.
    """
    print(f"\n{BOLD}=== replay {slug} ==={RESET}", flush=True)
    try:
        payload = _load_gap(slug)
    except FileNotFoundError as e:
        print(f"{RED}{e}{RESET}", file=sys.stderr)
        return 2

    head_now = git("rev-parse", "HEAD")
    ref_commit = payload.get("ref_commit") or ""
    if ref_commit and ref_commit != head_now:
        print(
            f"{RED}replay aborted: gap '{slug}' was captured against "
            f"{ref_commit[:12]}, but HEAD is {head_now[:12]}.{RESET}\n"
            f"  Check out the capture commit before replaying.",
            file=sys.stderr,
        )
        return 2

    from argentum_press.diagnose import Gap

    gap_data = payload["gap"]
    gap = Gap(
        kind=gap_data["kind"],
        label=gap_data["label"],
        card_name=gap_data["card_name"],
        oracle_text=gap_data["oracle_text"],
        parse_details=None,
    )
    set_code = payload.get("set_code", "")
    project_dir = Path(payload.get("project_dir", REPO))  # purely for engine hints
    ast_text = payload.get("ast_text")
    pe_block = payload.get("parse_error_block")

    ctx = gather_context(
        set_code=set_code,
        project_dir=project_dir,
        kind=gap.kind,
        label=gap.label,
        card_name=gap.card_name,
        oracle_text=gap.oracle_text,
        ast_text=ast_text,
        parse_error_block=pe_block,
    )

    prompt = _render_prompt_variant(prompt_variant, ctx)
    if dry_run:
        print(prompt)
        return 0

    rec = recorder.start_iteration(1)
    rec.gap_kind = gap.kind
    rec.gap_label = gap.label
    rec.card_name = gap.card_name
    transcript_path = recorder.transcript_jsonl_path(rec, _gap_slug(gap.label))
    # description carries the variant tag so a runs.tsv row is enough to know
    # which prompt produced it without grep'ing back through the script flags.
    rec.description = f"{description}|variant={prompt_variant}|replay={slug}" if description \
        else f"variant={prompt_variant}|replay={slug}"

    stamp(f"{YELLOW}gap{RESET} kind={gap.kind}  card={gap.card_name}  "
          f"label={gap.label.splitlines()[0]}")

    snap_head, _ = _snapshot_worktree()
    try:
        rc, _ = stream_claude(
            prompt,
            transcript_path=transcript_path,
            record=rec,
            claude_cmd=claude_cmd,
        )
        if rc != 0:
            stamp(f"{RED}claude exited {rc}; recording as claude_error.{RESET}")
            rec.outcome = "claude_error"
            recorder.finish_iteration(rec)
            return rc

        if skip_pytest:
            stamp(f"{DIM}skipping pytest (--skip-pytest).{RESET}")
            rec.outcome = "pass"
            recorder.finish_iteration(rec)
        else:
            stamp(f"{DIM}running pytest...{RESET}")
            pytest_rc, output = run_pytest()
            if pytest_rc != 0:
                stamp(f"{RED}pytest red after replay edit; recording abort_pytest.{RESET}")
                print(output[-2000:], file=sys.stderr)
                rec.outcome = "abort_pytest"
                recorder.finish_iteration(rec)
                return 0  # replay aborted-pytest is informational, not fatal
            stamp(f"{GREEN}pytest green.{RESET}")
            rec.outcome = "pass"
            recorder.finish_iteration(rec)
    finally:
        # Restore the worktree no matter what; replay is supposed to be
        # idempotent. Important: do this AFTER finish_iteration so the
        # rec.commit_after column reflects the (unchanged) HEAD before
        # we reset.
        _restore_worktree(snap_head)
        stamp(f"{DIM}worktree restored to {snap_head[:12]}{RESET}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("set_code", nargs="?", default="",
                    help="Scryfall set code. Ignored when --replay is set.")
    ap.add_argument("project_dir", type=Path, nargs="?", default=REPO,
                    help="Path to argentum-engine. Ignored when --replay is set.")
    ap.add_argument("--max-iter", type=int, default=0, help="0 = unbounded")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-commit", action="store_true")
    ap.add_argument(
        "--no-push", action="store_true",
        help="Don't try to push iteration commits to the remote. Use for "
             "ephemeral worktrees (the A/B race) where the branch has no "
             "upstream and pushing isn't meaningful.",
    )
    ap.add_argument("--allow-dirty", action="store_true")
    ap.add_argument(
        "--record", type=Path, default=None,
        help="Directory to stream per-iteration NDJSON transcripts + runs.tsv into.",
    )
    ap.add_argument(
        "--replay", type=str, default=None,
        help="Slug of a saved gap under experiments/gaps/<slug>.json to replay "
             "instead of running the live gap finder. Requires --record. "
             "Restores the working tree after the iteration; does not commit.",
    )
    ap.add_argument(
        "--capture-gap", dest="capture_gap", type=str, default=None,
        help="Run the gap finder once, save the gap to experiments/gaps/<slug>.json, "
             "and exit without invoking claude.",
    )
    ap.add_argument(
        "--capture-batch", dest="capture_batch", type=int, default=None,
        help="Capture N distinct gaps in one command, auto-naming each. "
             "Mutually exclusive with --capture-gap.",
    )
    ap.add_argument(
        "--prompt-variant", type=str, default="baseline",
        help="Name under prompts/ to use for the agent prompt (default: baseline).",
    )
    ap.add_argument(
        "--description", type=str, default="",
        help="Free-text label propagated into the runs.tsv description column.",
    )
    ap.add_argument(
        "--claude-cmd", type=str, default=None,
        help="JSON-encoded list of argv strings overriding the default ``claude -p`` "
             "invocation. Tests use this to point at a fake-claude shim.",
    )
    ap.add_argument(
        "--skip-pytest", action="store_true",
        help="Skip the post-agent pytest gate. Replay mode + experiment runner "
             "use this to keep wall-clock noise down when the agent is a no-op "
             "shim; production runs leave it off.",
    )
    ap.add_argument(
        "--card", type=str, default=None,
        help="Restrict the scan to a single card by name (the exact Scryfall "
             "card name, e.g. 'Spider-UK'). Lets you A/B different fix paths "
             "against the same gap on two branches.",
    )
    ap.add_argument(
        "--mode", choices=("freeform", "playbook"), default="freeform",
        help="freeform = freeform claude -p loop (default, unchanged). "
             "playbook = structured DAG of LLM + libcst steps for all three "
             "gap kinds (parse-error / unmodeled-rule / lower); freeform is "
             "the last-resort fallback for unknown kinds.",
    )
    args = ap.parse_args(argv)

    if args.capture_gap is not None and args.capture_batch is not None:
        print(
            f"{RED}--capture-gap and --capture-batch are mutually exclusive.{RESET}",
            file=sys.stderr,
        )
        return 2

    if args.capture_gap is not None:
        if not args.set_code:
            print(f"{RED}--capture-gap requires set_code positional.{RESET}", file=sys.stderr)
            return 2
        return _capture_gap(args.set_code, args.project_dir, args.capture_gap)

    if args.capture_batch is not None:
        if not args.set_code:
            print(f"{RED}--capture-batch requires set_code positional.{RESET}", file=sys.stderr)
            return 2
        if args.capture_batch < 1:
            print(f"{RED}--capture-batch N must be >= 1.{RESET}", file=sys.stderr)
            return 2
        return _capture_batch(args.set_code, args.project_dir, args.capture_batch)

    if args.replay is not None and args.record is None:
        print(
            f"{RED}--replay requires --record (replay only makes sense with measurement).{RESET}",
            file=sys.stderr,
        )
        return 2

    if not args.allow_dirty and git_dirty():
        print(
            f"{RED}working tree is dirty; commit or stash before running.{RESET}\n"
            f"  (pass --allow-dirty to override)",
            file=sys.stderr,
        )
        return 2

    claude_cmd: list[str] | None = None
    if args.claude_cmd:
        claude_cmd = json.loads(args.claude_cmd)

    recorder = Recorder(args.record) if args.record else None

    def _desc() -> str:
        # Prepend mode + variant tags to the user-supplied description so a
        # runs.tsv row's description column tells the full story (a tag
        # like 'mode=playbook|variant=h1-no-handler-map|seed-42' is
        # greppable; the bare user description without it isn't).
        head = f"mode={args.mode}|variant={args.prompt_variant}"
        if args.card:
            head += f"|card={args.card}"
        if args.description:
            return f"{head}|{args.description}"
        return head

    if args.replay is not None:
        assert recorder is not None
        return _run_replay(
            slug=args.replay,
            recorder=recorder,
            description=args.description,
            prompt_variant=args.prompt_variant,
            dry_run=args.dry_run,
            claude_cmd=claude_cmd,
            skip_pytest=args.skip_pytest,
        )

    if not args.set_code:
        print(f"{RED}set_code positional is required.{RESET}", file=sys.stderr)
        return 2

    # ---- pick strategy ----------------------------------------------------
    # Composition: --mode playbook wraps the freeform fixer as its non-lower
    # fallback. So --mode playbook means "playbook for lower, freeform for
    # everything else" — the realistic deployment shape and the fair A/B
    # comparison against pure freeform.
    freeform = FreeformFixer(
        stream_claude=stream_claude,
        render_prompt=_render_prompt_variant,
        run_pytest=run_pytest,
        claude_cmd=claude_cmd,
        prompt_variant=args.prompt_variant,
        say=lambda msg: stamp(f"{DIM}{msg}{RESET}"),
    )
    if args.mode == "playbook":
        from argentum_press.playbook import (
            lower as playbook_lower,
            parse_error as playbook_parse_error,
            unmodeled_rule as playbook_unmodeled_rule,
        )
        # Composition: parse-error -> unmodeled-rule -> lower -> freeform.
        # Every gap kind routes to its dedicated playbook; freeform is the
        # last-resort fallback when a gap doesn't match any kind (shouldn't
        # happen but the chain is structural, not exhaustive).
        say = lambda msg: stamp(f"{DIM}{msg}{RESET}")
        lower_fixer = LowerPlaybookFixer(
            run_lower=playbook_lower.run,
            fallback=freeform,
            say=say,
        )
        unmodeled_fixer = UnmodeledRulePlaybookFixer(
            run_unmodeled_rule=playbook_unmodeled_rule.run,
            fallback=lower_fixer,
            say=say,
        )
        strategy: GapFixer = ParseErrorPlaybookFixer(
            run_parse_error=playbook_parse_error.run,
            fallback=unmodeled_fixer,
            say=say,
        )
    else:
        strategy = freeform

    prev_label = ""
    i = 0
    while True:
        i += 1
        if args.max_iter and i > args.max_iter:
            stamp(f"{YELLOW}reached --max-iter={args.max_iter}{RESET}")
            return 0
        print(f"\n{BOLD}=== iteration {i} ==={RESET}", flush=True)

        rec = recorder.start_iteration(i) if recorder else None
        scan_path = recorder.scan_jsonl_path(rec) if (recorder and rec) else None

        only_cards = {args.card} if args.card else None
        try:
            gap, ast_text, pe_block = _find_gap_subprocess(
                args.set_code, args.project_dir,
                scan_jsonl_path=scan_path,
                only_cards=only_cards,
            )
        except GapSubprocessError as e:
            stamp(f"{RED}{e}{RESET}")
            if recorder and rec:
                rec.outcome = "abort_subprocess"
                rec.description = _desc()
                recorder.finish_iteration(rec)
            return 2
        if gap is None:
            stamp(f"{GREEN}no gaps remaining. done.{RESET}")
            return 0
        if gap.label == prev_label:
            stamp(f"{RED}no progress: label '{gap.label}' twice in a row. abort.{RESET}")
            if recorder and rec:
                rec.gap_kind = gap.kind
                rec.gap_label = gap.label
                rec.card_name = gap.card_name
                rec.outcome = "abort_no_progress"
                rec.description = _desc()
                recorder.finish_iteration(rec)
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

        if rec is not None and recorder is not None:
            rec.gap_kind = gap.kind
            rec.gap_label = gap.label
            rec.card_name = gap.card_name

        transcript_path = (
            recorder.transcript_jsonl_path(rec, _gap_slug(gap.label))
            if (recorder is not None and rec is not None) else None
        )
        iter_ctx = IterationContext(
            set_code=args.set_code,
            project_dir=args.project_dir,
            gap_ctx=ctx,
            ast_text=ast_text,
            pe_block=pe_block,
            recorder=recorder,
            rec=rec,
            transcript_path=transcript_path,
            dry_run=args.dry_run,
        )
        outcome = strategy.fix(gap, iter_ctx)
        if outcome.outcome_tag == "dry_run":
            return 0
        if outcome.rc != 0:
            # The strategy's outcome_tag is how it asks the orchestrator to
            # label this iteration in runs.tsv ("claude_error", "abort_pytest",
            # "playbook_aborted-l3", etc.). The strategy itself doesn't touch
            # the recorder.
            stamp(f"{RED}strategy aborted (rc={outcome.rc}, tag={outcome.outcome_tag}){RESET}")
            if outcome.summary:
                print(outcome.summary[-2000:], file=sys.stderr)
            if recorder and rec:
                rec.outcome = outcome.outcome_tag
                rec.description = _desc()
                recorder.finish_iteration(rec)
            return outcome.rc
        stamp(f"{GREEN}{outcome.outcome_tag}.{RESET}")
        summary = outcome.summary

        if not args.no_commit:
            commit_iteration(
                iteration=i,
                set_code=args.set_code,
                gap_kind=gap.kind,
                label=gap.label,
                card=gap.card_name,
                summary=summary,
                push=not args.no_push,
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

        if recorder and rec:
            rec.outcome = "pass"
            rec.description = _desc()
            recorder.finish_iteration(rec)

        prev_label = gap.label


if __name__ == "__main__":
    raise SystemExit(main())
