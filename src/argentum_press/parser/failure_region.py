# pyright: basic
"""Localize where in a card's oracle text the parser stops making progress.

For ``UnexpectedEOF`` failures Lark's ``pos_in_stream`` is ``-1`` — Earley
walks the whole input and only complains at the end that the parse can't
close. That throws away the most useful signal: WHICH phrase Lark
couldn't accept. We recover it deterministically by parsing each
sentence in isolation, then word-level scanning inside any sentence that
fails.

The output is intended for two consumers:

1. **The LLM** (parse-error playbook + freeform agent). A
   ``FailureRegion`` rendered into the prompt tells the model where the
   parser actually stops accepting tokens, eliminating the trial-and-
   error phase where the agent hand-crafts test phrases.
2. **The P1 ranker** in :mod:`argentum_press.playbook.context`. Scoring
   parent-rule candidates against just the failure-region text (instead
   of the whole oracle) avoids latching onto tokens that appear in
   already-parseable portions.

The parser is module-cached after the first compile, so subsequent
``parse()`` calls are sub-100ms; a typical card finishes in well under
a second of bisection work.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class SentenceStatus:
    """One sentence's parse outcome.

    ``kind`` distinguishes the three things ``parse()`` can return:

    * ``"ok"`` — the sentence is a complete cardtext.
    * ``"unmodeled"`` — Lark accepted the sentence; the lowerer doesn't
      have a handler for one of its AST nodes. **Not** a parse failure
      from the grammar's point of view.
    * ``"parse-error"`` — Lark itself can't accept the sentence. This is
      the case the parse-error playbook is meant to fix.

    For ``parse-error`` sentences, ``parseable_word_count`` is the
    largest ``k`` such that the first ``k`` words of the sentence don't
    produce a ``parse-error`` (i.e., still parse cleanly through Lark).
    The failure region inside the sentence is ``words[k:]``. ``None``
    for the other kinds.
    """

    sentence: str
    kind: str  # "ok" | "unmodeled" | "parse-error"
    parseable_word_count: int | None = None
    parse_error_label: str | None = None


@dataclass(frozen=True, slots=True)
class FailureRegion:
    """Per-sentence diagnostic for an oracle text that doesn't fully parse.

    ``fully_parses`` is ``True`` when ``parse(oracle_text)`` returns
    ``.ok`` (no error at all). In that case ``sentences`` is still
    populated for completeness, but no caller should branch on it.
    """

    fully_parses: bool
    overall_label: str | None
    sentences: tuple[SentenceStatus, ...]
    bisection_calls: int

    @property
    def failing_sentences(self) -> tuple[SentenceStatus, ...]:
        """The subset with ``kind == "parse-error"``."""
        return tuple(s for s in self.sentences if s.kind == "parse-error")

    def render_for_prompt(self) -> str:
        """One-block summary suitable for splicing into the pe_block.

        Format mirrors the existing pe_block sections: indented key-value
        pairs with multi-line context blocks. Empty when there's nothing
        useful to say (e.g. when the whole text parses cleanly — caller
        should normally not invoke render in that case).
        """
        if self.fully_parses:
            return "  (whole oracle text parses cleanly — failure-region analysis skipped)"
        lines: list[str] = []
        lines.append(f"  bisection_calls: {self.bisection_calls}")
        if not self.sentences:
            lines.append("  (no sentences parsed from oracle text)")
            return "\n".join(lines)
        lines.append("  per-sentence:")
        for i, s in enumerate(self.sentences, start=1):
            sentence_short = s.sentence if len(s.sentence) < 160 else s.sentence[:157] + "..."
            if s.kind == "ok":
                lines.append(f"    [{i}] OK         | {sentence_short}")
            elif s.kind == "unmodeled":
                label = s.parse_error_label or "(unknown)"
                lines.append(f"    [{i}] UNMODELED  | {label} | {sentence_short}")
            else:
                lines.append(f"    [{i}] PARSE-ERR  | {s.parse_error_label or '?'} | {sentence_short}")
                if s.parseable_word_count is not None:
                    words = s.sentence.split()
                    ok = " ".join(words[: s.parseable_word_count])
                    bad = " ".join(words[s.parseable_word_count :])
                    lines.append(f"        parser accepts: {ok!r}")
                    lines.append(f"        fails at:       {bad!r}")
        return "\n".join(lines)


def _classify(message: str | None) -> str:
    """Map a ParseError message to one of the three SentenceStatus kinds.

    Empty / unknown messages count as "ok" — the caller only invokes
    this when there's an error to classify.
    """
    if not message:
        return "ok"
    if message.startswith("parse-error:"):
        return "parse-error"
    # "unmodeled-rule:X", "lark-error:Y", etc. — Lark accepted it, the
    # failure is downstream of the parser itself.
    return "unmodeled"


def _is_parseable(message: str | None) -> bool:
    """True iff the message is NOT a Lark parse failure.

    `None`, "unmodeled-rule:...", "lark-error:..." — all mean Lark
    walked the input. Only "parse-error:..." indicates a grammar gap.
    """
    return _classify(message) != "parse-error"


def find_parse_failure_region(
    oracle_text: str, *, name: str = ""
) -> FailureRegion:
    """Locate the failure region in ``oracle_text``.

    Strategy:

    1. Parse the full text to get the overall label and confirm whether
       the failure is in the parser stage at all.
    2. Split into sentences and parse each in isolation. Sentences that
       parse (or fail at the lowerer with ``unmodeled-rule:``) are not
       the parser's problem; the ones that fail with ``parse-error:``
       are the candidate failure regions.
    3. For each failing sentence, forward-scan word-by-word: track the
       largest ``k`` such that ``words[:k]`` does NOT produce a
       ``parse-error``. The failure region within the sentence is
       ``words[k:]``.

    The word scan accepts the same "parses or fails downstream" success
    criterion as the sentence test. Prefixes that aren't a complete
    cardtext may still fail with ``parse-error:<EOF>`` (incomplete) —
    those count as parse-error here, so the scan localizes the LAST
    point where the prefix is parseable as a complete cardtext. That
    happens to align with the failure boundary on real-world cards in
    practice; pathological cases just return a smaller ``k``, which
    is still strictly more information than the full-text label.
    """
    # Local import: this module is loaded by the parser package; the
    # parse() entry point lives inside transformer.py and pulling it in
    # at module scope risks circular imports during package init.
    from argentum_press.parser.transformer import parse

    text = oracle_text.strip()
    calls = 0
    if not text:
        return FailureRegion(
            fully_parses=True,
            overall_label=None,
            sentences=(),
            bisection_calls=0,
        )

    overall = parse(text, name=name)
    calls += 1
    if overall.ok:
        return FailureRegion(
            fully_parses=True,
            overall_label=None,
            sentences=(),
            bisection_calls=calls,
        )

    overall_label = overall.error.message if overall.error else None

    sentences: list[SentenceStatus] = []
    for sentence in _SENTENCE_RE.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        r = parse(sentence, name=name)
        calls += 1
        if r.ok:
            sentences.append(SentenceStatus(sentence=sentence, kind="ok"))
            continue
        msg = r.error.message if r.error else None
        kind = _classify(msg)
        if kind != "parse-error":
            sentences.append(SentenceStatus(
                sentence=sentence, kind=kind, parse_error_label=msg,
            ))
            continue
        # Word-level localization for parse-error sentences.
        words = sentence.split()
        ok_k = 0
        for k in range(1, len(words) + 1):
            prefix = " ".join(words[:k])
            pr = parse(prefix, name=name)
            calls += 1
            if pr.ok or _is_parseable(pr.error.message if pr.error else None):
                ok_k = k
        sentences.append(SentenceStatus(
            sentence=sentence,
            kind="parse-error",
            parseable_word_count=ok_k,
            parse_error_label=msg,
        ))

    return FailureRegion(
        fully_parses=False,
        overall_label=overall_label,
        sentences=tuple(sentences),
        bisection_calls=calls,
    )


__all__ = ["FailureRegion", "SentenceStatus", "find_parse_failure_region"]
