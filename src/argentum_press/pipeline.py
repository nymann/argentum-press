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
        for card in pending:
            name = card["name"]
            result = self.parser.parse(card)
            if not result.ok:
                outcome = DeferredParseFailed(name, _format_error(result.error))
                deferred_parse.append(outcome)
                self.reporter.card_parse_failed(outcome)
                continue
            assert result.ast is not None
            match classify(result.ast, self.lowerer):
                case Bucket1(body=body):
                    bucket_1.append((card, body))
                    self.reporter.card_classified_bucket_1(name)
                case Bucket2(missing_node=node_type):
                    outcome = DeferredEmitterGap(name, node_type)
                    bucket_2.append(outcome)
                    self.reporter.card_classified_bucket_2(outcome)
        self.reporter.phase_classify_end(
            bucket_1=len(bucket_1),
            bucket_2=len(bucket_2),
            parse_failed=len(deferred_parse),
        )
        return deferred_parse, bucket_1, bucket_2

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
