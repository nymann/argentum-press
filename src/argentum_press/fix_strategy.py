"""Pluggable fix-strategies for the per-iteration body of the fix-loop.

The orchestrator (``scripts/fix_parser_gaps.py``) finds a gap and runs pytest;
how the gap is *fixed* in between is a strategy choice:

- :class:`FreeformFixer` renders a prompt, hands it to ``claude -p``, then runs
  pytest. The historical default; still the only path that can handle every
  gap kind.
- :class:`LowerPlaybookFixer` dispatches lower-kind gaps to
  :mod:`argentum_press.playbook.lower` (structured tool-use + libcst), and
  falls back to an inner fixer (typically the freeform one) for everything
  else.

A strategy is picked once at startup based on ``--mode`` and called as
``strategy.fix(gap, ctx)`` in the loop. Adding a new playbook (parse-error,
unmodeled-rule) is just another :class:`GapFixer` subclass.

Strategies receive helpers (``stream_claude``, ``render_prompt``,
``run_pytest``, ``playbook.lower.run``) as constructor arguments rather than
importing them; the script wires the dependencies, the strategies stay
trivially mockable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(slots=True)
class FixOutcome:
    """What a strategy returns to the orchestrator.

    ``rc`` is 0 on success (orchestrator commits + continues), non-zero on
    abort (orchestrator records the outcome and returns). ``summary`` becomes
    the per-iteration commit body. ``outcome_tag`` is the runs.tsv outcome
    column; "pass" on success, a strategy-specific string on abort
    ("claude_error", "abort_pytest", "playbook_aborted-l3", ...).
    """

    rc: int
    summary: str = ""
    outcome_tag: str = "pass"


@dataclass(slots=True)
class IterationContext:
    """Per-iteration state passed to a strategy.

    The orchestrator owns the gap, the recorder, the dry-run flag, and the
    transcript path; strategies read from this struct rather than receiving
    a long argument list.
    """

    set_code: str
    project_dir: Path
    gap_ctx: Any
    """The GapContext dataclass from scripts/fix_parser_gaps.py. Typed Any so
    this module doesn't need to import from a script."""

    ast_text: str | None
    pe_block: str | None
    recorder: Any  # Recorder | None
    rec: Any       # IterationRecord | None
    transcript_path: Path | None
    dry_run: bool


class GapFixer(ABC):
    """Strategy interface."""

    @abstractmethod
    def fix(self, gap: Any, ctx: IterationContext) -> FixOutcome:
        ...


class FreeformFixer(GapFixer):
    """Renders a prompt, streams a ``claude -p`` agent, runs pytest."""

    def __init__(
        self,
        *,
        stream_claude: Callable[..., tuple[int, str]],
        render_prompt: Callable[[str, Any], str],
        run_pytest: Callable[[], tuple[int, str]],
        claude_cmd: list[str] | None,
        prompt_variant: str,
        say: Callable[[str], None] | None = None,
    ) -> None:
        self._stream_claude = stream_claude
        self._render_prompt = render_prompt
        self._run_pytest = run_pytest
        self._claude_cmd = claude_cmd
        self._prompt_variant = prompt_variant
        self._say = say or (lambda _msg: None)

    def fix(self, gap: Any, ctx: IterationContext) -> FixOutcome:
        prompt = self._render_prompt(self._prompt_variant, ctx.gap_ctx)
        if ctx.dry_run:
            print(prompt)
            return FixOutcome(rc=0, outcome_tag="dry_run")

        rc, summary = self._stream_claude(
            prompt,
            transcript_path=ctx.transcript_path,
            record=ctx.rec,
            claude_cmd=self._claude_cmd,
        )
        if rc != 0:
            return FixOutcome(rc=rc, summary="", outcome_tag="claude_error")

        self._say("running pytest...")
        pytest_rc, output = self._run_pytest()
        if pytest_rc != 0:
            return FixOutcome(
                rc=pytest_rc,
                summary=output[-2000:],
                outcome_tag="abort_pytest",
            )
        return FixOutcome(rc=0, summary=summary, outcome_tag="pass")


