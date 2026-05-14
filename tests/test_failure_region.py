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
    # Now that the word-scan is gone, the cost is exactly:
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
