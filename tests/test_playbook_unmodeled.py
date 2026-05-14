# pyright: basic
"""Tests for the unmodeled-rule playbook.

Mirrors the parse-error tests: scripted LLM client, real context helpers
exercised against the live AST tree, and edit application sandboxed in
``tmp_path`` so the real parser/ast/* files are never mutated by tests.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from argentum_press.playbook import context, edits, heuristics, llm, unmodeled_rule


# ---------------------------------------------------------------------------
# Context (U0/U1/U2)
# ---------------------------------------------------------------------------


def test_list_ast_modules_returns_known_modules():
    mods = context.list_ast_modules()
    names = {m.module for m in mods}
    assert "statements" in names
    assert "expressions" in names
    assert "abilities" in names
    # statements.py defines a long list; pick one that should be there.
    statements = next(m for m in mods if m.module == "statements")
    assert "AtStatement" in statements.class_names


def test_grammar_rule_block_finds_known_rule():
    r = context.grammar_rule_block("thenstatement")
    assert r is not None
    assert r.name == "thenstatement"
    assert r.start_line > 0


def test_grammar_rule_block_resolves_alias_to_parent_rule():
    # `castbyalternatecost` is an `-> alias` of an alternative inside the
    # `castmodifier` rule, not a top-level rule. Lark emits Trees named after
    # the alias, so the transformer needs a per-alias handler; U0 must surface
    # the parent rule for the LLM.
    r = context.grammar_rule_block("castbyalternatecost")
    assert r is not None
    assert r.name == "castbyalternatecost"
    assert "castmodifier:" in r.source
    assert "-> castbyalternatecost" in r.source


def test_gather_unmodeled_rule_for_alias_label():
    ctx = context.gather_unmodeled_rule(
        label="unmodeled-rule:castbyalternatecost",
        project_dir=Path("/tmp"),
        oracle_text="",
    )
    assert ctx.rule_name == "castbyalternatecost"
    assert ctx.rule is not None
    assert "castmodifier:" in ctx.rule.source


def test_recent_transformer_exemplars_smoke():
    # The git log should have at least one `parser: handle <rule>` commit
    # in recent history.
    ex = context.recent_transformer_exemplars(limit=2)
    # We don't assert on contents because the git log is environment-
    # dependent; we just want the call to not blow up + return a list.
    assert isinstance(ex, list)


def test_gather_unmodeled_rule_packs_everything():
    ctx = context.gather_unmodeled_rule(
        label="unmodeled-rule:thenstatement",
        project_dir=Path("/tmp"),
        oracle_text="x",
    )
    assert ctx.rule_name == "thenstatement"
    assert ctx.rule is not None
    assert len(ctx.ast_modules) > 0


# ---------------------------------------------------------------------------
# Heuristics (U2b — parent-module picker)
# ---------------------------------------------------------------------------


def test_pick_parent_module_statement_suffix():
    choice = heuristics.pick_parent_module("becomesstatement")
    assert choice.module == "statements"
    assert choice.confidence >= 0.8


def test_pick_parent_module_expression_suffix():
    choice = heuristics.pick_parent_module("dealsdamageexpression")
    assert choice.module == "expressions"
    assert choice.confidence >= 0.8


def test_pick_parent_module_ability_suffix():
    choice = heuristics.pick_parent_module("awakenability")
    assert choice.module == "abilities"
    assert choice.confidence >= 0.8


def test_pick_parent_module_unknown_falls_back():
    choice = heuristics.pick_parent_module("foobar")
    assert choice.module == "expressions"
    assert choice.confidence < 0.5


# ---------------------------------------------------------------------------
# Edits — U5 multi-file writer (against a temp ast copy)
# ---------------------------------------------------------------------------


def _make_sandbox(tmp_path: Path) -> tuple[Path, Path, Path]:
    ast_dir = tmp_path / "ast"
    shutil.copytree(context.AST_DIR, ast_dir)
    ast_init = ast_dir / "__init__.py"
    transformer = tmp_path / "transformer.py"
    shutil.copy(context.TRANSFORMER, transformer)
    return ast_dir, ast_init, transformer


def test_apply_unmodeled_rule_writes_three_files(tmp_path: Path):
    ast_dir, ast_init, transformer = _make_sandbox(tmp_path)
    result = edits.apply_unmodeled_rule(
        ast_dir=ast_dir,
        ast_init_path=ast_init,
        transformer_path=transformer,
        parent_module="statements",
        classname="WidgetStatement",
        ast_class_source=(
            "@dataclass(frozen=True, slots=True)\n"
            "class WidgetStatement(Statement):\n"
            '    """test widget."""\n'
            "    pass\n"
        ),
        transformer_method_source=(
            "def widgetstatement(self, items):\n"
            "    return WidgetStatement()\n"
        ),
        extra_imports=[],
    )
    new_init = ast_init.read_text(encoding="utf-8")
    assert "WidgetStatement" in new_init
    new_module = (ast_dir / "statements.py").read_text(encoding="utf-8")
    assert "class WidgetStatement(Statement):" in new_module
    new_xformer = transformer.read_text(encoding="utf-8")
    assert "def widgetstatement(self, items):" in new_xformer
    # extra_imports None — but the classname itself must be added.
    assert "WidgetStatement" in new_xformer
    assert len(result.paths) == 3


def test_apply_unmodeled_rule_unknown_parent_module(tmp_path: Path):
    ast_dir, ast_init, transformer = _make_sandbox(tmp_path)
    with pytest.raises(edits.AnchorNotFoundError):
        edits.apply_unmodeled_rule(
            ast_dir=ast_dir,
            ast_init_path=ast_init,
            transformer_path=transformer,
            parent_module="nonsense",
            classname="X",
            ast_class_source="@dataclass\nclass X:\n    pass\n",
            transformer_method_source="def x(self, items): return X()\n",
        )


def test_apply_unmodeled_rule_revert_restores_all(tmp_path: Path):
    ast_dir, ast_init, transformer = _make_sandbox(tmp_path)
    statements_path = ast_dir / "statements.py"
    originals = {
        statements_path: statements_path.read_text(encoding="utf-8"),
        ast_init: ast_init.read_text(encoding="utf-8"),
        transformer: transformer.read_text(encoding="utf-8"),
    }
    result = edits.apply_unmodeled_rule(
        ast_dir=ast_dir,
        ast_init_path=ast_init,
        transformer_path=transformer,
        parent_module="statements",
        classname="WidgetStatement",
        ast_class_source=(
            "@dataclass(frozen=True, slots=True)\n"
            "class WidgetStatement(Statement):\n"
            '    """test widget."""\n'
            "    pass\n"
        ),
        transformer_method_source=(
            "def widgetstatement(self, items):\n    return WidgetStatement()\n"
        ),
    )
    for p, original in originals.items():
        assert p.read_text(encoding="utf-8") != original, p
    edits.revert_unmodeled_rule(result)
    for p, original in originals.items():
        assert p.read_text(encoding="utf-8") == original, p


def test_apply_unmodeled_rule_rollback_on_method_failure(tmp_path: Path):
    ast_dir, ast_init, transformer = _make_sandbox(tmp_path)
    statements_path = ast_dir / "statements.py"
    originals = {
        statements_path: statements_path.read_text(encoding="utf-8"),
        ast_init: ast_init.read_text(encoding="utf-8"),
        transformer: transformer.read_text(encoding="utf-8"),
    }
    with pytest.raises(edits.AnchorNotFoundError):
        edits.apply_unmodeled_rule(
            ast_dir=ast_dir,
            ast_init_path=ast_init,
            transformer_path=transformer,
            parent_module="statements",
            classname="BogusStatement",
            ast_class_source=(
                "@dataclass(frozen=True, slots=True)\n"
                "class BogusStatement(Statement):\n"
                '    """test."""\n'
                "    pass\n"
            ),
            transformer_method_source="def bogus(self, items):\n  return X(\n",  # malformed
        )
    # All three originals should be restored.
    for p, original in originals.items():
        assert p.read_text(encoding="utf-8") == original, p


# ---------------------------------------------------------------------------
# LLM schemas
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
        return _FakeResponse(content=[self._queue.pop(0)])


class _ScriptedClient:
    def __init__(self, blocks: list[_FakeBlock]) -> None:
        self.messages = _FakeMessages(blocks)


def test_call_tool_validates_ast_design():
    client = _ScriptedClient([
        _FakeBlock(type="tool_use", name="emit_ast_class_design", input={
            "classname": "X",
            "parent_class": "Statement",
            "parent_module": "statements",
            "fields": [{"name": "f", "type": "int"}],
            "docstring": "x",
        }),
    ])
    res = llm.call_tool(
        tool_name="emit_ast_class_design",
        system_prompt="sys",
        static_context_blocks=[{"type": "text", "text": "x"}],
        user_prompt="hi",
        model="fake",
        client=client,
    )
    assert res.arguments["classname"] == "X"


def test_call_tool_rejects_invalid_ast_design():
    client = _ScriptedClient([
        _FakeBlock(type="tool_use", name="emit_ast_class_design", input={
            "classname": "X",
            # missing parent_class, fields, etc.
        }),
    ])
    with pytest.raises(ValueError):
        llm.call_tool(
            tool_name="emit_ast_class_design",
            system_prompt="sys",
            static_context_blocks=[],
            user_prompt="hi",
            model="fake",
            client=client,
        )


# ---------------------------------------------------------------------------
# Driver end-to-end (mocked LLM, sandboxed edits via monkeypatch on context)
# ---------------------------------------------------------------------------


def _green_pytest(_repo: Path) -> tuple[int, str]:
    return 0, "1 passed"


def _red_then_green():
    state = {"n": 0}
    def runner(_repo: Path) -> tuple[int, str]:
        state["n"] += 1
        if state["n"] == 1:
            return 1, "ImportError: cannot import name 'X'"
        return 0, "1 passed"
    return runner


def _snapshot_writeable_targets() -> dict[Path, str]:
    return {
        context.AST_INIT: context.AST_INIT.read_text(encoding="utf-8"),
        context.TRANSFORMER: context.TRANSFORMER.read_text(encoding="utf-8"),
        context.AST_DIR / "statements.py": (context.AST_DIR / "statements.py").read_text(encoding="utf-8"),
    }


def _restore_targets(snapshot: dict[Path, str]) -> None:
    for p, original in snapshot.items():
        p.write_text(original, encoding="utf-8")


def test_driver_happy_path(tmp_path: Path):
    blocks = [
        _FakeBlock(type="tool_use", name="emit_ast_class_design", input={
            "classname": "WidgetStatement",
            "parent_class": "Statement",
            "parent_module": "statements",
            "fields": [],
            "docstring": "test widget",
        }),
        _FakeBlock(type="tool_use", name="emit_transformer_method", input={
            "method_source": "def thenstatement_fake(self, items):\n    return WidgetStatement()\n",
            "extra_imports": [],
        }),
    ]
    client = _ScriptedClient(blocks)
    snap = _snapshot_writeable_targets()
    try:
        result = unmodeled_rule.run(
            label="unmodeled-rule:thenstatement",
            project_dir=tmp_path,
            client=client,
            pytest_runner=_green_pytest,
            oracle_text="...",
            verbose=False,
        )
    finally:
        _restore_targets(snap)
    assert result.outcome == "applied", result.as_json()
    assert result.final_plan is not None


def test_driver_retry_on_pytest_red(tmp_path: Path):
    blocks = [
        _FakeBlock(type="tool_use", name="emit_ast_class_design", input={
            "classname": "WidgetStatement",
            "parent_class": "Statement",
            "parent_module": "statements",
            "fields": [],
            "docstring": "x",
        }),
        _FakeBlock(type="tool_use", name="emit_transformer_method", input={
            "method_source": "def thenstatement_fake(self, items):\n    return WidgetStatement()\n",
            "extra_imports": [],
        }),
        _FakeBlock(type="tool_use", name="emit_transformer_method", input={
            "method_source": "def thenstatement_fake2(self, items):\n    return WidgetStatement()\n",
            "extra_imports": [],
        }),
    ]
    client = _ScriptedClient(blocks)
    runner = _red_then_green()
    snap = _snapshot_writeable_targets()
    try:
        result = unmodeled_rule.run(
            label="unmodeled-rule:thenstatement",
            project_dir=tmp_path,
            client=client,
            pytest_runner=runner,
            verbose=False,
        )
    finally:
        _restore_targets(snap)
    assert result.outcome == "applied-after-retry", result.as_json()
    assert result.pytest_first_tail != ""


def test_driver_aborts_when_rule_unknown(tmp_path: Path):
    result = unmodeled_rule.run(
        label="unmodeled-rule:thisrulenamedefinitelydoesnotexist",
        project_dir=tmp_path,
        client=_ScriptedClient([]),
        pytest_runner=_green_pytest,
        verbose=False,
    )
    assert result.outcome == "aborted-u0"


def test_driver_aborts_on_libcst_failure(tmp_path: Path):
    blocks = [
        _FakeBlock(type="tool_use", name="emit_ast_class_design", input={
            "classname": "WidgetStatement",
            "parent_class": "Statement",
            "parent_module": "statements",
            "fields": [],
            "docstring": "x",
        }),
        _FakeBlock(type="tool_use", name="emit_transformer_method", input={
            "method_source": "def thenstatement_fake(self, items)\n    return WidgetStatement(\n",  # malformed
            "extra_imports": [],
        }),
    ]
    client = _ScriptedClient(blocks)
    snap = _snapshot_writeable_targets()
    try:
        result = unmodeled_rule.run(
            label="unmodeled-rule:thenstatement",
            project_dir=tmp_path,
            client=client,
            pytest_runner=_green_pytest,
            verbose=False,
        )
    finally:
        _restore_targets(snap)
    assert result.outcome == "aborted-u5"


# ---------------------------------------------------------------------------
# U6b live-classify gate
# ---------------------------------------------------------------------------


def test_driver_aborts_when_live_classify_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    # pytest passes (green runner) but a fresh parse of the originating
    # card still produces the same unmodeled-rule:X label. The gate
    # reverts U5's three-file edit and aborts so the strategy chain
    # can fall back to freeform.
    monkeypatch.setattr(
        unmodeled_rule, "_live_card_still_failing_unmodeled",
        lambda **_kw: True,
    )

    blocks = [
        _FakeBlock(type="tool_use", name="emit_ast_class_design", input={
            "classname": "WidgetStatement",
            "parent_class": "Statement",
            "parent_module": "statements",
            "fields": [],
            "docstring": "x",
        }),
        _FakeBlock(type="tool_use", name="emit_transformer_method", input={
            "method_source": "def thenstatement_fake(self, items):\n    return WidgetStatement()\n",
            "extra_imports": [],
        }),
    ]
    client = _ScriptedClient(blocks)
    snap = _snapshot_writeable_targets()
    try:
        result = unmodeled_rule.run(
            label="unmodeled-rule:thenstatement",
            project_dir=tmp_path,
            client=client,
            pytest_runner=_green_pytest,
            card_name="Fake Card",
            oracle_text="then draw a card.",
            verbose=False,
        )
    finally:
        # Defensive restore — the gate should revert via
        # edits.revert_unmodeled_rule, but a test-failure interrupt
        # shouldn't leave the AST package dirty.
        _restore_targets(snap)
    assert result.outcome == "aborted-classify-unchanged", result.as_json()


def test_live_classify_helper_unmodeled_noop_when_card_data_missing():
    assert unmodeled_rule._live_card_still_failing_unmodeled(
        card_name="", oracle_text="", label="unmodeled-rule:thenstatement",
    ) is False
    assert unmodeled_rule._live_card_still_failing_unmodeled(
        card_name="Card", oracle_text="", label="unmodeled-rule:thenstatement",
    ) is False
    assert unmodeled_rule._live_card_still_failing_unmodeled(
        card_name="", oracle_text="text", label="unmodeled-rule:thenstatement",
    ) is False
