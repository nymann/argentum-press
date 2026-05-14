"""Pipeline tests stub mtgcompiler, the catalog, and the verifier so we can
drive the orchestration without external state. Each test exercises one of
the four phases (triage / classify / emit / verify).

After the rich-AST absorption, all ``ParseResult.ast`` payloads are rich
:class:`argentum_press.parser.ast.Card` instances; the parser stub returns
``argentum_press.parser.ParseResult`` directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argentum_press.existing import cards_dir, set_root_dir
from argentum_press.lowerer import KotlinLowerer
from argentum_press.parser import ParseError, ParseResult, ast
from argentum_press.pipeline import (
    AddSetPipeline,
    FilesystemWriter,
    PipelineReport,
)
from argentum_press.verify import CompileFail, CompileOk, CompileResult


@dataclass
class _StubCatalog:
    cards: list[dict[str, Any]]

    def fetch(self, set_code: str) -> list[dict[str, Any]]:
        return self.cards


def _empty_responses() -> dict[str, ParseResult]:
    return {}


@dataclass
class _ScriptedParser:
    responses: dict[str, ParseResult] = field(default_factory=_empty_responses)

    def parse(self, card: dict[str, Any]) -> ParseResult:
        return self.responses.get(
            card["name"],
            ParseResult(error=ParseError("incomplete", "no rule")),
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


def _flying_bird(name: str = "Test Bird") -> dict[str, Any]:
    return {
        "name": name,
        "mana_cost": "{1}{U}",
        "type_line": "Creature — Bird",
        "power": "1",
        "toughness": "1",
        "oracle_text": "Flying",
        "rarity": "common",
        "collector_number": "1",
        "color_identity": ["U"],
    }


# ---- rich-AST shape builders ------------------------------------------------


def _flying_card() -> ast.Card:
    """A card whose sole ability is the simple-keyword Flying."""
    return ast.Card(
        abilities=(ast.SimpleKeywordAbility(keyword=ast.Keyword.FLYING),),
    )


def _activated_card() -> ast.Card:
    """A card whose sole ability is activated (lowerer raises EmitterGap)."""
    return ast.Card(
        abilities=(
            ast.ActivatedAbility(
                cost=ast.Name(name="{T}"),
                instructions=ast.ExpressionStatement(
                    root=ast.CardDrawExpression(
                        quantity=ast.NumberValue(value=1),
                    ),
                ),
            ),
        ),
    )


def _build_pipeline(
    tmp_path: Path,
    *,
    cards: list[dict[str, Any]],
    parser: _ScriptedParser,
    verifier: _AlwaysOkVerifier | _AlwaysFailVerifier | None = None,
) -> AddSetPipeline:
    return AddSetPipeline(
        catalog=_StubCatalog(cards),
        parser=parser,
        lowerer=KotlinLowerer(),
        writer=FilesystemWriter(),
        project_dir=tmp_path,
        set_code="por",
        verifier=verifier,
    )


# ---- triage ----

def test_already_implemented_cards_are_not_processed(tmp_path: Path) -> None:
    target = cards_dir(tmp_path, "por") / "TestBird.kt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('val TestBird = card("Test Bird") { }\n')

    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird()],
        parser=_ScriptedParser(),  # would fail on any parse — we shouldn't call it
    )
    report = pipeline.run()
    assert len(report.already_implemented) == 1
    assert report.already_implemented[0].name == "Test Bird"
    assert report.emitted == ()
    assert report.deferred_parse == ()


def test_dfc_already_implemented_matches_by_front_face(tmp_path: Path) -> None:
    target = cards_dir(tmp_path, "por") / "Day.kt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('val DayNight = card("Day // Night") { }\n')

    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird("Day // Night")],
        parser=_ScriptedParser(),
    )
    report = pipeline.run()
    assert len(report.already_implemented) == 1


# ---- classify ----

def test_parse_failure_yields_deferred_parse(tmp_path: Path) -> None:
    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird("Untemplated")],
        parser=_ScriptedParser(),  # no entry => incomplete
    )
    report = pipeline.run()
    assert len(report.deferred_parse) == 1
    assert report.deferred_parse[0].name == "Untemplated"
    assert "incomplete" in report.deferred_parse[0].error
    assert report.emitted == ()


def test_emitter_gap_yields_bucket_2_with_node_type(tmp_path: Path) -> None:
    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird("Activated")],
        parser=_ScriptedParser({"Activated": ParseResult(ast=_activated_card())}),
    )
    report = pipeline.run()
    assert len(report.bucket_2) == 1
    assert report.bucket_2[0].name == "Activated"
    assert "ActivatedAbility" in report.bucket_2[0].missing_node
    assert report.emitted == ()


# ---- emit ----

def test_bucket_1_is_written_at_argentum_path(tmp_path: Path) -> None:
    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird()],
        parser=_ScriptedParser({"Test Bird": ParseResult(ast=_flying_card())}),
    )
    report = pipeline.run()
    assert len(report.emitted) == 1
    expected = cards_dir(tmp_path, "por") / "TestBird.kt"
    assert report.emitted[0].path == expected
    assert expected.exists()
    assert "keywords(Keyword.FLYING)" in expected.read_text()


# ---- verify (phase 4 is a stub) ----

def test_phase_4_skipped_when_no_verifier(tmp_path: Path) -> None:
    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird()],
        parser=_ScriptedParser({"Test Bird": ParseResult(ast=_flying_card())}),
        verifier=None,
    )
    report = pipeline.run()
    assert report.compile_stderr is None


def test_phase_4_skipped_when_nothing_was_emitted(tmp_path: Path) -> None:
    # Bucket-2 only; phase 4 doesn't run.
    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird("Activated")],
        parser=_ScriptedParser({"Activated": ParseResult(ast=_activated_card())}),
        verifier=_AlwaysFailVerifier(stderr="should not run"),
    )
    report = pipeline.run()
    assert report.compile_stderr is None


def test_phase_4_records_compile_stderr_without_crashing(tmp_path: Path) -> None:
    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird()],
        parser=_ScriptedParser({"Test Bird": ParseResult(ast=_flying_card())}),
        verifier=_AlwaysFailVerifier(stderr="kotlinc: unresolved reference: Effects.Wat"),
    )
    report = pipeline.run()
    assert report.compile_stderr is not None
    assert "unresolved reference" in report.compile_stderr
    # The file is still on disk; we report the failure, we don't roll back.
    assert len(report.emitted) == 1


def test_phase_4_compile_ok_leaves_stderr_unset(tmp_path: Path) -> None:
    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird()],
        parser=_ScriptedParser({"Test Bird": ParseResult(ast=_flying_card())}),
        verifier=_AlwaysOkVerifier(),
    )
    report = pipeline.run()
    assert report.compile_stderr is None


# ---- limit ----

def test_limit_caps_processing(tmp_path: Path) -> None:
    cards = [_flying_bird(f"Bird {i}") for i in range(5)]
    pipeline = _build_pipeline(
        tmp_path,
        cards=cards,
        parser=_ScriptedParser({
            c["name"]: ParseResult(ast=_flying_card())
            for c in cards
        }),
    )
    report = pipeline.run(limit=2)
    assert len(report.emitted) == 2


# ---- basic lands ----


def _basic(subtype: str, collector: str, set_name: str = "Marvel's Spider-Man") -> dict[str, Any]:
    return {
        "name": subtype,
        "type_line": f"Basic Land — {subtype}",
        "collector_number": collector,
        "set_name": set_name,
        "artist": "Test Artist",
        "image_uris": {"normal": f"https://example/{subtype.lower()}-{collector}.jpg"},
        # Fields the regular renderer expects — basics bypass the renderer
        # but the triage still pulls `name`, so keep them present.
        "mana_cost": "",
        "color_identity": [],
        "rarity": "common",
    }


def test_basic_lands_bypass_classify_and_get_one_combined_file(tmp_path: Path) -> None:
    # The set has no `*Set.kt` yet — prefix falls back to PascalCase(set_name).
    parser = _ScriptedParser()  # would explode if any basic reached parse
    pipeline = _build_pipeline(
        tmp_path,
        cards=[
            _basic("Plains", "1", set_name="Bloomburrow"),
            _basic("Island", "2", set_name="Bloomburrow"),
        ],
        parser=parser,
    )
    report = pipeline.run()
    assert report.deferred_parse == ()
    assert report.emitted == ()
    assert report.emitted_basic_lands is not None
    expected = cards_dir(tmp_path, "por") / "BloomburrowBasicLands.kt"
    assert report.emitted_basic_lands.path == expected
    assert report.emitted_basic_lands.count == 2
    body = expected.read_text()
    assert 'basicLand("Plains")' in body
    assert 'basicLand("Island")' in body
    # Critically: must NOT emit each basic as its own `card("Plains")` file.
    assert not (cards_dir(tmp_path, "por") / "Plains.kt").exists()
    assert not (cards_dir(tmp_path, "por") / "Island.kt").exists()


def test_basic_lands_prefix_prefers_existing_set_kt(tmp_path: Path) -> None:
    # Scaffold a `*Set.kt` like argentum-engine has for SPM.
    root = set_root_dir(tmp_path, "por")
    root.mkdir(parents=True)
    (root / "SpiderManSet.kt").write_text(
        "object SpiderManSet : MtgSet { override val code = \"POR\" }\n"
    )
    pipeline = _build_pipeline(
        tmp_path,
        cards=[_basic("Forest", "10")],
        parser=_ScriptedParser(),
    )
    report = pipeline.run()
    assert report.emitted_basic_lands is not None
    assert report.emitted_basic_lands.path.name == "SpiderManBasicLands.kt"
    body = report.emitted_basic_lands.path.read_text()
    assert "val SpiderManForest10 = basicLand(\"Forest\")" in body


def test_basics_already_implemented_skips_basics_phase(tmp_path: Path) -> None:
    # An existing `*BasicLands.kt` registers "Plains" etc.; triage marks every
    # Plains printing AlreadyImplemented, so the basics phase has nothing to do.
    existing = cards_dir(tmp_path, "por") / "BloomburrowBasicLands.kt"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text('val Plains1 = basicLand("Plains") { }\n')

    pipeline = _build_pipeline(
        tmp_path,
        cards=[_basic("Plains", "1"), _basic("Plains", "2")],
        parser=_ScriptedParser(),
    )
    report = pipeline.run()
    assert len(report.already_implemented) == 2
    assert report.emitted_basic_lands is None
    # The existing file is untouched (no second `*BasicLands.kt` written).
    assert existing.read_text() == 'val Plains1 = basicLand("Plains") { }\n'


# ---- report shape ----

def test_all_outcomes_aggregates_every_bucket(tmp_path: Path) -> None:
    target = cards_dir(tmp_path, "por") / "Existing.kt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('val Existing = card("Existing") { }\n')

    flying_card = _flying_bird()
    activated_card = _flying_bird("Activated")
    untemplated_card = _flying_bird("Untemplated")
    existing_card = _flying_bird("Existing")

    pipeline = _build_pipeline(
        tmp_path,
        cards=[existing_card, flying_card, activated_card, untemplated_card],
        parser=_ScriptedParser({
            "Test Bird": ParseResult(ast=_flying_card()),
            "Activated": ParseResult(ast=_activated_card()),
            # "Untemplated" intentionally missing -> parse error
        }),
    )
    report: PipelineReport = pipeline.run()
    assert {type(o).__name__ for o in report.all_outcomes} == {
        "AlreadyImplemented",
        "Emitted",
        "DeferredEmitterGap",
        "DeferredParseFailed",
    }
