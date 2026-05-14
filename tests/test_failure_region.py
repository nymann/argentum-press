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


def test_failing_sentence_localizes_word_boundary():
    # A failing sentence whose first 3 words ("When Foo enters") parse
    # cleanly. Anything longer fails. The word scan should report
    # parseable_word_count == 3.
    sentence = "When Foo enters then zzqqxx the world."
    table = {
        sentence: _parse_error(),
        "When": _parse_error(),  # incomplete: still parse-error
        "When Foo": _parse_error(),
        "When Foo enters": _ok(),
    }
    # All longer prefixes default to parse-error.
    parse_fn = _make_parse_fn(table, default=_parse_error())
    fr = find_parse_failure_region(sentence, name="Foo", parse_fn=parse_fn)
    assert fr.fully_parses is False
    s = fr.failing_sentences[0]
    assert s.parseable_word_count == 3, fr.render_for_prompt()


def test_failing_sentence_accepts_unmodeled_prefix_as_parseable():
    # "Lark parses but lowerer doesn't" — i.e. unmodeled-rule on a
    # prefix — counts as "parses far enough", so the boundary moves
    # past it. This matches the real-world freeform probe behaviour
    # (unmodeled-rule means the parser walked the whole prefix).
    sentence = "Put cards on the bottom then zzqqxx."
    table = {
        sentence: _parse_error(),
        "Put": _parse_error(),
        "Put cards": _parse_error(),
        "Put cards on": _parse_error(),
        "Put cards on the": _parse_error(),
        "Put cards on the bottom": _unmodeled("putinzoneexpression"),
    }
    parse_fn = _make_parse_fn(table, default=_parse_error())
    fr = find_parse_failure_region(sentence, name="Foo", parse_fn=parse_fn)
    s = fr.failing_sentences[0]
    assert s.parseable_word_count == 5, fr.render_for_prompt()


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


def test_bisection_call_count_reflects_work():
    # 1 (full) + 2 (per-sentence) + N (word scan in the failing one)
    # — N is len(s2.split()) = 4 here. Total = 7.
    s1 = "When Foo enters, you draw a card."
    s2 = "Foo zzqqxx random gibberish."
    full = f"{s1} {s2}"
    parse_fn = _make_parse_fn(
        {full: _parse_error(), s1: _ok(), s2: _parse_error()},
        default=_parse_error(),
    )
    fr = find_parse_failure_region(full, name="Foo", parse_fn=parse_fn)
    assert fr.bisection_calls == 1 + 2 + len(s2.split())
