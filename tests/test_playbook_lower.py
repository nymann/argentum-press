# pyright: basic
"""Tests for the lower-gap playbook.

Real Anthropic calls are forbidden in pytest; the end-to-end driver test
injects a hand-coded fake client that returns canned tool_use blocks
matching the schemas defined in :mod:`argentum_press.playbook.llm`.
"""
from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from argentum_press.playbook import cache, context, edits, heuristics, llm, lower


# ---------------------------------------------------------------------------
# L0 — extract_ast_class
# ---------------------------------------------------------------------------


def test_extract_ast_class_finds_atstatement():
    info = context.extract_ast_class("argentum_press.parser.ast.statements.AtStatement")
    assert info is not None
    assert info.classname == "AtStatement"
    assert info.parent_module == "statements"
    names = {n for n, _ in info.fields}
    assert {"conditional", "consequence", "inverted"} <= names


def test_extract_ast_class_unknown_returns_none():
    info = context.extract_ast_class("argentum_press.parser.ast.statements.NopeStatement")
    assert info is None


# ---------------------------------------------------------------------------
# L1 — collect_lowerer_exemplars
# ---------------------------------------------------------------------------


def test_collect_lowerer_exemplars_yields_both_patterns():
    ex = context.collect_lowerer_exemplars()
    # The lowerer has dozens of @ability.register clauses and a healthy set
    # of isinstance branches inside _effects_from_statement.
    assert len(ex.register_handlers) > 20
    assert len(ex.isinstance_branches) > 5
    fns = {b.function for b in ex.isinstance_branches}
    assert "_effects_from_statement" in fns


# ---------------------------------------------------------------------------
# L5a — heuristic pattern picker
# ---------------------------------------------------------------------------


def test_heuristic_picks_isinstance_for_existing_statement_branch():
    ex = context.collect_lowerer_exemplars()
    # UntilStatement already has an isinstance branch in _effects_from_statement.
    choice = heuristics.pick_pattern("UntilStatement", ex)
    assert choice.pattern == "isinstance-branch"
    assert choice.confidence >= 0.9


def test_heuristic_picks_register_for_ability_suffix():
    ex = context.collect_lowerer_exemplars()
    choice = heuristics.pick_pattern("NewlyAddedAbility", ex)
    assert choice.pattern == "register-handler"
    assert choice.confidence >= 0.8


def test_heuristic_falls_back_low_confidence_for_unknown_shape():
    ex = context.collect_lowerer_exemplars()
    choice = heuristics.pick_pattern("StrangeBag", ex)
    assert choice.pattern == "register-handler"
    assert choice.confidence < 0.5


# ---------------------------------------------------------------------------
# L3 — disk cache
# ---------------------------------------------------------------------------


def test_l3_cache_roundtrip(tmp_path: Path):
    src = "class Foo:\n    pass\n"
    summary = {"summary": "x", "mtg_term": "y", "similar_handlers": []}
    assert cache.get(src, root=tmp_path) is None
    cache.put(src, summary, root=tmp_path)
    assert cache.get(src, root=tmp_path) == summary


def test_l3_cache_miss_on_different_source(tmp_path: Path):
    cache.put("class A: pass\n", {"summary": "a", "mtg_term": "", "similar_handlers": []}, root=tmp_path)
    assert cache.get("class B: pass\n", root=tmp_path) is None


# ---------------------------------------------------------------------------
# L6 — edits.apply_plan
# ---------------------------------------------------------------------------


_FAKE_LOWERER = '''"""Fake mini-lowerer for edit tests."""
from functools import singledispatchmethod


class EmitterGap(RuntimeError):
    pass


class KotlinLowerer:
    @singledispatchmethod
    def ability(self, ability):
        raise EmitterGap(ability)

    def _effects_from_statement(self, stmt):
        if isinstance(stmt, str):
            return ("str",)
        raise EmitterGap(stmt)
'''


def _write_fake(path: Path) -> Path:
    f = path / "lowerer.py"
    f.write_text(_FAKE_LOWERER, encoding="utf-8")
    return f


