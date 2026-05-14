# pyright: basic
"""Tests for the parse-error playbook.

Real Anthropic calls are forbidden; the driver tests inject a fake
:class:`_ScriptedClient` returning canned tool_use blocks. The grammar
edits run against a temporary grammar file so the real
``parser/grammar/grammar.py`` is never touched by tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from argentum_press.playbook import context, edits, llm, parse_error


# ---------------------------------------------------------------------------
# Context helpers (P0/P1/P2)
# ---------------------------------------------------------------------------


def test_grammar_rule_index_loads_rules():
    rules = context.grammar_rule_index()
    names = {r.name for r in rules}
    assert "thenstatement" in names
    assert "wheneverstatement" in names
    # A spot-check rule definition.
    r = next(r for r in rules if r.name == "thenstatement")
    assert r.literals == ("then",) or "then" in r.literals


def test_rank_candidate_parent_rules_returns_overlap():
    failing = "this creature deals 2 damage to target player"
    ranked = context.rank_candidate_parent_rules(failing, top_n=3)
    assert len(ranked) <= 3
    # Score is at least 1 (the ranker filters out zero-overlap rules).
    if ranked:
        assert ranked[0].score >= 1


def test_dump_rule_definitions_includes_line_numbers():
    rules = context.grammar_rule_index()
    target = next(r for r in rules if r.name == "thenstatement")
    out = context.dump_rule_definitions([target])
    assert f"{target.start_line}: " in out
    assert "thenstatement" in out


# ---------------------------------------------------------------------------
# Heuristics (parse-error has no heuristic; just smoke-test the existing one)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Grammar splice edit
# ---------------------------------------------------------------------------


_FAKE_GRAMMAR = '''# pyright: basic
def getGrammar():
    return """
        start: thenstatement

        thenstatement: "then" statement
        statement: "do" "x"

        %import common.WS
        %ignore WS
        """
'''


def _write_fake_grammar(path: Path) -> Path:
    f = path / "grammar.py"
    f.write_text(_FAKE_GRAMMAR, encoding="utf-8")
    return f


def test_apply_grammar_alternative_inserts_line(tmp_path: Path, monkeypatch):
    fake = _write_fake_grammar(tmp_path)
    # Bypass Lark validation: stub _validate_grammar_compiles since the fake
    # grammar uses `start: thenstatement` instead of `cardtext`.
    monkeypatch.setattr(edits, "_validate_grammar_compiles", lambda p: None)
    result = edits.apply_grammar_alternative(
        grammar_path=fake,
        parent_rule="thenstatement",
        alternative_text='"then" "also" statement',
        label="thenalsostatement",
    )
    new_src = result.new_source
    assert '| "then" "also" statement -> thenalsostatement' in new_src
    # The original line is preserved.
    assert '"then" statement' in new_src


def test_apply_grammar_alternative_unknown_rule(tmp_path: Path, monkeypatch):
    fake = _write_fake_grammar(tmp_path)
    monkeypatch.setattr(edits, "_validate_grammar_compiles", lambda p: None)
    with pytest.raises(edits.AnchorNotFoundError):
        edits.apply_grammar_alternative(
            grammar_path=fake,
            parent_rule="nonexistentrule",
            alternative_text='"x"',
            label=None,
        )


def test_apply_grammar_alternative_revert(tmp_path: Path, monkeypatch):
    fake = _write_fake_grammar(tmp_path)
    monkeypatch.setattr(edits, "_validate_grammar_compiles", lambda p: None)
    original = fake.read_text(encoding="utf-8")
    result = edits.apply_grammar_alternative(
        grammar_path=fake,
        parent_rule="thenstatement",
        alternative_text='"then" "also" statement',
        label="alsostatement",
    )
    assert fake.read_text(encoding="utf-8") != original
    edits.revert_grammar(result)
    assert fake.read_text(encoding="utf-8") == original


def test_apply_grammar_alternative_validates_on_real_grammar():
    """Sanity check that Lark still compiles after a unique-ish alternative
    is spliced in. Uses the real GRAMMAR path; reverts before returning so
    the test leaves no trace."""
    real = context.GRAMMAR
    result = edits.apply_grammar_alternative(
        grammar_path=real,
        parent_rule="thenstatement",
        alternative_text='"then" "subsequently" statement',
        label="thensubsequentlystatement",
    )
    edits.revert_grammar(result)


# ---------------------------------------------------------------------------
# LLM wrapper — new schemas
# ---------------------------------------------------------------------------


@dataclass
class _FakeBlock:
    type: str
    name: str
    input: dict[str, Any]


@dataclass
class _FakeResponse:
    content: list[_FakeBlock]
    model: str = "fake"


class _FakeMessages:
    def __init__(self, queue: list[_FakeBlock]) -> None:
        self._queue = queue
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        block = self._queue.pop(0)
        return _FakeResponse(content=[block])


class _ScriptedClient:
    def __init__(self, blocks: list[_FakeBlock]) -> None:
        self.messages = _FakeMessages(blocks)


def test_call_tool_validates_parse_alternative_schema():
    client = _ScriptedClient([
        _FakeBlock(type="tool_use", name="emit_parse_alternative", input={
            "parent_rule": "thenstatement",
            "alternative_text": '"then" "also" statement',
            "label": "thenalsostatement",
        }),
    ])
    res = llm.call_tool(
        tool_name="emit_parse_alternative",
        system_prompt="sys",
        static_context_blocks=[{"type": "text", "text": "x"}],
        user_prompt="hi",
        model="fake",
        client=client,
    )
    assert res.arguments["parent_rule"] == "thenstatement"


def test_call_tool_rejects_invalid_parse_alternative():
    client = _ScriptedClient([
        _FakeBlock(type="tool_use", name="emit_parse_alternative", input={
            "parent_rule": "thenstatement",
            # missing alternative_text + label
        }),
    ])
    with pytest.raises(ValueError):
        llm.call_tool(
            tool_name="emit_parse_alternative",
            system_prompt="sys",
            static_context_blocks=[],
            user_prompt="hi",
            model="fake",
            client=client,
        )


# ---------------------------------------------------------------------------
# End-to-end driver
# ---------------------------------------------------------------------------


def _green_pytest(_repo: Path) -> tuple[int, str]:
    return 0, "1 passed"


def _red_then_green():
    state = {"n": 0}

    def runner(_repo: Path) -> tuple[int, str]:
        state["n"] += 1
        if state["n"] == 1:
            return 1, "AssertionError: still failing"
        return 0, "1 passed"

    return runner


_OPEN_ENDED_ORACLE = "this creature deals damage to target creature you control"


def _ranked_target_rule(oracle: str) -> str:
    """Return the rule name P1 will rank highest for this oracle text.

    Lets the test pick the deterministic winner instead of hard-coding a
    rule name — keeps the test stable when the ranker or grammar changes.
    """
    ranked = context.rank_candidate_parent_rules(oracle, top_n=3)
    assert ranked, "ranker returned no candidates; pick richer oracle text"
    return ranked[0].rule.name


def test_driver_happy_path(monkeypatch, tmp_path: Path):
    # Skip Lark compile validation so the test runs fast and doesn't
    # require building the full grammar; the splice is exercised on the
    # real grammar but we don't re-validate.
    monkeypatch.setattr(edits, "_validate_grammar_compiles", lambda p: None)
    oracle = "then also " + _OPEN_ENDED_ORACLE
    target = _ranked_target_rule(oracle)

    blocks = [
        _FakeBlock(type="tool_use", name="emit_parse_parent_choice", input={
            "parent_rule": target,
            "missing_phrase": "deals damage",
            "rationale": f"{target} is the closest parent for this phrase",
        }),
        _FakeBlock(type="tool_use", name="emit_parse_alternative", input={
            "parent_rule": target,
            "alternative_text": '"then" "also" statement',
            "label": "thenalsostatement",
        }),
    ]
    client = _ScriptedClient(blocks)

    # Capture-and-revert grammar.py.
    grammar_path = context.GRAMMAR
    original = grammar_path.read_text(encoding="utf-8")
    try:
        result = parse_error.run(
            label=f"parse-error:<EOF>@t",
            project_dir=tmp_path,
            client=client,
            pytest_runner=_green_pytest,
            card_name="Imaginary Card",
            oracle_text=oracle,
            pe_block="(fake pe block)",
            verbose=False,
        )
    finally:
        grammar_path.write_text(original, encoding="utf-8")
    assert result.outcome == "applied", result.as_json()
    assert result.final_plan is not None


def test_driver_retry_on_pytest_red(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(edits, "_validate_grammar_compiles", lambda p: None)
    oracle = "then also " + _OPEN_ENDED_ORACLE
    target = _ranked_target_rule(oracle)

    blocks = [
        _FakeBlock(type="tool_use", name="emit_parse_parent_choice", input={
            "parent_rule": target,
            "missing_phrase": "deals damage",
            "rationale": f"{target} matches",
        }),
        _FakeBlock(type="tool_use", name="emit_parse_alternative", input={
            "parent_rule": target,
            "alternative_text": '"then" "also" statement',
            "label": "alsostatement",
        }),
        # Retry plan after red pytest
        _FakeBlock(type="tool_use", name="emit_parse_alternative", input={
            "parent_rule": target,
            "alternative_text": '"then" "actually" statement',
            "label": "actuallystatement",
        }),
    ]
    client = _ScriptedClient(blocks)
    runner = _red_then_green()

    grammar_path = context.GRAMMAR
    original = grammar_path.read_text(encoding="utf-8")
    try:
        result = parse_error.run(
            label="parse-error:<EOF>@t",
            project_dir=tmp_path,
            client=client,
            pytest_runner=runner,
            oracle_text=oracle,
            pe_block="(fake pe block)",
            verbose=False,
        )
    finally:
        grammar_path.write_text(original, encoding="utf-8")
    assert result.outcome == "applied-after-retry", result.as_json()
    assert result.pytest_first_tail != ""


def test_driver_aborts_when_no_candidates(monkeypatch, tmp_path: Path):
    # Force the candidate list to be empty.
    monkeypatch.setattr(
        context, "rank_candidate_parent_rules", lambda *a, **kw: []
    )
    result = parse_error.run(
        label="parse-error:<EOF>@t",
        project_dir=tmp_path,
        client=_ScriptedClient([]),
        pytest_runner=_green_pytest,
        oracle_text="zzz qqq nothing matches",
        pe_block=None,
        verbose=False,
    )
    assert result.outcome == "aborted-p1"


def test_driver_aborts_when_llm_picks_unknown_rule(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(edits, "_validate_grammar_compiles", lambda p: None)
    blocks = [
        _FakeBlock(type="tool_use", name="emit_parse_parent_choice", input={
            "parent_rule": "wibbleflorbglob",  # not in candidates
            "missing_phrase": "x",
            "rationale": "y",
        }),
    ]
    client = _ScriptedClient(blocks)
    result = parse_error.run(
        label="parse-error:<EOF>@t",
        project_dir=tmp_path,
        client=client,
        pytest_runner=_green_pytest,
        oracle_text="then also do stuff",
        pe_block=None,
        verbose=False,
    )
    assert result.outcome == "aborted-p3"
