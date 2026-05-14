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
        say: Callable[[str], None] | None = None,
    ) -> None:
        self._run_lower = run_lower
        self._fallback = fallback
        self._say = say or (lambda _msg: None)

    def fix(self, gap: Any, ctx: IterationContext) -> FixOutcome:
        if gap.kind != "lower":
            self._say(f"playbook: kind={gap.kind} not supported, falling back to freeform")
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
        )
        wall_s = time.monotonic() - t_start
        self._say(f"playbook outcome={result.outcome}  wall_s={wall_s:.2f}")

        if ctx.rec is not None:
            # Playbook keeps richer per-step timing in its own trace JSON;
            # the runs.tsv only carries the aggregate. Surface wall_s + zero
            # the tool_counts (the playbook is structured, not freeform).
            ctx.rec.wall_s = wall_s
            ctx.rec.tool_counts = {}

        if not result.outcome.startswith("applied"):
            return FixOutcome(
                rc=2,
                summary=f"playbook outcome={result.outcome}",
                outcome_tag=f"playbook_{result.outcome}",
            )

        import json
        summary = json.dumps(result.final_plan, indent=2) if result.final_plan else ""
        return FixOutcome(rc=0, summary=summary, outcome_tag="pass")


__all__ = [
    "FixOutcome",
    "FreeformFixer",
    "GapFixer",
    "IterationContext",
    "LowerPlaybookFixer",
]
