"""End-to-end orchestration in four phases:

  1. triage      — subtract cards argentum-engine already has
  2. classify    — dry-run the lowerer; bucket 1 (emit-ready) vs bucket 2
                    (needs an argentum-engine primitive we don't lower into)
  3. emit        — write bucket-1 cards to <project>/mtg-sets/.../<set>/cards/
  4. verify      — single gradle compileKotlin run against the whole set
                    (no LLM repair yet; failures are surfaced in the report
                    but don't crash the run)

Phase 4 is currently a stub: we run gradle if a verifier was passed in, and
on failure the report carries the gradle stderr. We do *not* try to attribute
the failure to specific cards — that's where the LLM repair turn will plug
in, by parsing the stderr into per-file errors.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from . import _ast, existing
from .catalog import CacheState
from .classify import Bucket1, Bucket2, classify
from .lowerer import KotlinLowerer
from .outcome import (
    AlreadyImplemented,
    CardOutcome,
    DeferredEmitterGap,
    DeferredParseFailed,
    Emitted,
)
from .reporter import NullReporter, Reporter
from .template import _pascal_case, render
from .verify import CompileFail, CompileOk, CompileResult

# ---- classify-loop result variants ----
#
# The classify phase produces one of these per card. We surface them as
# their own discriminated union (rather than the public CardOutcome) so a
# successful bucket-1 classification can carry the lowered body forward
# to the emit phase without re-running the lowerer. Each variant is a
# frozen dataclass with picklable fields so the same shape works in
# either the serial or process-pool path.


@dataclass(frozen=True, slots=True)
class _ClassifyParseFailed:
    outcome: DeferredParseFailed
    worker_pid: int | None = None
    elapsed_s: float | None = None


@dataclass(frozen=True, slots=True)
class _ClassifyBucket1:
    card: dict[str, Any]
    body: str
    worker_pid: int | None = None
    elapsed_s: float | None = None


@dataclass(frozen=True, slots=True)
class _ClassifyBucket2:
    outcome: DeferredEmitterGap
    worker_pid: int | None = None
    elapsed_s: float | None = None


_ClassifyResult = _ClassifyParseFailed | _ClassifyBucket1 | _ClassifyBucket2


class Catalog(Protocol):
    def fetch(self, set_code: str) -> list[dict[str, Any]]: ...


class Parser(Protocol):
    """The contract argentum-press expects mtgcompiler to satisfy."""

    def parse(self, card: dict[str, Any]) -> _ast.ParseResult: ...


class Writer(Protocol):
    def write(self, path: Path, contents: str) -> None: ...


class Verifier(Protocol):
    def verify(self) -> CompileResult: ...


class FilesystemWriter:
    def write(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)


@dataclass(frozen=True, slots=True)
class PipelineReport:
    set_code: str
    already_implemented: tuple[AlreadyImplemented, ...] = ()
    deferred_parse: tuple[DeferredParseFailed, ...] = ()
    bucket_2: tuple[DeferredEmitterGap, ...] = ()
    emitted: tuple[Emitted, ...] = ()
    compile_stderr: str | None = None
    """Populated only when phase 4 ran AND gradle returned non-zero."""

    @property
    def all_outcomes(self) -> list[CardOutcome]:
        out: list[CardOutcome] = []
        out.extend(self.already_implemented)
        out.extend(self.deferred_parse)
        out.extend(self.bucket_2)
        out.extend(self.emitted)
        return out


@dataclass
class AddSetPipeline:
    catalog: Catalog
    parser: Parser
    lowerer: KotlinLowerer
    writer: Writer
    project_dir: Path
    set_code: str
    verifier: Verifier | None = None
    """When None (or --skip-verify on the CLI), phase 4 is skipped entirely."""

    reporter: Reporter = field(default_factory=NullReporter)
    """Receives phase + per-card events as the run progresses. Default no-op."""

    workers: int = 1
    """When > 1, the classify phase runs each card in a ProcessPoolExecutor
    worker. Default 1 (serial) keeps tests deterministic and lets them inject
    fake parsers; the CLI passes os.cpu_count(). Workers always parse via
    mtgcompiler.parse directly — the injected `parser` is only used on the
    serial path."""

    def run(self, limit: int | None = None) -> PipelineReport:
        cards = self._fetch(limit)
        already, pending = self._triage(cards)
        deferred_parse, bucket_1, bucket_2 = self._classify_pending(pending)
        emitted = self._emit_bucket_1(bucket_1)
        compile_stderr = self._verify(emitted)

        return PipelineReport(
            set_code=self.set_code,
            already_implemented=tuple(already),
            deferred_parse=tuple(deferred_parse),
            bucket_2=tuple(bucket_2),
            emitted=tuple(emitted),
            compile_stderr=compile_stderr,
        )

    # ---- phase 1: triage ----

    def _fetch(self, limit: int | None) -> list[dict[str, Any]]:
        self.reporter.phase_triage_start(self.set_code)
        cards = self.catalog.fetch(self.set_code)
        if limit is not None:
            cards = cards[:limit]
        self.reporter.phase_triage_fetched(len(cards), _cache_label(self.catalog))
        return cards

    def _triage(
        self, cards: list[dict[str, Any]]
    ) -> tuple[list[AlreadyImplemented], list[dict[str, Any]]]:
        implemented = existing.implemented_cards_in_set(self.project_dir, self.set_code)
        already: list[AlreadyImplemented] = []
        pending: list[dict[str, Any]] = []
        for card in cards:
            front = existing.front_face(card["name"])
            if front in implemented:
                already.append(AlreadyImplemented(card["name"]))
            else:
                pending.append(card)
        self.reporter.phase_triage_end(
            already_implemented=len(already), pending=len(pending)
        )
        return already, pending

    # ---- phase 2: classify ----

    def _classify_pending(
        self, pending: list[dict[str, Any]]
    ) -> tuple[
        list[DeferredParseFailed],
        list[tuple[dict[str, Any], str]],
        list[DeferredEmitterGap],
    ]:
        self.reporter.phase_classify_start(len(pending))
        deferred_parse: list[DeferredParseFailed] = []
        bucket_1: list[tuple[dict[str, Any], str]] = []
        bucket_2: list[DeferredEmitterGap] = []
        for result in self._iter_classifications(pending):
            match result:
                case _ClassifyParseFailed(outcome=outcome, worker_pid=pid, elapsed_s=el):
                    deferred_parse.append(outcome)
                    self.reporter.card_parse_failed(
                        outcome, worker_pid=pid, elapsed_s=el
                    )
                case _ClassifyBucket1(card=card, body=body, worker_pid=pid, elapsed_s=el):
                    bucket_1.append((card, body))
                    self.reporter.card_classified_bucket_1(
                        card["name"], worker_pid=pid, elapsed_s=el
                    )
                case _ClassifyBucket2(outcome=outcome, worker_pid=pid, elapsed_s=el):
                    bucket_2.append(outcome)
                    self.reporter.card_classified_bucket_2(
                        outcome, worker_pid=pid, elapsed_s=el
                    )
        self.reporter.phase_classify_end(
            bucket_1=len(bucket_1),
            bucket_2=len(bucket_2),
            parse_failed=len(deferred_parse),
        )
        return deferred_parse, bucket_1, bucket_2

    def _iter_classifications(
        self, pending: list[dict[str, Any]]
    ) -> Iterator[_ClassifyResult]:
        if self.workers <= 1:
            for card in pending:
                yield _classify_one(card, self.parser, self.lowerer)
            return

        # Process-pool path: each worker (re-)imports mtgcompiler and builds
        # its own cached compiler on first call. We use as_completed (not map)
        # so results stream out as workers finish — otherwise a single slow
        # card at the front of the list blocks the reporter from displaying
        # every fast card behind it, which makes the run look stuck.
        from concurrent.futures import ProcessPoolExecutor, as_completed

        with ProcessPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(_classify_card_worker, card) for card in pending]
            for future in as_completed(futures):
                yield future.result()

    # ---- phase 3: emit ----

    def _emit_bucket_1(
        self, bucket_1: list[tuple[dict[str, Any], str]]
    ) -> list[Emitted]:
        self.reporter.phase_emit_start(len(bucket_1))
        emitted: list[Emitted] = []
        for card, body in bucket_1:
            kotlin = render(card, body, self.set_code)
            path = self._target_path(card["name"])
            self.writer.write(path, kotlin)
            outcome = Emitted(card["name"], path)
            emitted.append(outcome)
            self.reporter.card_emitted(outcome)
        self.reporter.phase_emit_end(len(emitted))
        return emitted

    # ---- phase 4: verify (single shot, optional) ----

    def _verify(self, emitted: list[Emitted]) -> str | None:
        if self.verifier is None:
            self.reporter.phase_verify_skipped("no verifier configured (--skip-verify)")
            return None
        if not emitted:
            self.reporter.phase_verify_skipped("nothing was emitted")
            return None
        self.reporter.phase_verify_start()
        match self.verifier.verify():
            case CompileOk():
                self.reporter.phase_verify_passed()
                return None
            case CompileFail() as fail:
                self.reporter.phase_verify_failed(fail.stderr)
                return fail.stderr

    # ---- helpers ----

    def _target_path(self, card_name: str) -> Path:
        return (
            existing.cards_dir(self.project_dir, self.set_code)
            / f"{_pascal_case(card_name)}.kt"
        )


def _cache_label(catalog: Catalog) -> str:
    """Best-effort human-readable cache state for the reporter. Catalog
    implementations that don't expose `last_cache_state` show 'unknown'."""
    state: CacheState | None = getattr(catalog, "last_cache_state", None)
    if state is None:
        return "unknown"
    return state.source


