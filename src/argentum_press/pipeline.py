"""End-to-end orchestration: catalog -> parse -> lower -> template -> verify."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from . import _ast
from .lowerer import EmitterGap, KotlinLowerer
from .outcome import (
    CardOutcome,
    CompileFailed,
    DeferredEmitterGap,
    DeferredParseFailed,
    Emitted,
)
from .template import _pascal_case, render
from .verify import CompileFail, CompileOk, CompileVerifier


class Catalog(Protocol):
    def fetch(self, set_code: str) -> list[dict[str, Any]]: ...


class Parser(Protocol):
    """The contract argentum-press expects mtgcompiler to satisfy."""

    def parse(self, card: dict[str, Any]) -> _ast.ParseResult: ...


class Writer(Protocol):
    def write(self, path: Path, contents: str) -> None: ...


class FilesystemWriter:
    def write(self, path: Path, contents: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)


class CompileVerificationFailed(RuntimeError):
    """Raised by the pipeline when gradle compileKotlin rejects an emitted card.

    Per current policy (no LLM repair yet) the pipeline propagates this rather
    than trying to recover. The eventual repair loop catches this and asks the
    Driver for a patch.
    """

    def __init__(self, name: str, path: Path, fail: CompileFail) -> None:
        self.outcome = CompileFailed(name=name, path=path, stderr=fail.stderr)
        super().__init__(
            f"compile failed for {name} at {path} (exit {fail.exit_code}):\n{fail.stderr}"
        )


@dataclass
class AddCardPipeline:
    catalog: Catalog
    parser: Parser
    lowerer: KotlinLowerer
    verifier: CompileVerifier
    writer: Writer
    project_dir: Path
    set_code: str

    def run(self, limit: int | None = None) -> list[CardOutcome]:
        cards = self.catalog.fetch(self.set_code)
        if limit is not None:
            cards = cards[:limit]
        return [self._process(card) for card in cards]

    def _process(self, card: dict[str, Any]) -> CardOutcome:
        name = card["name"]
        parse = self.parser.parse(card)
        if not parse.ok:
            return DeferredParseFailed(name, _format_error(parse.error))
        assert parse.ast is not None
        try:
            body = self.lowerer.lower_card(parse.ast)
        except EmitterGap as gap:
            return DeferredEmitterGap(name, gap.node_type)
        kotlin = render(card, body, self.set_code)
        target = self._target_path(name)
        self.writer.write(target, kotlin)
        match self.verifier.verify():
            case CompileOk():
                return Emitted(name, target)
            case CompileFail() as fail:
                raise CompileVerificationFailed(name, target, fail)

    def _target_path(self, card_name: str) -> Path:
        return (
            self.project_dir
            / "mtg-sets"
            / "src"
            / "main"
            / "kotlin"
            / "com"
            / "wingedsheep"
            / "mtg"
            / "sets"
            / "definitions"
            / self.set_code
            / "cards"
            / f"{_pascal_case(card_name)}.kt"
        )


def _format_error(error: _ast.ParseError | None) -> str:
    if error is None:
        return "parse failed with no error detail"
    if error.position is not None:
        return f"[{error.kind}@{error.position}] {error.message}"
    return f"[{error.kind}] {error.message}"


def summarize(outcomes: list[CardOutcome]) -> dict[str, int]:
    counts: dict[str, int] = {
        "emitted": 0,
        "deferred_parse": 0,
        "deferred_gap": 0,
    }
    for outcome in outcomes:
        match outcome:
            case Emitted():
                counts["emitted"] += 1
            case DeferredParseFailed():
                counts["deferred_parse"] += 1
            case DeferredEmitterGap():
                counts["deferred_gap"] += 1
            case CompileFailed():
                # The pipeline raises on compile fail today, so this branch is
                # only reached if a caller constructs a CompileFailed outcome
                # directly (the future repair loop will).
                counts["compile_failed"] = counts.get("compile_failed", 0) + 1
    return counts
