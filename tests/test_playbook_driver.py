"""Tests for the long-lived ``claude`` CLI driver used by the playbook.

The driver replaces the SDK path so the playbook rides the same transport
freeform uses (avoids the OAuth-bearer rate-limit envelope that 429'd the
SDK path persistently). We exercise the driver against a Python shim that
speaks the same stream-json wire format ``claude -p`` emits; the shim
echoes a canned NDJSON sequence per stdin user turn.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from argentum_press.playbook import driver as drv
from argentum_press.playbook import llm


# ---------------------------------------------------------------------------
# Fake-claude shim — same idea as tests/test_fix_loop_recording.fake_claude
# but interactive: reads one JSON user turn per line on stdin, emits a canned
# transcript on stdout per turn.
# ---------------------------------------------------------------------------


_SHIM_SOURCE = r"""
import json
import os
import sys

# Two canned transcripts so we can verify forget() actually re-runs the
# subprocess (each call gets a fresh shim instance with attempt counter 1).
TRANSCRIPTS = json.loads(os.environ.get("FAKE_CLAUDE_TRANSCRIPTS", "[]"))
attempt = 0

while True:
    line = sys.stdin.readline()
    if not line:
        break
    if not line.strip():
        continue
    if attempt >= len(TRANSCRIPTS):
        # Out of canned events — exit cleanly so the driver sees stdout EOF.
        break
    for ev in TRANSCRIPTS[attempt]:
        sys.stdout.write(json.dumps(ev) + "\n")
        sys.stdout.flush()
    attempt += 1
"""


def _shim_cmd(tmp_path: Path) -> list[str]:
    shim = tmp_path / "fake_claude_driver.py"
    shim.write_text(_SHIM_SOURCE, encoding="utf-8")
    return [sys.executable, str(shim)]


def _transcript(text: str) -> list[dict[str, Any]]:
    """Build a minimal canned stream-json transcript that produces ``text``."""
    return [
        {"type": "system", "subtype": "init", "model": "fake"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        },
        {"type": "result", "subtype": "success", "result": text},
    ]


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def test_driver_attempt_returns_assistant_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "FAKE_CLAUDE_TRANSCRIPTS",
        json.dumps([_transcript("hello world")]),
    )
    d = drv.ClaudeCliDriver(
        model="fake",
        working_dir=tmp_path,
        claude_cmd=_shim_cmd(tmp_path),
        idle_timeout_s=10,
    )
    try:
        result = d.attempt("ping")
    finally:
        d.close()
    assert result.assistant_text == "hello world"
    assert any(e.get("type") == "result" for e in result.raw_events)


def test_driver_forget_restarts_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After ``forget()`` the attempt counter inside the shim resets to 0,
    so we get TRANSCRIPTS[0] again — confirming the subprocess restarted."""
    monkeypatch.setenv(
        "FAKE_CLAUDE_TRANSCRIPTS",
        json.dumps([
            _transcript("first session response"),
            _transcript("would only be reached without forget"),
        ]),
    )
    d = drv.ClaudeCliDriver(
        model="fake",
        working_dir=tmp_path,
        claude_cmd=_shim_cmd(tmp_path),
        idle_timeout_s=10,
    )
    try:
        first = d.attempt("ping").assistant_text
        d.forget()
        # Fresh process; attempt counter starts at 0 again → same first text.
        second = d.attempt("ping").assistant_text
    finally:
        d.close()
    assert first == "first session response"
    assert second == "first session response"