def _format_error(error: _ast.ParseError | None) -> str:
    if error is None:
        return "parse failed with no error detail"
    if error.position is not None:
        return f"[{error.kind}@{error.position}] {error.message}"
    return f"[{error.kind}] {error.message}"


def _classify_one(
    card: dict[str, Any], parser: Parser, lowerer: KotlinLowerer
) -> _ClassifyResult:
    """Parse + classify one card. Pure function — shared by the serial path
    on the pipeline and the worker entry below. Stamps wall-clock elapsed
    time so the reporter can flag slow cards."""
    name = card["name"]
    t0 = time.perf_counter()
    result = parser.parse(card)
    if not result.ok:
        elapsed = time.perf_counter() - t0
        return _ClassifyParseFailed(
            DeferredParseFailed(name, _format_error(result.error)),
            elapsed_s=elapsed,
        )
    assert result.ast is not None
    match classify(result.ast, lowerer):
        case Bucket1(body=body):
            elapsed = time.perf_counter() - t0
            return _ClassifyBucket1(card=card, body=body, elapsed_s=elapsed)
        case Bucket2(missing_node=node_type):
            elapsed = time.perf_counter() - t0
            return _ClassifyBucket2(
                DeferredEmitterGap(name, node_type), elapsed_s=elapsed
            )


