"""Pipeline tests stub mtgcompiler, the catalog, and the verifier so we can
drive the orchestration without external state. Each test exercises one of
the four phases (triage / classify / emit / verify)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argentum_press import _ast as ast
from argentum_press.existing import cards_dir
from argentum_press.lowerer import KotlinLowerer
from argentum_press.outcome import (
    AlreadyImplemented,
    DeferredEmitterGap,
    DeferredParseFailed,
    Emitted,
)
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


@dataclass
class _ScriptedParser:
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
    activated = ast.Card(
        (ast.ActivatedAbility((ast.TapSelf(),), (ast.DrawCards(1),)),)
    )
    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird("Activated")],
        parser=_ScriptedParser({"Activated": ast.ParseResult(ast=activated)}),
    )
    report = pipeline.run()
    assert len(report.bucket_2) == 1
    assert report.bucket_2[0].name == "Activated"
    assert "ActivatedAbility" in report.bucket_2[0].missing_node
    assert report.emitted == ()


# ---- emit ----

def test_bucket_1_is_written_at_argentum_path(tmp_path: Path) -> None:
    flying_ast = ast.Card((ast.KeywordAbility(ast.Keyword.FLYING),))
    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird()],
        parser=_ScriptedParser({"Test Bird": ast.ParseResult(ast=flying_ast)}),
    )
    report = pipeline.run()
    assert len(report.emitted) == 1
    expected = cards_dir(tmp_path, "por") / "TestBird.kt"
    assert report.emitted[0].path == expected
    assert expected.exists()
    assert "keywords(Keyword.FLYING)" in expected.read_text()


# ---- verify (phase 4 is a stub) ----

def test_phase_4_skipped_when_no_verifier(tmp_path: Path) -> None:
    flying_ast = ast.Card((ast.KeywordAbility(ast.Keyword.FLYING),))
    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird()],
        parser=_ScriptedParser({"Test Bird": ast.ParseResult(ast=flying_ast)}),
        verifier=None,
    )
    report = pipeline.run()
    assert report.compile_stderr is None


def test_phase_4_skipped_when_nothing_was_emitted(tmp_path: Path) -> None:
    # Bucket-2 only; phase 4 doesn't run.
    activated = ast.Card(
        (ast.ActivatedAbility((ast.TapSelf(),), (ast.DrawCards(1),)),)
    )
    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird("Activated")],
        parser=_ScriptedParser({"Activated": ast.ParseResult(ast=activated)}),
        verifier=_AlwaysFailVerifier(stderr="should not run"),
    )
    report = pipeline.run()
    assert report.compile_stderr is None


def test_phase_4_records_compile_stderr_without_crashing(tmp_path: Path) -> None:
    flying_ast = ast.Card((ast.KeywordAbility(ast.Keyword.FLYING),))
    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird()],
        parser=_ScriptedParser({"Test Bird": ast.ParseResult(ast=flying_ast)}),
        verifier=_AlwaysFailVerifier(stderr="kotlinc: unresolved reference: Effects.Wat"),
    )
    report = pipeline.run()
    assert report.compile_stderr is not None
    assert "unresolved reference" in report.compile_stderr
    # The file is still on disk; we report the failure, we don't roll back.
    assert len(report.emitted) == 1


def test_phase_4_compile_ok_leaves_stderr_unset(tmp_path: Path) -> None:
    flying_ast = ast.Card((ast.KeywordAbility(ast.Keyword.FLYING),))
    pipeline = _build_pipeline(
        tmp_path,
        cards=[_flying_bird()],
        parser=_ScriptedParser({"Test Bird": ast.ParseResult(ast=flying_ast)}),
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
            c["name"]: ast.ParseResult(
                ast=ast.Card((ast.KeywordAbility(ast.Keyword.FLYING),))
            )
            for c in cards
        }),
    )
    report = pipeline.run(limit=2)
    assert len(report.emitted) == 2


# ---- report shape ----

def test_all_outcomes_aggregates_every_bucket(tmp_path: Path) -> None:
    target = cards_dir(tmp_path, "por") / "Existing.kt"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('val Existing = card("Existing") { }\n')

    flying_card = _flying_bird()
    activated_card = _flying_bird("Activated")
    untemplated_card = _flying_bird("Untemplated")
    existing_card = _flying_bird("Existing")
    activated_ast = ast.Card(
        (ast.ActivatedAbility((ast.TapSelf(),), (ast.DrawCards(1),)),)
    )

    pipeline = _build_pipeline(
        tmp_path,
        cards=[existing_card, flying_card, activated_card, untemplated_card],
        parser=_ScriptedParser({
            "Test Bird": ast.ParseResult(
                ast=ast.Card((ast.KeywordAbility(ast.Keyword.FLYING),))
            ),
            "Activated": ast.ParseResult(ast=activated_ast),
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
