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

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import _ast, existing
from .classify import Bucket1, Bucket2, classify
from .lowerer import KotlinLowerer
from .outcome import (
    AlreadyImplemented,
    CardOutcome,
    DeferredEmitterGap,
    DeferredParseFailed,
    Emitted,
)
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

    def run(self, limit: int | None = None) -> PipelineReport:
        cards = self.catalog.fetch(self.set_code)
        if limit is not None:
            cards = cards[:limit]

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
        return already, pending

    # ---- phase 2: classify ----

    def _classify_pending(
        self, pending: list[dict[str, Any]]
    ) -> tuple[
        list[DeferredParseFailed],
        list[tuple[dict[str, Any], str]],
        list[DeferredEmitterGap],
    ]:
        deferred_parse: list[DeferredParseFailed] = []
        bucket_1: list[tuple[dict[str, Any], str]] = []
        bucket_2: list[DeferredEmitterGap] = []
        for card in pending:
            name = card["name"]
            result = self.parser.parse(card)
            if not result.ok:
                deferred_parse.append(
                    DeferredParseFailed(name, _format_error(result.error))
                )
                continue
            assert result.ast is not None
            match classify(result.ast, self.lowerer):
                case Bucket1(body=body):
                    bucket_1.append((card, body))
                case Bucket2(missing_node=node_type):
                    bucket_2.append(DeferredEmitterGap(name, node_type))
        return deferred_parse, bucket_1, bucket_2

    # ---- phase 3: emit ----

    def _emit_bucket_1(
        self, bucket_1: list[tuple[dict[str, Any], str]]
    ) -> list[Emitted]:
        emitted: list[Emitted] = []
        for card, body in bucket_1:
            kotlin = render(card, body, self.set_code)
            path = self._target_path(card["name"])
            self.writer.write(path, kotlin)
            emitted.append(Emitted(card["name"], path))
        return emitted

    # ---- phase 4: verify (single shot, optional) ----

    def _verify(self, emitted: list[Emitted]) -> str | None:
        if self.verifier is None or not emitted:
            return None
        match self.verifier.verify():
            case CompileOk():
                return None
            case CompileFail() as fail:
                return fail.stderr

    # ---- helpers ----

    def _target_path(self, card_name: str) -> Path:
        return (
            existing.cards_dir(self.project_dir, self.set_code)
            / f"{_pascal_case(card_name)}.kt"
        )


def _format_error(error: _ast.ParseError | None) -> str:
    if error is None:
        return "parse failed with no error detail"
    if error.position is not None:
        return f"[{error.kind}@{error.position}] {error.message}"
    return f"[{error.kind}] {error.message}"