# Per-worker singletons. ProcessPoolExecutor uses 'spawn' on macOS, so each
# worker process gets its own fresh copies and rebuilds the Lark compiler
# on first parse. We cache here so successive cards in the same worker reuse
# the same parser + lowerer instead of rebuilding 250 times.
_WORKER_PARSER: Parser | None = None
_WORKER_LOWERER: KotlinLowerer | None = None


def _classify_card_worker(card: dict[str, Any]) -> _ClassifyResult:
    """Top-level worker entry — must be importable so ProcessPoolExecutor can
    pickle it. Workers always parse via mtgcompiler directly; the pipeline's
    injected `parser` is only honored on the serial path (see workers field)."""
    global _WORKER_PARSER, _WORKER_LOWERER
    if _WORKER_PARSER is None:
        import mtgcompiler  # type: ignore[import-untyped]

        class _MtgCompilerParser:
            def parse(self, card: dict[str, Any]) -> _ast.ParseResult:
                return mtgcompiler.parse(card)  # type: ignore[no-any-return]

        _WORKER_PARSER = _MtgCompilerParser()
    if _WORKER_LOWERER is None:
        _WORKER_LOWERER = KotlinLowerer()
    result = _classify_one(card, _WORKER_PARSER, _WORKER_LOWERER)
    pid = os.getpid()
    # Stamp the worker pid onto the result so the reporter can show which
    # worker did the parse. Preserve elapsed_s that _classify_one measured.
    match result:
        case _ClassifyParseFailed(outcome=o, elapsed_s=el):
            return _ClassifyParseFailed(outcome=o, worker_pid=pid, elapsed_s=el)
        case _ClassifyBucket1(card=c, body=b, elapsed_s=el):
            return _ClassifyBucket1(card=c, body=b, worker_pid=pid, elapsed_s=el)
        case _ClassifyBucket2(outcome=o, elapsed_s=el):
            return _ClassifyBucket2(outcome=o, worker_pid=pid, elapsed_s=el)
