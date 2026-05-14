# pyright: basic
"""Tests for the parser-failure localizer.

The helper accepts an injected ``parse_fn`` so tests run against a
hand-built parser model in microseconds instead of paying the ~10s
real-grammar compile per test. Integration coverage against the real
parser lives in the playbook flow (failure-region rendering is
exercised end-to-end whenever a real parse-error gap is processed).
"""
from __future__ import annotations

from dataclasses import dataclass

from argentum_press.parser.failure_region import (
    FailureRegion,
    SentenceStatus,
    find_parse_failure_region,
)


@dataclass(frozen=True)
class _FakeError:
    message: str


@dataclass(frozen=True)
class _FakeResult:
    ok: bool
    error: _FakeError | None


def _ok() -> _FakeResult:
    return _FakeResult(ok=True, error=None)


def _parse_error(label: str = "parse-error:<EOF>@t") -> _FakeResult:
    return _FakeResult(ok=False, error=_FakeError(message=label))


def _unmodeled(rule: str = "putinzoneexpression") -> _FakeResult:
    return _FakeResult(
        ok=False, error=_FakeError(message=f"unmodeled-rule:{rule}"),
    )


def _make_parse_fn(table: dict[str, _FakeResult], *, default: _FakeResult) -> object:
    """Return a parse(text, name=...) that looks up exact-match strings.

    Unknown strings return ``default`` — this keeps the test minimal: the
    test states only the inputs it cares about and the fake fills in the
    rest. Trailing whitespace is stripped to match the helper's own
    behaviour.
    """
    def fake(text, *, name=""):
        return table.get(text.strip(), default)
    return fake


def test_empty_text_is_fully_parses():
    fr = find_parse_failure_region("", name="X", parse_fn=_make_parse_fn({}, default=_ok()))
    assert fr.fully_parses is True
    assert fr.sentences == ()
    assert fr.bisection_calls == 0


def test_fully_parsing_text_skips_per_sentence_walk():
    parse_fn = _make_parse_fn({"Flying": _ok()}, default=_ok())
    fr = find_parse_failure_region("Flying", name="X", parse_fn=parse_fn)
    assert fr.fully_parses is True
    assert fr.sentences == ()
    assert fr.bisection_calls == 1


def test_per_sentence_classification_separates_kinds():
    # Two-sentence text. Sentence 1 (a trigger) parses; sentence 2
    # (gibberish) fails with parse-error. The helper should classify
    # them accordingly and only word-scan the failing one.
    s1 = "When Foo enters, you draw a card."
    s2 = "Foo zzqqxx random gibberish."
    full = f"{s1} {s2}"
    parse_fn = _make_parse_fn(
        {
            full: _parse_error(),
            s1: _ok(),
            s2: _parse_error(),
        },
        default=_parse_error(),  # word-scan prefixes default to parse-error
    )
    fr = find_parse_failure_region(full, name="Foo", parse_fn=parse_fn)
    assert fr.fully_parses is False
    kinds = {s.sentence: s.kind for s in fr.sentences}
    assert kinds[s1] == "ok"
    assert kinds[s2] == "parse-error"


def test_per_sentence_classifies_unmodeled_as_not_failing():
    # A sentence that Lark accepts but the lowerer doesn't recognize
    # (LoweringIncomplete → "unmodeled-rule:X") is NOT a parser failure
    # and must not be reported as one.
    s1 = "Put the cards on the bottom of their library."
    s2 = "Foo zzqqxx mystery noise."
    full = f"{s1} {s2}"
    parse_fn = _make_parse_fn(
        {
            full: _parse_error(),
            s1: _unmodeled("putinzoneexpression"),
            s2: _parse_error(),
        },
        default=_parse_error(),
    )
    fr = find_parse_failure_region(full, name="Foo", parse_fn=parse_fn)
    assert fr.failing_sentences  # only s2
    assert all(s.sentence == s2 for s in fr.failing_sentences)
    s1_status = next(s for s in fr.sentences if s.sentence == s1)
    assert s1_status.kind == "unmodeled"
    assert s1_status.parse_error_label == "unmodeled-rule:putinzoneexpression"


def test_bisection_cost_is_bounded_at_sentence_count():
    # The helper used to do a word-by-word linear scan inside each
    # failing sentence — on Earley-slow parses this turned into minutes
    # of silent work for long cards. The trimmed version only does
    # `1 (full) + N (per-sentence)` parses. Verify that bound holds.
    s1 = "When Foo enters, you draw a card."
    s2 = "Foo zzqqxx random gibberish."
    s3 = "Bar zzqqxx more gibberish."
    full = f"{s1} {s2} {s3}"
    parse_fn = _make_parse_fn(
        {full: _parse_error(), s1: _ok(), s2: _parse_error(), s3: _parse_error()},
        default=_parse_error(),
    )
    fr = find_parse_failure_region(full, name="Foo", parse_fn=parse_fn)
    # 1 full + 3 per-sentence = 4 parses regardless of word count.
    assert fr.bisection_calls == 4
    assert len(fr.failing_sentences) == 2


def test_render_for_prompt_emits_readable_block():
    s1 = "When Foo enters, you draw a card."
    s2 = "Foo zzqqxx random gibberish."
    full = f"{s1} {s2}"
    parse_fn = _make_parse_fn(
        {
            full: _parse_error(),
            s1: _ok(),
            s2: _parse_error(),
        },
        default=_parse_error(),
    )
    fr = find_parse_failure_region(full, name="Foo", parse_fn=parse_fn)
    rendered = fr.render_for_prompt()
    assert "bisection_calls:" in rendered
    assert "per-sentence:" in rendered
    assert "PARSE-ERR" in rendered
    assert "OK" in rendered