def _dump_trace(result: Any, ctx: IterationContext, label: str, say: Callable[[str], None]) -> None:
    """On playbook abort, drop a trace JSON next to the recorder and print
    the failing step's error to stderr.

    Shared helper for every playbook fixer. The standalone ``run_playbook_*.py``
    CLIs accept ``--trace-out``; when a playbook runs as a strategy inside
    the fix-loop there's no equivalent hook, and the in-memory steps
    evaporate. The trace JSON keeps the failure mode visible.
    """
    import json
    import sys
    last = result.steps[-1] if getattr(result, "steps", None) else None
    if last and isinstance(last.payload, dict) and "error" in last.payload:
        say(f"playbook {result.outcome} on {last.name}: {last.payload['error']}")

    from pathlib import Path
    slug = label.split(".")[-1].lower() or "abort"
    # Strip kind-prefix bits that contain colons/spaces so the filename is sane.
    slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in slug)
    ts = ""
    rec = ctx.rec
    if rec is not None and getattr(rec, "started_at", None):
        ts = f"{rec.started_at}-"
    out_root: Path | None = None
    recorder = ctx.recorder
    if recorder is not None and getattr(recorder, "record_dir", None):
        out_root = recorder.record_dir
    if out_root is None:
        out_root = Path.cwd() / "experiments" / "playbook-traces"
    out_root.mkdir(parents=True, exist_ok=True)
    out_path = out_root / f"{ts}{slug}-{result.outcome}.json"
    try:
        out_path.write_text(result.as_json(), encoding="utf-8")
        print(f"  playbook trace written to {out_path}", file=sys.stderr)
    except OSError as e:
        print(f"  failed to write playbook trace: {e}", file=sys.stderr)


def _wrap_playbook_outcome(
    result: Any,
    wall_s: float,
    ctx: IterationContext,
    label: str,
    say: Callable[[str], None],
) -> FixOutcome:
    """Convert a :class:`PlaybookResult` into the strategy's :class:`FixOutcome`.

    Common across the three playbook fixers: surface wall time on the
    recorder, zero out tool_counts (no claude turns), dump trace on abort,
    JSON-encode the final plan as the commit summary on success.
    """
    say(f"playbook outcome={result.outcome}  wall_s={wall_s:.2f}")
    if ctx.rec is not None:
        ctx.rec.wall_s = wall_s
        ctx.rec.tool_counts = {}

    if not result.outcome.startswith("applied"):
        _dump_trace(result, ctx, label, say)
        return FixOutcome(
            rc=2,
            summary=f"playbook outcome={result.outcome}",
            outcome_tag=f"playbook_{result.outcome}",
        )

    import json
    summary = json.dumps(result.final_plan, indent=2) if result.final_plan else ""
    return FixOutcome(rc=0, summary=summary, outcome_tag="pass")


class LowerPlaybookFixer(GapFixer):
    """Dispatches lower-kind gaps to the structured playbook; defers others.

    Composition rather than inheritance: holds a ``fallback`` strategy and
    delegates to it for every non-lower gap. The fallback is typically a
    :class:`FreeformFixer` so ``--mode playbook`` measures
    "playbook on lower, freeform on everything else" — the realistic
    deployment shape, and the only fair comparison against pure freeform.
    """

    def __init__(
        self,
        *,
        run_lower: Callable[..., Any],
        fallback: GapFixer,
        pool: Any = None,
        say: Callable[[str], None] | None = None,
    ) -> None:
        self._run_lower = run_lower
        self._fallback = fallback
        self._pool = pool
        self._say = say or (lambda _msg: None)

    def fix(self, gap: Any, ctx: IterationContext) -> FixOutcome:
        if gap.kind != "lower":
            self._say(f"lower-playbook: kind={gap.kind} not lower, falling back")
            return self._fallback.fix(gap, ctx)

        if ctx.dry_run:
            self._say(f"--dry-run with --mode playbook: would call playbook.lower.run for label={gap.label}")
            return FixOutcome(rc=0, outcome_tag="dry_run")

        import time
        t_start = time.monotonic()
        self._say("running playbook.lower...")
        result = self._run_lower(
            label=gap.label,
            project_dir=ctx.project_dir,
            card_name=gap.card_name,
            oracle_text=gap.oracle_text,
            ast_text=ctx.ast_text,
            verbose=True,
            pool=self._pool,
        )
        return _wrap_playbook_outcome(
            result, time.monotonic() - t_start, ctx, gap.label, self._say
        )


