"""Tests for the prompts/ template loader (Phase 3 scaffolding).

The baseline templates are supposed to render byte-identically to the
pre-refactor inline output. We don't have the old function to compare
against, so the tests here lock in a known-good rendering: small synthetic
GapContexts whose expected output is checked against substrings we know
must appear (and not appear) in each kind.

Variant lookup raises on unknown names and missing files so a typo in
the user's --prompt-variant flag fails loudly instead of silently
falling back to baseline.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import fix_parser_gaps as flp  # noqa: E402


def _ctx(kind: str, label: str) -> flp.GapContext:
    return flp.GapContext(
        set_code="test",
        project_dir=Path("/tmp"),
        kind=kind,
        label=label,
        card_name="Sample Card",
        oracle_text="Sample oracle text.\nMulti-line.",
        preprocessed_text=None,
        parse_error_block="  parse error detail",
        ast_block="ast block content",
        gap_class_def="  class Sample: ...",
        rule_def="rule: A | B",
        rule_uses="  line 7: bar -> rule",
        grammar_index_excerpt="  line 1: foo",
        handler_map="  @disp.register",
        engine_hints="  matching kotlin",
        file_sizes="  grammar.py: 1 line",
        recent_commits="abcd1234 commit subject",
    )


def test_baseline_lower_includes_handler_map_and_ast_block() -> None:
    out = flp._render_prompt_variant("baseline", _ctx("lower", "argentum.foo.Bar"))
    assert "Fix one lowerer gap" in out
    assert "GAP  kind=lower  label=argentum.foo.Bar" in out
    assert "HANDLER MAP" in out
    assert "ENGINE DSL HINTS" in out
    assert "PARSED AST FOR THIS CARD" in out
    # The shared tail must be there.
    assert "FILES YOU MAY EDIT" in out
    assert "WORKFLOW" in out


def test_baseline_parse_error_includes_parse_error_block() -> None:
    out = flp._render_prompt_variant(
        "baseline", _ctx("parse", "parse-error:unexpected token"),
    )
    assert "Fix one grammar gap" in out
    assert "PARSE ERROR DETAIL" in out
    assert "parse error detail" in out
    # The unmodeled-only "RULE DEFINITION" header must NOT appear.
    assert "GRAMMAR RULE DEFINITION" not in out


def test_baseline_unmodeled_includes_rule_def() -> None:
    out = flp._render_prompt_variant(
        "baseline", _ctx("parse", "unmodeled-rule:foo"),
    )
    assert "Fix one transformer gap" in out
    assert "GRAMMAR RULE DEFINITION" in out
    assert "rule: A | B" in out
    # parse-error-only block must NOT appear.
    assert "PARSE ERROR DETAIL" not in out


def test_oracle_text_is_indented_four_spaces() -> None:
    """The baseline output indents the oracle text by exactly four spaces."""
    out = flp._render_prompt_variant("baseline", _ctx("lower", "argentum.X"))
    assert "    Sample oracle text." in out
    assert "    Multi-line." in out


def test_unknown_variant_raises() -> None:
    with pytest.raises(FileNotFoundError):
        flp._render_prompt_variant("nonexistent-variant", _ctx("lower", "X"))


def test_missing_placeholder_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo'd placeholder in a hand-edited variant must surface as a
    KeyError at render time — silent passthrough would paste {{rule_def}}
    into the agent's prompt verbatim and corrupt the experiment."""
    variant_dir = tmp_path / "broken"
    variant_dir.mkdir(parents=True)
    (variant_dir / "lower.md").write_text(
        "Body with {{nonexistent_placeholder}}.\n", encoding="utf-8"
    )
    (variant_dir / "_common_tail.md").write_text("tail\n", encoding="utf-8")
    monkeypatch.setattr(flp, "PROMPTS_DIR", tmp_path)

    with pytest.raises(KeyError):
        flp._render_prompt_variant("broken", _ctx("lower", "X"))


def test_render_prompt_default_uses_baseline() -> None:
    """The legacy ``render_prompt`` entrypoint is preserved and routes to the
    baseline variant — main() and dry-run callers should keep working."""
    ctx = _ctx("lower", "argentum.X")
    a = flp.render_prompt(ctx)
    b = flp._render_prompt_variant("baseline", ctx)
    assert a == b


def test_dry_run_default_variant_smoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--dry-run rendering against the default variant should not crash and
    should include the baseline tail boilerplate. We stub out the gap
    subprocess so the test doesn't fetch from Scryfall."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("ARGENTUM_PARSE_CACHE_DIR", str(tmp_path / "pcache"))

    def fake_find(set_code: str, project_dir: Path, *,
                  scan_jsonl_path: Path | None = None) -> tuple:
        from argentum_press.diagnose import Gap
        return (
            Gap(
                kind="lower",
                label="argentum_press.parser.ast.foo.Bar",
                card_name="Test Card",
                oracle_text="Flying.",
                parse_details=None,
            ),
            None,
            None,
        )

    monkeypatch.setattr(flp, "_find_gap_subprocess", fake_find)

    rc = flp.main(["spm", "/tmp", "--dry-run", "--allow-dirty"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Fix one lowerer gap" in out
    assert "FILES YOU MAY EDIT" in out