def test_apply_plan_register_handler(tmp_path: Path):
    fake = _write_fake(tmp_path)
    plan = {
        "anchor": {"pattern": "register-handler", "dispatcher": "ability"},
        "body_python": (
            "@ability.register\n"
            "def _(self, ability: int) -> str:\n"
            "    return 'int-ability'\n"
        ),
        "new_imports": [],
    }
    result = edits.apply_plan(plan, fake)
    new_src = result.new_source
    assert "def _(self, ability: int) -> str:" in new_src
    assert "@ability.register" in new_src
    # The new handler should land inside KotlinLowerer.
    assert new_src.index("class KotlinLowerer") < new_src.index("def _(self, ability: int)")


def test_apply_plan_isinstance_branch(tmp_path: Path):
    fake = _write_fake(tmp_path)
    plan = {
        "anchor": {"pattern": "isinstance-branch", "function": "_effects_from_statement"},
        "body_python": (
            "if isinstance(stmt, int):\n"
            "    return ('int',)\n"
        ),
        "new_imports": [],
    }
    result = edits.apply_plan(plan, fake)
    new_src = result.new_source
    # New branch lands before the raise.
    int_idx = new_src.index("isinstance(stmt, int)")
    raise_idx = new_src.index("raise EmitterGap(stmt)")
    assert int_idx < raise_idx


def test_apply_plan_unknown_pattern_raises(tmp_path: Path):
    fake = _write_fake(tmp_path)
    with pytest.raises(edits.AnchorNotFoundError):
        edits.apply_plan(
            {"anchor": {"pattern": "weird"}, "body_python": "", "new_imports": []},
            fake,
        )


def test_apply_plan_unknown_function_raises(tmp_path: Path):
    fake = _write_fake(tmp_path)
    with pytest.raises(edits.AnchorNotFoundError):
        edits.apply_plan(
            {
                "anchor": {"pattern": "isinstance-branch", "function": "_no_such_helper"},
                "body_python": "if isinstance(stmt, int):\n    return ('int',)\n",
                "new_imports": [],
            },
            fake,
        )


def test_apply_plan_adds_new_imports(tmp_path: Path):
    fake = _write_fake(tmp_path)
    plan = {
        "anchor": {"pattern": "register-handler", "dispatcher": "ability"},
        "body_python": "@ability.register\ndef _(self, ability: float) -> str:\n    return 'f'\n",
        "new_imports": ["from math import pi"],
    }
    result = edits.apply_plan(plan, fake)
    assert "from math import pi" in result.new_source


def test_apply_plan_revert(tmp_path: Path):
    fake = _write_fake(tmp_path)
    original = fake.read_text(encoding="utf-8")
    plan = {
        "anchor": {"pattern": "register-handler", "dispatcher": "ability"},
        "body_python": "@ability.register\ndef _(self, ability: int) -> str:\n    return 'i'\n",
        "new_imports": [],
    }
    result = edits.apply_plan(plan, fake)
    assert fake.read_text(encoding="utf-8") != original
    edits.revert(result)
    assert fake.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# LLM wrapper — schema validation + fake client
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


class _FakeClient:
    def __init__(self, queue: list[_FakeBlock]) -> None:
        self.messages = _FakeMessages(queue)


def test_call_tool_validates_schema():
    client = _FakeClient([
        _FakeBlock(type="tool_use", name="emit_ast_summary", input={
            "summary": "Foo", "mtg_term": "bar", "similar_handlers": ["a"],
        }),
    ])
    res = llm.call_tool(
        tool_name="emit_ast_summary",
        system_prompt="sys",
        static_context_blocks=[{"type": "text", "text": "x"}],
        user_prompt="hi",
        model="fake",
        client=client,
    )
    assert res.arguments["summary"] == "Foo"


def test_call_tool_rejects_invalid_args():
    client = _FakeClient([
        _FakeBlock(type="tool_use", name="emit_ast_summary", input={
            "summary": "Foo",  # missing required fields
        }),
    ])
    with pytest.raises(ValueError):
        llm.call_tool(
            tool_name="emit_ast_summary",
            system_prompt="sys",
            static_context_blocks=[],
            user_prompt="hi",
            model="fake",
            client=client,
        )


def test_call_tool_missing_tool_use_raises():
    client = _FakeClient([
        _FakeBlock(type="text", name="", input={}),  # not a tool_use
    ])
    with pytest.raises(ValueError):
        llm.call_tool(
            tool_name="emit_ast_summary",
            system_prompt="sys",
            static_context_blocks=[],
            user_prompt="hi",
            model="fake",
            client=client,
        )