class ParseErrorPlaybookFixer(GapFixer):
    """Dispatches ``parse-error:`` gaps to the parse-error playbook.

    Every other gap is deferred to ``fallback``; in production we chain it
    in front of :class:`UnmodeledRulePlaybookFixer` (which in turn fronts
    :class:`LowerPlaybookFixer`, which fronts a :class:`FreeformFixer`).
    """

    def __init__(
        self,
        *,
        run_parse_error: Callable[..., Any],
        fallback: GapFixer,
        pool: Any = None,
        say: Callable[[str], None] | None = None,
    ) -> None:
        self._run_parse_error = run_parse_error
        self._fallback = fallback
        self._pool = pool
        self._say = say or (lambda _msg: None)

    def fix(self, gap: Any, ctx: IterationContext) -> FixOutcome:
        if gap.kind != "parse" or not gap.label.startswith("parse-error:"):
            return self._fallback.fix(gap, ctx)

        if ctx.dry_run:
            self._say(f"--dry-run: would call playbook.parse_error.run for label={gap.label}")
            return FixOutcome(rc=0, outcome_tag="dry_run")

        import time
        t_start = time.monotonic()
        self._say("running playbook.parse_error...")
        result = self._run_parse_error(
            label=gap.label,
            project_dir=ctx.project_dir,
            card_name=gap.card_name,
            oracle_text=gap.oracle_text,
            pe_block=ctx.pe_block,
            verbose=True,
            pool=self._pool,
        )
        return _wrap_playbook_outcome(
            result, time.monotonic() - t_start, ctx, gap.label, self._say
        )


class UnmodeledRulePlaybookFixer(GapFixer):
    """Dispatches ``unmodeled-rule:`` gaps to the unmodeled-rule playbook."""

    def __init__(
        self,
        *,
        run_unmodeled_rule: Callable[..., Any],
        fallback: GapFixer,
        pool: Any = None,
        say: Callable[[str], None] | None = None,
    ) -> None:
        self._run_unmodeled_rule = run_unmodeled_rule
        self._fallback = fallback
        self._pool = pool
        self._say = say or (lambda _msg: None)

    def fix(self, gap: Any, ctx: IterationContext) -> FixOutcome:
        if gap.kind != "parse" or not gap.label.startswith("unmodeled-rule:"):
            return self._fallback.fix(gap, ctx)

        if ctx.dry_run:
            self._say(f"--dry-run: would call playbook.unmodeled_rule.run for label={gap.label}")
            return FixOutcome(rc=0, outcome_tag="dry_run")

        import time
        t_start = time.monotonic()
        self._say("running playbook.unmodeled_rule...")
        result = self._run_unmodeled_rule(
            label=gap.label,
            project_dir=ctx.project_dir,
            card_name=gap.card_name,
            oracle_text=gap.oracle_text,
            verbose=True,
            pool=self._pool,
        )
        return _wrap_playbook_outcome(
            result, time.monotonic() - t_start, ctx, gap.label, self._say
        )


__all__ = [
    "FixOutcome",
    "FreeformFixer",
    "GapFixer",
    "IterationContext",
    "LowerPlaybookFixer",
    "ParseErrorPlaybookFixer",
    "UnmodeledRulePlaybookFixer",
]
