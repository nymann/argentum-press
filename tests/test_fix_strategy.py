# pyright: basic
"""Tests for the per-iteration strategy chain.

The orchestrator (``scripts/fix_parser_gaps.py``) wires concrete fixers
together; these tests pin down the routing rules independently — what a
strategy passes through vs. delegates to its fallback, and which abort
outcomes trigger that delegation.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from argentum_press.fix_strategy import (
    FixOutcome,
    GapFixer,
    IterationContext,
    ParseErrorPlaybookFixer,
)


@dataclass
class _FakeGap:
    kind: str
    label: str
    card_name: str = ""
    oracle_text: str = ""


@dataclass
class _FakePlaybookResult:
    outcome: str
    final_plan: Any = None
    steps: list[Any] | None = None

    def as_json(self) -> str:
        return ""


class _RecordingFallback(GapFixer):
    """Tracks whether `fix` was called and what it returned."""

    def __init__(self, returns: FixOutcome) -> None:
        self.returns = returns
        self.calls: int = 0

    def fix(self, gap: Any, ctx: IterationContext) -> FixOutcome:
        self.calls += 1
        return self.returns


def _ctx() -> IterationContext:
    return IterationContext(
        set_code="x",
        project_dir=Path("/tmp"),
        gap_ctx=None,
        ast_text=None,
        pe_block=None,
        recorder=None,
        rec=None,
        transcript_path=None,
        dry_run=False,
    )


@pytest.mark.parametrize(
    "outcome",
    ["aborted-p4-duplicate", "aborted-p8-duplicate", "aborted-classify-unchanged"],
)
def test_parse_error_fixer_falls_back_on_definitive_abort(outcome: str):
    """Definitive aborts (dedupe + classify gate) escalate to freeform.

    The playbook has tried both directions and definitively can't reach
    this card with structured tool-use. Falling back lets a more flexible
    freeform agent take a swing instead of bailing the whole loop on
    rc=2.
    """
    def fake_run(**_kw):
        return _FakePlaybookResult(outcome=outcome)

    fallback = _RecordingFallback(FixOutcome(rc=0, outcome_tag="pass"))
    fixer = ParseErrorPlaybookFixer(
        run_parse_error=fake_run,
        fallback=fallback,
    )
    gap = _FakeGap(kind="parse", label="parse-error:<EOF>@t")
    result = fixer.fix(gap, _ctx())
    assert fallback.calls == 1, f"fallback should have been invoked for {outcome!r}"
    assert result.rc == 0
    assert result.outcome_tag == "pass"


@pytest.mark.parametrize(
    "outcome",
    ["aborted-p3", "aborted-p5", "aborted-retry-pytest"],
)
def test_parse_error_fixer_bubbles_other_aborts(outcome: str):
    """Infrastructure aborts (LLM error, libcst rejection, pytest red on
    retry) should bubble up so the user sees them — falling back here
    would mask real failures behind a freeform attempt that probably
    can't help."""
    def fake_run(**_kw):
        return _FakePlaybookResult(outcome=outcome)

    fallback = _RecordingFallback(FixOutcome(rc=0, outcome_tag="pass"))
    fixer = ParseErrorPlaybookFixer(
        run_parse_error=fake_run,
        fallback=fallback,
    )
    gap = _FakeGap(kind="parse", label="parse-error:<EOF>@t")
    result = fixer.fix(gap, _ctx())
    assert fallback.calls == 0, "fallback should NOT have been invoked"
    assert result.rc != 0
    assert result.outcome_tag == f"playbook_{outcome}"


def test_parse_error_fixer_passes_through_when_kind_mismatch():
    """A non-parse-error gap is delegated unconditionally — the playbook
    is single-kind, not exhaustive."""
    def fake_run(**_kw):
        raise AssertionError("playbook should not have been called")

    fallback = _RecordingFallback(FixOutcome(rc=0, outcome_tag="pass"))
    fixer = ParseErrorPlaybookFixer(
        run_parse_error=fake_run,
        fallback=fallback,
    )
    gap = _FakeGap(kind="lower", label="lower:Foo")
    fixer.fix(gap, _ctx())
    assert fallback.calls == 1


def test_parse_error_fixer_returns_applied_unchanged():
    """When the playbook says ``applied``, the wrapped outcome is rc=0 +
    outcome_tag=pass and the fallback is NOT consulted."""
    def fake_run(**_kw):
        return _FakePlaybookResult(outcome="applied", final_plan={"x": 1})

    fallback = _RecordingFallback(FixOutcome(rc=0, outcome_tag="pass"))
    fixer = ParseErrorPlaybookFixer(
        run_parse_error=fake_run,
        fallback=fallback,
    )
    gap = _FakeGap(kind="parse", label="parse-error:<EOF>@t")
    result = fixer.fix(gap, _ctx())
    assert fallback.calls == 0
    assert result.rc == 0
    assert result.outcome_tag == "pass"