# ---------------------------------------------------------------------------
# End-to-end driver with mocked LLM + mocked pytest
# ---------------------------------------------------------------------------


class _ScriptedClient:
    """Replays a queue of tool_use blocks in call order."""

    def __init__(self, blocks: list[_FakeBlock]) -> None:
        self.messages = _FakeMessages(blocks)


def _green_pytest(_repo: Path) -> tuple[int, str]:
    return 0, "1 passed"


def _red_then_green() -> Any:
    state = {"n": 0}
    def runner(_repo: Path) -> tuple[int, str]:
        state["n"] += 1
        if state["n"] == 1:
            return 1, "AssertionError: something\n... long pytest output ..."
        return 0, "1 passed"
    return runner


def test_driver_happy_path_register_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # Use a class that L5a picks register-handler for. AwakenAbility ends in
    # Ability and isn't in any isinstance branch.
    blocks = [
        _FakeBlock(type="tool_use", name="emit_ast_summary", input={
            "summary": "Awaken N", "mtg_term": "awaken", "similar_handlers": ["EnchantAbility"],
        }),
        _FakeBlock(type="tool_use", name="emit_strategy", input={
            "strategy": "stub", "target_dsl_symbol": "Effects.Awaken",
            "justification": "no engine surface yet",
        }),
        _FakeBlock(type="tool_use", name="emit_plan", input={
            "anchor": {"pattern": "register-handler", "dispatcher": "ability"},
            "body_python": (
                "@ability.register\n"
                "def _(self, ability: ast.AwakenAbility) -> str:\n"
                "    raise EmitterGap(ability)\n"
            ),
            "new_imports": [],
        }),
    ]
    client = _ScriptedClient(blocks)

    # Capture-and-revert the lowerer file so we don't actually leave it edited.
    lowerer_path = context.LOWERER
    original_text = lowerer_path.read_text(encoding="utf-8")
    try:
        result = lower.run(
            label="argentum_press.parser.ast.abilities.AwakenAbility",
            project_dir=tmp_path,  # no engine repo needed for the test
            client=client,
            pytest_runner=_green_pytest,
            cache_root=tmp_path / "cache",
            verbose=False,
        )
    finally:
        lowerer_path.write_text(original_text, encoding="utf-8")
    assert result.outcome == "applied", result.as_json()
    assert result.final_plan is not None


def test_driver_retry_on_pytest_red(tmp_path: Path):
    blocks = [
        _FakeBlock(type="tool_use", name="emit_ast_summary", input={
            "summary": "Awaken N", "mtg_term": "awaken", "similar_handlers": [],
        }),
        _FakeBlock(type="tool_use", name="emit_strategy", input={
            "strategy": "stub", "target_dsl_symbol": "Effects.Awaken",
            "justification": "no engine surface yet",
        }),
        _FakeBlock(type="tool_use", name="emit_plan", input={
            "anchor": {"pattern": "register-handler", "dispatcher": "ability"},
            "body_python": (
                "@ability.register\n"
                "def _(self, ability: ast.AwakenAbility) -> str:\n"
                "    raise EmitterGap(ability)\n"
            ),
            "new_imports": [],
        }),
        # L9 revised plan after the first pytest fails.
        _FakeBlock(type="tool_use", name="emit_plan", input={
            "anchor": {"pattern": "register-handler", "dispatcher": "ability"},
            "body_python": (
                "@ability.register\n"
                "def _(self, ability: ast.AwakenAbility) -> str:\n"
                "    return 'Effects.Awaken()'\n"
            ),
            "new_imports": [],
        }),
    ]
    client = _ScriptedClient(blocks)
    runner = _red_then_green()

    lowerer_path = context.LOWERER
    original_text = lowerer_path.read_text(encoding="utf-8")
    try:
        result = lower.run(
            label="argentum_press.parser.ast.abilities.AwakenAbility",
            project_dir=tmp_path,
            client=client,
            pytest_runner=runner,
            cache_root=tmp_path / "cache",
            verbose=False,
        )
    finally:
        lowerer_path.write_text(original_text, encoding="utf-8")
    assert result.outcome == "applied-after-retry", result.as_json()
    assert result.pytest_first_tail != ""


