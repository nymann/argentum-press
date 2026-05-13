"""Pipeline tests stub mtgcompiler so we can drive the orchestration without
the real parser. Catches the wiring bugs (path computation, outcome routing,
crash-on-compile-fail policy)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from argentum_press import _ast as ast
from argentum_press.lowerer import KotlinLowerer
from argentum_press.outcome import (
    DeferredEmitterGap,
    DeferredParseFailed,
    Emitted,
)
from argentum_press.pipeline import (
    AddCardPipeline,
    CompileVerificationFailed,
    FilesystemWriter,
)
from argentum_press.verify import CompileFail, CompileOk, CompileResult


@dataclass
class _StubCatalog:
    cards: list[dict[str, Any]]

    def fetch(self, set_code: str) -> list[dict[str, Any]]:
        return self.cards


@dataclass
class _ScriptedParser:
    """Looks up its response by card name. Anything not in the map is an
    'incomplete' parse error."""

    responses: dict[str, ast.ParseResult] = field(default_factory=dict)

    def parse(self, card: dict[str, Any]) -> ast.ParseResult:
        return self.responses.get(
            card["name"],
            ast.ParseResult(error=ast.ParseError("incomplete", "no rule")),
        )


@dataclass
class _AlwaysOkVerifier:
    def verify(self) -> CompileResult:
        return CompileOk()


@dataclass
class _AlwaysFailVerifier:
    stderr: str = "boom"

    def verify(self) -> CompileResult:
        return CompileFail(exit_code=1, stdout="", stderr=self.stderr)


def _flying_bird_card() -> dict[str, Any]:
    return {
        "name": "Test Bird",
        "mana_cost": "{1}{U}",
        "type_line": "Creature — Bird",
        "power": "1",
        "toughness": "1",
        "oracle_text": "Flying",
        "rarity": "common",
        "collector_number": "1",
        "color_identity": ["U"],
        "artist": "Anonymous",
    }


def test_emit_writes_to_expected_argentum_path(tmp_path: Path) -> None:
    card = _flying_bird_card()
    parser = _ScriptedParser({
        "Test Bird": ast.ParseResult(
            ast=ast.Card((ast.KeywordAbility(ast.Keyword.FLYING),))
        ),
    })
    pipeline = AddCardPipeline(
        catalog=_StubCatalog([card]),
        parser=parser,
        lowerer=KotlinLowerer(),
        verifier=_AlwaysOkVerifier(),
        writer=FilesystemWriter(),
        project_dir=tmp_path,
        set_code="por",
    )
    outcomes = pipeline.run()
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], Emitted)

    expected = (
        tmp_path
        / "mtg-sets"
        / "src"
        / "main"
        / "kotlin"
        / "com"
        / "wingedsheep"
        / "mtg"
        / "sets"
        / "definitions"
        / "por"
        / "cards"
        / "TestBird.kt"
    )
    assert expected.exists()
    body = expected.read_text()
    assert 'val TestBird = card("Test Bird")' in body
    assert "keywords(Keyword.FLYING)" in body


def test_parse_failure_yields_deferred_parse_failed(tmp_path: Path) -> None:
    card = _flying_bird_card() | {"name": "Untemplated"}
    pipeline = AddCardPipeline(
        catalog=_StubCatalog([card]),
        parser=_ScriptedParser({}),  # no entry -> incomplete
        lowerer=KotlinLowerer(),
        verifier=_AlwaysOkVerifier(),
        writer=FilesystemWriter(),
        project_dir=tmp_path,
        set_code="por",
    )
    [outcome] = pipeline.run()
    assert isinstance(outcome, DeferredParseFailed)
    assert outcome.name == "Untemplated"
    assert "incomplete" in outcome.error


def test_emitter_gap_yields_deferred_with_qualified_node(tmp_path: Path) -> None:
    card = _flying_bird_card() | {"name": "Activated"}
    activated_card = ast.Card(
        (ast.ActivatedAbility((ast.TapSelf(),), (ast.DrawCards(1),)),)
    )
    parser = _ScriptedParser({"Activated": ast.ParseResult(ast=activated_card)})
    pipeline = AddCardPipeline(
        catalog=_StubCatalog([card]),
        parser=parser,
        lowerer=KotlinLowerer(),
        verifier=_AlwaysOkVerifier(),
        writer=FilesystemWriter(),
        project_dir=tmp_path,
        set_code="por",
    )
    [outcome] = pipeline.run()
    gap = outcome
    assert isinstance(gap, DeferredEmitterGap)
    assert "ActivatedAbility" in gap.missing_node


def test_compile_failure_raises_with_outcome_attached(tmp_path: Path) -> None:
    card = _flying_bird_card()
    parser = _ScriptedParser({
        "Test Bird": ast.ParseResult(
            ast=ast.Card((ast.KeywordAbility(ast.Keyword.FLYING),))
        ),
    })
    pipeline = AddCardPipeline(
        catalog=_StubCatalog([card]),
        parser=parser,
        lowerer=KotlinLowerer(),
        verifier=_AlwaysFailVerifier(stderr="kotlinc: unresolved reference"),
        writer=FilesystemWriter(),
        project_dir=tmp_path,
        set_code="por",
    )
    with pytest.raises(CompileVerificationFailed) as info:
        pipeline.run()
    assert info.value.outcome.name == "Test Bird"
    assert "unresolved reference" in info.value.outcome.stderr


def test_limit_caps_processing(tmp_path: Path) -> None:
    cards = [
        _flying_bird_card() | {"name": f"Bird {i}"} for i in range(5)
    ]
    parser = _ScriptedParser({
        c["name"]: ast.ParseResult(
            ast=ast.Card((ast.KeywordAbility(ast.Keyword.FLYING),))
        )
        for c in cards
    })
    pipeline = AddCardPipeline(
        catalog=_StubCatalog(cards),
        parser=parser,
        lowerer=KotlinLowerer(),
        verifier=_AlwaysOkVerifier(),
        writer=FilesystemWriter(),
        project_dir=tmp_path,
        set_code="por",
    )
    outcomes = pipeline.run(limit=2)
    assert len(outcomes) == 2
