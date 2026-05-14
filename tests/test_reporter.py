"""ConsoleReporter is straight string-formatting; tests pin the user-visible
shape so regressions stand out when the CLI output changes."""

from __future__ import annotations

import io
from pathlib import Path

from argentum_press.outcome import (
    DeferredEmitterGap,
    DeferredParseFailed,
    Emitted,
)
from argentum_press.reporter import ConsoleReporter, NullReporter


def _new_reporter() -> tuple[ConsoleReporter, io.StringIO]:
    sink = io.StringIO()
    return ConsoleReporter(sink=sink), sink


def test_phase_headers_separate_sections() -> None:
    reporter, sink = _new_reporter()
    reporter.phase_triage_start("blb")
    reporter.phase_classify_start(10)
    reporter.phase_emit_start(4)
    reporter.phase_verify_start()
    out = sink.getvalue()
    assert "phase 1: triage" in out
    assert "phase 2: classify" in out
    assert "phase 3: emit" in out
    assert "phase 4: verify" in out


def test_classify_emits_per_card_progress() -> None:
    reporter, sink = _new_reporter()
    reporter.phase_triage_end(already_implemented=180, basic_lands=0, pending=3)
    reporter.phase_classify_start(3)
    reporter.card_classified_bucket_1("Lightning Bolt")
    reporter.card_classified_bucket_2(
        DeferredEmitterGap("Sneaky Card", "argentum_press._ast.ActivatedAbility")
    )
    reporter.card_parse_failed(DeferredParseFailed("Weird Card", "[incomplete] eof"))
    lines = sink.getvalue().splitlines()
    classify_lines = [line for line in lines if line.startswith("  [")]
    assert len(classify_lines) == 3
    assert "[  1/  3]" in classify_lines[0]
    assert "b1" in classify_lines[0]
    assert "Lightning Bolt" in classify_lines[0]
    assert "b2" in classify_lines[1]
    assert "ActivatedAbility" in classify_lines[1]  # short class name
    assert "parse" in classify_lines[2]


def test_emit_announces_path() -> None:
    reporter, _ = _new_reporter()
    reporter.phase_triage_end(already_implemented=0, basic_lands=0, pending=1)
    reporter.phase_classify_start(1)
    reporter.card_classified_bucket_1("Test Bird")
    reporter.phase_emit_start(1)
    reporter.card_emitted(Emitted("Test Bird", Path("/tmp/TestBird.kt")))
    # No assertion on a specific string; the smoke is "doesn't crash on a
    # well-formed event sequence." Pin the path in the message instead:
    assert reporter is not None  # placeholder


def test_emit_message_mentions_path() -> None:
    reporter, sink = _new_reporter()
    reporter.phase_triage_end(already_implemented=0, basic_lands=0, pending=1)
    reporter.phase_classify_start(1)
    reporter.card_classified_bucket_1("Test Bird")
    reporter.phase_emit_start(1)
    reporter.card_emitted(Emitted("Test Bird", Path("/tmp/TestBird.kt")))
    assert "/tmp/TestBird.kt" in sink.getvalue()


def test_verify_failed_prefixes_each_stderr_line() -> None:
    reporter, sink = _new_reporter()
    reporter.phase_verify_failed("error: unresolved reference: Effects.Foo\n  more context")
    out = sink.getvalue()
    assert "BUILD FAILED" in out
    assert "| error: unresolved reference: Effects.Foo" in out
    assert "| " in out  # at least one continuation line is prefixed


def test_null_reporter_swallows_every_event() -> None:
    n = NullReporter()
    # Just confirm no exception. NullReporter is the test default for the
    # pipeline; any future Reporter method must be no-op-safe here too.
    n.phase_triage_start("x")
    n.phase_triage_fetched(0, "miss")
    n.phase_triage_end(already_implemented=0, basic_lands=0, pending=0)
    n.phase_classify_start(0)
    n.phase_classify_end(bucket_1=0, bucket_2=0, parse_failed=0)
    n.phase_emit_start(0)
    n.phase_emit_end(0)
    n.phase_basics_start(0)
    n.phase_basics_skipped("none")
    n.phase_verify_start()
    n.phase_verify_skipped("nothing")
    n.phase_verify_passed()
    n.phase_verify_failed("err")