def test_driver_aborts_when_ast_class_unknown(tmp_path: Path):
    result = lower.run(
        label="argentum_press.parser.ast.statements.NopeStatement",
        project_dir=tmp_path,
        client=_ScriptedClient([]),
        pytest_runner=_green_pytest,
        cache_root=tmp_path / "cache",
        verbose=False,
    )
    assert result.outcome == "aborted-l0"


def test_driver_aborts_on_libcst_failure(tmp_path: Path):
    blocks = [
        _FakeBlock(type="tool_use", name="emit_ast_summary", input={
            "summary": "x", "mtg_term": "x", "similar_handlers": [],
        }),
        _FakeBlock(type="tool_use", name="emit_strategy", input={
            "strategy": "stub", "target_dsl_symbol": "Effects.Awaken",
            "justification": "x",
        }),
        _FakeBlock(type="tool_use", name="emit_plan", input={
            "anchor": {"pattern": "isinstance-branch", "function": "no_such_helper_anywhere"},
            "body_python": "if isinstance(stmt, int):\n    return ('x',)\n",
            "new_imports": [],
        }),
    ]
    client = _ScriptedClient(blocks)
    lowerer_path = context.LOWERER
    original_text = lowerer_path.read_text(encoding="utf-8")
    try:
        result = lower.run(
            label="argentum_press.parser.ast.abilities.AwakenAbility",
            project_dir=tmp_path,
            client=client,
            pytest_runner=_green_pytest,
            cache_root=tmp_path / "cache",
            verbose=False,
        )
    finally:
        lowerer_path.write_text(original_text, encoding="utf-8")
    assert result.outcome == "aborted-l6"


# ---------------------------------------------------------------------------
# L7b live-classify gate (catches pytest-green-but-live-card-still-broken)
# ---------------------------------------------------------------------------


def test_driver_aborts_when_live_classify_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # pytest passes (green runner) but the originating card's live
    # classify still produces the same lower:<class> gap label. The
    # gate must revert the lowerer edit and abort with
    # aborted-classify-unchanged so the strategy chain can fall back
    # to freeform.
    monkeypatch.setattr(
        lower, "_live_card_still_failing_lower", lambda **_kw: True
    )

    blocks = [
        _FakeBlock(type="tool_use", name="emit_ast_summary", input={
            "summary": "Awaken N", "mtg_term": "awaken", "similar_handlers": [],
        }),
        _FakeBlock(type="tool_use", name="emit_strategy", input={
            "strategy": "stub", "target_dsl_symbol": "Effects.Awaken",
            "justification": "no engine surface yet",
        }),
        _FakeBlock(type="tool_use", name="emit_plan", input={
            "anchor": {"pattern": "register-handler", "dispatcher": "ability"},
            "body_python": (
                "@ability.register\n"
                "def _(self, ability: ast.AwakenAbility) -> str:\n"
                "    raise EmitterGap(ability)\n"
            ),
            "new_imports": [],
        }),
    ]
    client = _ScriptedClient(blocks)

    lowerer_path = context.LOWERER
    original_text = lowerer_path.read_text(encoding="utf-8")
    try:
        result = lower.run(
            label="argentum_press.parser.ast.abilities.AwakenAbility",
            project_dir=tmp_path,
            client=client,
            pytest_runner=_green_pytest,
            cache_root=tmp_path / "cache",
            card_name="Fake Card",
            oracle_text="Awaken 2.",
            verbose=False,
        )
    finally:
        # Defensive restore: the gate should have reverted via edits.revert,
        # but if a test failure interrupted the flow before that point the
        # tree shouldn't be left dirty.
        lowerer_path.write_text(original_text, encoding="utf-8")
    assert result.outcome == "aborted-classify-unchanged", result.as_json()
    # The lowerer must end up unchanged from where it started — the gate
    # is responsible for reverting before returning.
    assert lowerer_path.read_text(encoding="utf-8") == original_text


def test_live_classify_helper_noop_when_card_data_missing():
    # No card_name + oracle_text → the helper should return False
    # (no work to do, defaults to "progress made"). Keeps the existing
    # happy-path tests from spawning subprocesses unnecessarily.
    assert lower._live_card_still_failing_lower(
        card_name="", oracle_text="", label="some.label",
    ) is False
    assert lower._live_card_still_failing_lower(
        card_name="Fake", oracle_text="", label="some.label",
    ) is False
    assert lower._live_card_still_failing_lower(
        card_name="", oracle_text="text", label="some.label",
    ) is False