def test_bisection_call_count_is_one_per_sentence_plus_full():
    # Without overall_label, the cost is exactly:
    # 1 (full text) + N (one per sentence). Independent of word count.
    s1 = "When Foo enters, you draw a card."
    s2 = "Foo zzqqxx random gibberish."
    full = f"{s1} {s2}"
    parse_fn = _make_parse_fn(
        {full: _parse_error(), s1: _ok(), s2: _parse_error()},
        default=_parse_error(),
    )
    fr = find_parse_failure_region(full, name="Foo", parse_fn=parse_fn)
    assert fr.bisection_calls == 1 + 2  # full + 2 sentences


# ---------------------------------------------------------------------------
# overall_label: skip the redundant full-text parse when caller already
# has the label from the gap classification.
# ---------------------------------------------------------------------------


class _CountingParseFn:
    """A parse_fn wrapper that records every (text, name) call.

    Lets tests verify which texts were parsed without coupling them to
    a specific in-process ordering.
    """

    def __init__(self, table: dict[str, _FakeResult], *, default: _FakeResult) -> None:
        self.table = table
        self.default = default
        self.calls: list[str] = []

    def __call__(self, text: str, *, name: str = "") -> _FakeResult:
        self.calls.append(text.strip())
        return self.table.get(text.strip(), self.default)


def test_overall_label_kwarg_skips_full_text_parse():
    s1 = "When Foo enters, draw a card."
    s2 = "Foo zzqqxx random gibberish."
    full = f"{s1} {s2}"
    parse_fn = _CountingParseFn(
        {s1: _ok(), s2: _parse_error()},
        default=_parse_error(),
    )
    fr = find_parse_failure_region(
        full, name="Foo",
        parse_fn=parse_fn,
        overall_label="parse-error:<EOF>@thash",
    )
    # The full-text string must NOT have been parsed — the caller
    # already told us the label.
    assert full not in parse_fn.calls
    # Sentence parses still happen, exactly two of them.
    assert parse_fn.calls == [s1, s2]
    # bisection_calls reflects the actual work done (per-sentence only).
    assert fr.bisection_calls == 2
    # The overall_label is preserved verbatim.
    assert fr.overall_label == "parse-error:<EOF>@thash"
    # And we still correctly identify the failing sentence.
    assert len(fr.failing_sentences) == 1
    assert fr.failing_sentences[0].sentence == s2


def test_overall_label_kwarg_with_empty_text_short_circuits():
    """Empty oracle text + overall_label: still treats as fully_parses,
    no per-sentence walk attempted. Defensive — caller might supply a
    label for a card with no text."""
    parse_fn = _CountingParseFn({}, default=_ok())
    fr = find_parse_failure_region(
        "", name="X",
        parse_fn=parse_fn,
        overall_label="parse-error:<EOF>@thash",
    )
    assert fr.fully_parses is True
    assert parse_fn.calls == []
    assert fr.bisection_calls == 0


def test_without_overall_label_still_parses_full_text():
    """Backward compatibility: existing callers don't pass
    overall_label and should see the previous 1+N behaviour."""
    s1 = "When Foo enters, draw a card."
    s2 = "Foo zzqqxx gibberish."
    full = f"{s1} {s2}"
    parse_fn = _CountingParseFn(
        {full: _parse_error(), s1: _ok(), s2: _parse_error()},
        default=_parse_error(),
    )
    fr = find_parse_failure_region(full, name="Foo", parse_fn=parse_fn)
    assert parse_fn.calls == [full, s1, s2]
    assert fr.bisection_calls == 3


# ---------------------------------------------------------------------------
# max_workers: parallel-equivalence via injected parse_fn.
# ---------------------------------------------------------------------------


def test_max_workers_with_parse_fn_produces_same_result_as_sequential():
    """When parse_fn is provided, max_workers is a no-op (pools can't
    pickle in-process fakes). The output must be byte-identical to
    sequential — production reliability rests on the parse_fn-injection
    path being the source of truth for behaviour."""
    s1 = "When Foo enters, draw a card."
    s2 = "Foo zzqqxx gibberish."
    s3 = "Bar zzqqxx more gibberish."
    full = f"{s1} {s2} {s3}"
    table = {full: _parse_error(), s1: _ok(), s2: _parse_error(), s3: _parse_error()}

    fr_serial = find_parse_failure_region(
        full, name="Foo",
        parse_fn=_make_parse_fn(table, default=_parse_error()),
        max_workers=1,
    )
    fr_parallel = find_parse_failure_region(
        full, name="Foo",
        parse_fn=_make_parse_fn(table, default=_parse_error()),
        max_workers=4,
    )
    assert fr_serial.sentences == fr_parallel.sentences
    assert fr_serial.bisection_calls == fr_parallel.bisection_calls
    assert fr_serial.overall_label == fr_parallel.overall_label


def test_max_workers_with_overall_label_and_parse_fn():
    """All three knobs together: skip-full-parse + parallel + injected
    parse_fn. Must produce exactly the per-sentence calls and the
    supplied overall_label."""
    s1 = "When Foo enters, draw a card."
    s2 = "Foo zzqqxx gibberish."
    parse_fn = _CountingParseFn(
        {s1: _ok(), s2: _parse_error()},
        default=_parse_error(),
    )
    fr = find_parse_failure_region(
        f"{s1} {s2}", name="Foo",
        parse_fn=parse_fn,
        overall_label="parse-error:<EOF>@t1234",
        max_workers=4,
    )
    assert sorted(parse_fn.calls) == sorted([s1, s2])
    assert fr.overall_label == "parse-error:<EOF>@t1234"
    assert fr.bisection_calls == 2