def test_driver_raises_on_early_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the shim exits without emitting a result event, the driver must
    surface a clear error rather than blocking forever."""
    # Empty top-level list: shim exits after one stdin read (no transcripts
    # for any attempt → loop breaks → stdout closes → driver sees EOF).
    monkeypatch.setenv("FAKE_CLAUDE_TRANSCRIPTS", json.dumps([]))
    d = drv.ClaudeCliDriver(
        model="fake",
        working_dir=tmp_path,
        claude_cmd=_shim_cmd(tmp_path),
        idle_timeout_s=5,
    )
    try:
        with pytest.raises(drv.DriverError, match="exited before emitting"):
            d.attempt("ping")
    finally:
        d.close()


# ---------------------------------------------------------------------------
# DriverPool
# ---------------------------------------------------------------------------


def test_driver_pool_memoises_per_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "FAKE_CLAUDE_TRANSCRIPTS",
        json.dumps([_transcript("ok")] * 4),
    )
    pool = drv.DriverPool(
        working_dir=tmp_path,
        claude_cmd=_shim_cmd(tmp_path),
        idle_timeout_s=10,
    )
    try:
        a1 = pool.get("haiku")
        a2 = pool.get("haiku")
        b1 = pool.get("opus")
        assert a1 is a2
        assert a1 is not b1
    finally:
        pool.close()


# ---------------------------------------------------------------------------
# call_tool_via_cli — JSON extraction + schema validation
# ---------------------------------------------------------------------------


def _summary_payload() -> dict[str, Any]:
    return {
        "summary": "MayStatement wraps a may effect.",
        "mtg_term": "may",
        "similar_handlers": ["RegularAbility"],
    }


def test_call_tool_via_cli_parses_fenced_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _summary_payload()
    fenced = "Here you go:\n\n```json\n" + json.dumps(payload) + "\n```\n"
    monkeypatch.setenv(
        "FAKE_CLAUDE_TRANSCRIPTS",
        json.dumps([_transcript(fenced)]),
    )
    pool = drv.DriverPool(
        working_dir=tmp_path,
        claude_cmd=_shim_cmd(tmp_path),
        idle_timeout_s=10,
    )
    try:
        result = llm.call_tool_via_cli(
            tool_name="emit_ast_summary",
            system_prompt="sys",
            static_context_blocks=[{"type": "text", "text": "ctx"}],
            user_prompt="summarise",
            pool=pool,
            model="fake",
        )
    finally:
        pool.close()
    assert result.arguments == payload


def test_call_tool_via_cli_parses_bare_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fence — the extractor falls back to whole-text JSON parse."""
    payload = _summary_payload()
    monkeypatch.setenv(
        "FAKE_CLAUDE_TRANSCRIPTS",
        json.dumps([_transcript(json.dumps(payload))]),
    )
    pool = drv.DriverPool(
        working_dir=tmp_path,
        claude_cmd=_shim_cmd(tmp_path),
        idle_timeout_s=10,
    )
    try:
        result = llm.call_tool_via_cli(
            tool_name="emit_ast_summary",
            system_prompt="sys",
            static_context_blocks=[],
            user_prompt="summarise",
            pool=pool,
            model="fake",
        )
    finally:
        pool.close()
    assert result.arguments == payload


def test_call_tool_via_cli_rejects_schema_violations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    bad = {"summary": "missing fields"}
    monkeypatch.setenv(
        "FAKE_CLAUDE_TRANSCRIPTS",
        json.dumps([_transcript("```json\n" + json.dumps(bad) + "\n```")]),
    )
    pool = drv.DriverPool(
        working_dir=tmp_path,
        claude_cmd=_shim_cmd(tmp_path),
        idle_timeout_s=10,
    )
    try:
        with pytest.raises(ValueError, match="failed schema"):
            llm.call_tool_via_cli(
                tool_name="emit_ast_summary",
                system_prompt="sys",
                static_context_blocks=[],
                user_prompt="summarise",
                pool=pool,
                model="fake",
            )
    finally:
        pool.close()


def test_extract_json_object_prefers_fenced_block() -> None:
    text = (
        'preamble {"summary": "ignored"} more text\n'
        '```json\n{"summary": "Foo", "mtg_term": "bar", "similar_handlers": []}\n```'
    )
    obj = llm._extract_json_object(text)
    assert obj["summary"] == "Foo"


def test_extract_json_object_balanced_fallback() -> None:
    text = 'noise before {"summary": "Foo", "mtg_term": "bar", "similar_handlers": []} noise after'
    obj = llm._extract_json_object(text)
    assert obj["summary"] == "Foo"


def test_extract_json_object_raises_when_no_json() -> None:
    with pytest.raises(ValueError, match="could not extract JSON"):
        llm._extract_json_object("no json here at all")
