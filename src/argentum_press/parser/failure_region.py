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
from typing import Any, Callable


ParseFn = Callable[..., Any]
"""Shape of :func:`argentum_press.parser.transformer.parse`. Accepts the
oracle text and a ``name=`` kwarg, returns a ``ParseResult``. Pulled out
as a type so tests can inject a fast in-memory fake instead of paying
the ~10s cost of compiling the real grammar."""


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
    """

    sentence: str
    kind: str  # "ok" | "unmodeled" | "parse-error"
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


def _classify_parse_result(r: Any) -> tuple[str, str | None]:
    """Map a ParseResult to ``(kind, label_or_None)``.

    Shared between the sequential walk and the ProcessPool worker so
    classification can't drift between paths.
    """
    if r.ok:
        return "ok", None
    msg = r.error.message if (r.error is not None) else None
    kind = _classify(msg)
    return kind, (msg if kind != "ok" else None)


def _pool_worker_init() -> None:
    """ProcessPool initialiser: pre-compile the grammar in each worker.

    Each worker is a fresh interpreter (macOS uses ``spawn``), so we
    pay the ~10s grammar compile per worker on startup. Subsequent
    ``parse()`` calls in that worker reuse the module-level
    ``_PARSER`` cache for free.
    """
    from argentum_press.parser.transformer import _get_parser
    _get_parser()


def _pool_worker_parse(args: tuple[str, str]) -> tuple[str, str | None]:
    """ProcessPool worker entry point. Must be module-level (picklable).

    ``args = (sentence, name)``. Returns the same classification tuple
    the sequential path uses.
    """
    sentence, name = args
    from argentum_press.parser.transformer import parse
    return _classify_parse_result(parse(sentence, name=name))


def find_parse_failure_region(
    oracle_text: str,
    *,
    name: str = "",
    parse_fn: ParseFn | None = None,
    overall_label: str | None = None,
    max_workers: int = 1,
) -> FailureRegion:
    """Locate the failure region in ``oracle_text``.

    Strategy:

    1. (Optional) Parse the full text to get the overall label. Skipped
       when ``overall_label`` is supplied — the caller (typically
       ``_format_parse_error_block``) already has the label from gap
       classification and the re-parse is pure waste.
    2. Split into sentences and parse each in isolation. Sentences that
       parse (or fail at the lowerer with ``unmodeled-rule:``) are not
       the parser's problem; the ones that fail with ``parse-error:``
       are the candidate failure regions.

    Cost is bounded at ``N`` parse calls when ``overall_label`` is
    supplied, ``1 + N`` otherwise. Each Earley parse is ~10–30s on real
    card-shaped text.

    Parallelism: ``max_workers > 1`` dispatches the per-sentence parses
    to a :class:`ProcessPoolExecutor` whose workers pre-compile the
    grammar via :func:`_pool_worker_init`. Wall time drops from
    ``N × parse_time`` to roughly ``grammar_compile + parse_time``
    (workers compile in parallel). Falls back to sequential when
    ``parse_fn`` is provided (test injection — pools can't pickle
    in-process fakes) or ``max_workers <= 1``.
    """
    text = oracle_text.strip()
    if not text:
        return FailureRegion(
            fully_parses=True,
            overall_label=overall_label,
            sentences=(),
            bisection_calls=0,
        )

    # --- step 1: confirm failure + get overall label -----------------
    calls = 0
    if overall_label is None:
        # Test path or call-sites that don't yet know the label.
        fn = parse_fn
        if fn is None:
            from argentum_press.parser.transformer import parse as fn
        overall = fn(text, name=name)
        calls += 1
        if overall.ok:
            return FailureRegion(
                fully_parses=True,
                overall_label=None,
                sentences=(),
                bisection_calls=calls,
            )
        overall_label = overall.error.message if overall.error else None
    # else: caller supplied overall_label; skip the redundant parse.

    # --- step 2: per-sentence walk -----------------------------------
    raw_sentences = [s.strip() for s in _SENTENCE_RE.split(text)]
    raw_sentences = [s for s in raw_sentences if s]

    # Parallel path only fires when no injected parse_fn (tests) and the
    # caller asked for >1 workers and we actually have more than one
    # sentence to dispatch — single-sentence cards aren't worth the
    # pool-setup overhead.
    use_pool = parse_fn is None and max_workers > 1 and len(raw_sentences) > 1
    if use_pool:
        from concurrent.futures import ProcessPoolExecutor
        workers = min(max_workers, len(raw_sentences))
        with ProcessPoolExecutor(
            max_workers=workers,
            initializer=_pool_worker_init,
        ) as pool:
            classifications = list(pool.map(
                _pool_worker_parse,
                [(s, name) for s in raw_sentences],
            ))
        sentences = tuple(
            SentenceStatus(sentence=s, kind=kind, parse_error_label=label)
            for s, (kind, label) in zip(raw_sentences, classifications, strict=True)
        )
        calls += len(raw_sentences)
        return FailureRegion(
            fully_parses=False,
            overall_label=overall_label,
            sentences=sentences,
            bisection_calls=calls,
        )

    # Sequential fallback (the test-injected path and the
    # single-sentence path both land here).
    fn = parse_fn
    if fn is None:
        from argentum_press.parser.transformer import parse as fn

    sentence_statuses: list[SentenceStatus] = []
    for sentence in raw_sentences:
        r = fn(sentence, name=name)
        calls += 1
        kind, label = _classify_parse_result(r)
        sentence_statuses.append(SentenceStatus(
            sentence=sentence, kind=kind, parse_error_label=label,
        ))

    return FailureRegion(
        fully_parses=False,
        overall_label=overall_label,
        sentences=tuple(sentence_statuses),
        bisection_calls=calls,
    )


__all__ = ["FailureRegion", "SentenceStatus", "find_parse_failure_region"]
