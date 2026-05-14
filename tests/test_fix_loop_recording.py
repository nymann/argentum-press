"""Tests for the fix-loop's Phase 0 recording infrastructure.

We exercise the public seams of ``scripts/fix_parser_gaps.py`` directly —
``Recorder``, ``stream_claude``, ``_gap_slug`` — rather than driving the
full main() loop, because main() pulls in Scryfall I/O and the slow Earley
parser. The seams under test are the ones that matter for replay analysis:
the TSV row schema, the verbatim NDJSON transcript, and tool-use counting.

The "fake claude binary" is a tiny Python shim that ignores its stdin and
emits a canned NDJSON stream on stdout. That's enough to drive
``stream_claude`` end-to-end without spending real $ on a live claude call.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import fix_parser_gaps as flp  # noqa: E402


CANNED_TRANSCRIPT = [
    {"type": "system", "subtype": "init", "model": "claude-fake"},
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "text", "text": "Let me look at the file."},
                {
                    "type": "tool_use", "id": "tu1", "name": "Read",
                    "input": {"file_path": "/tmp/example.txt"},
                },
            ]
        },
    },
    {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "tu1", "content": "ok"}
            ]
        },
    },
    {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use", "id": "tu2", "name": "Grep",
                    "input": {"pattern": "foo", "path": "/tmp"},
                },
            ]
        },
    },
    {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "tu2", "content": "match"}
            ]
        },
    },
    {
        "type": "assistant",
        "message": {
            "content": [
                {
                    "type": "tool_use", "id": "tu3", "name": "Edit",
                    "input": {
                        "file_path": "/tmp/x", "old_string": "a", "new_string": "b",
                    },
                },
                {"type": "text", "text": "done."},
            ]
        },
    },
    {
        "type": "result", "subtype": "success",
        "num_turns": 3, "total_cost_usd": 0.42,
    },
]


@pytest.fixture
def fake_claude(tmp_path: Path) -> list[str]:
    """A Python shim that emits the canned transcript above.

    Writing the events into the shim via env var (FAKE_CLAUDE_TRANSCRIPT)
    keeps the shim itself trivial and lets the test mutate the transcript
    without rewriting the file each call.
    """
    shim = tmp_path / "fake_claude.py"
    shim.write_text(
        "import json, os, sys\n"
        "_ = sys.stdin.read()\n"  # drain the prompt
        "for line in os.environ['FAKE_CLAUDE_TRANSCRIPT'].splitlines():\n"
        "    if line.strip():\n"
        "        sys.stdout.write(line + '\\n')\n"
        "        sys.stdout.flush()\n",
        encoding="utf-8",
    )
    return [sys.executable, str(shim)]


def test_recorder_writes_header_and_row(tmp_path: Path) -> None:
    """The Recorder lays down a header on first use and one row per
    ``finish_iteration``. Subsequent recorders pointed at the same dir append
    without re-writing the header (TSV invariant)."""
    rec_dir = tmp_path / "runs"
    recorder = flp.Recorder(rec_dir)
    rec = recorder.start_iteration(1)
    rec.gap_kind = "parse"
    rec.gap_label = "unmodeled-rule:foo"
    rec.card_name = "Test Card"
    rec.scanned = 7
    rec.cost_usd = 0.123
    rec.num_turns = 4
    rec.wall_s = 12.5
    rec.tool_counts = {"Read": 2, "Edit": 1, "Bash": 3}
    rec.outcome = "pass"
    rec.description = "smoke"
    recorder.finish_iteration(rec)

    content = (rec_dir / "runs.tsv").read_text(encoding="utf-8")
    lines = content.splitlines()
    assert lines[0] == flp.RUNS_TSV_HEADER
    assert len(lines) == 2
    row = lines[1].split("\t")
    header = flp.RUNS_TSV_HEADER.split("\t")
    cells = dict(zip(header, row, strict=True))
    assert cells["gap_kind"] == "parse"
    assert cells["gap_label"] == "unmodeled-rule:foo"
    assert cells["card_name"] == "Test Card"
    assert cells["scanned"] == "7"
    assert cells["num_turns"] == "4"
    assert cells["n_reads"] == "2"
    assert cells["n_edits"] == "1"
    assert cells["n_bash"] == "3"
    assert cells["n_writes"] == "0"
    assert cells["n_greps"] == "0"
    assert cells["outcome"] == "pass"
    assert cells["description"] == "smoke"


def test_recorder_appends_without_duplicating_header(tmp_path: Path) -> None:
    rec_dir = tmp_path / "runs"
    flp.Recorder(rec_dir)
    flp.Recorder(rec_dir)  # second instance shouldn't re-write the header
    content = (rec_dir / "runs.tsv").read_text(encoding="utf-8")
    assert content.count(flp.RUNS_TSV_HEADER) == 1


def test_gap_slug_handles_punctuation() -> None:
    assert flp._gap_slug("unmodeled-rule:colorandexpr") == "unmodeled-rule_colorandexpr"
    # parse-error labels can be long with arbitrary punctuation; the slug
    # truncates at 80 chars so it's safe as a path component.
    long_label = "parse-error: no terminal matches '@' in the current parser context"
    slug = flp._gap_slug(long_label)
    assert len(slug) <= 80
    assert " " not in slug
    assert ":" not in slug


def test_stream_claude_records_tool_counts_and_writes_transcript(
    tmp_path: Path, fake_claude: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    transcript_lines = "\n".join(json.dumps(e) for e in CANNED_TRANSCRIPT)
    monkeypatch.setenv("FAKE_CLAUDE_TRANSCRIPT", transcript_lines)
    # NO_COLOR so the rendered output doesn't litter stdout with ANSI sequences
    # when pytest is captured.
    monkeypatch.setenv("NO_COLOR", "1")

    recorder = flp.Recorder(tmp_path / "runs")
    rec = recorder.start_iteration(1)
    transcript_path = recorder.transcript_jsonl_path(rec, "fake-slug")

    rc, summary = flp.stream_claude(
        "test prompt",
        transcript_path=transcript_path,
        record=rec,
        claude_cmd=fake_claude,
    )
    assert rc == 0
    assert summary == "done."

    # Wall time was measured.
    assert rec.wall_s > 0
    # Cost + turns extracted from the result event.
    assert rec.num_turns == 3
    assert rec.cost_usd == pytest.approx(0.42)
    # Tool counts: one Read, one Grep, one Edit.
    assert rec.tool_counts == {"Read": 1, "Grep": 1, "Edit": 1}

    # Transcript file: one line per non-empty event, byte-for-byte JSON.
    captured = transcript_path.read_text(encoding="utf-8").splitlines()
    assert len(captured) == len(CANNED_TRANSCRIPT)
    for cap, expected in zip(captured, CANNED_TRANSCRIPT, strict=True):
        assert json.loads(cap) == expected


def test_stream_claude_without_recording_writes_no_files(
    tmp_path: Path, fake_claude: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recording machinery is strictly opt-in; without --record nothing
    appears on disk."""
    transcript_lines = "\n".join(json.dumps(e) for e in CANNED_TRANSCRIPT)
    monkeypatch.setenv("FAKE_CLAUDE_TRANSCRIPT", transcript_lines)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.chdir(tmp_path)

    rc, _ = flp.stream_claude(
        "test prompt",
        transcript_path=None,
        record=None,
        claude_cmd=fake_claude,
    )
    assert rc == 0
    # No incidental files were dropped in cwd.
    assert list(tmp_path.iterdir()) == [tmp_path / "fake_claude.py"]


# ----------------------------------------------------------------------------
# replay (Phase 1)
# ----------------------------------------------------------------------------


def _write_gap_file(gap_path: Path, *, ref_commit: str) -> None:
    """Persist a tiny captured-gap payload at ``gap_path``.

    We use a real (unparseable-by-grammar) oracle text so gather_context
    runs the same code path it would in production — exercising the
    grammar/handler-map readers — without depending on a specific gap
    that may shift as the parser evolves."""
    gap_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "set_code": "test",
        "gap": {
            "kind": "parse",
            "label": "unmodeled-rule:examplerule",
            "card_name": "Replay Smoke Card",
            "oracle_text": "Some oracle text the parser does not handle.",
        },
        "ast_text": None,
        "parse_error_block": None,
        "ref_commit": ref_commit,
    }
    gap_path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def test_replay_writes_row_and_transcript(
    tmp_path: Path, fake_claude: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end replay against a fake claude binary.

    We assert: (1) the saved gap was loaded, (2) one row was appended to
    runs.tsv with the right gap fields, (3) the agent transcript was
    captured verbatim, (4) the worktree-restore logic ran without error
    (verified by HEAD being unchanged).
    """
    transcript_lines = "\n".join(json.dumps(e) for e in CANNED_TRANSCRIPT)
    monkeypatch.setenv("FAKE_CLAUDE_TRANSCRIPT", transcript_lines)
    monkeypatch.setenv("NO_COLOR", "1")
    # Don't pollute the shared parse cache.
    monkeypatch.setenv("ARGENTUM_PARSE_CACHE_DIR", str(tmp_path / "pcache"))
    # Redirect the gap library out of the repo so writing the test gap doesn't
    # dirty the worktree (which would trip the cleanliness guard).
    monkeypatch.setenv("ARGENTUM_GAPS_DIR", str(tmp_path / "gaps"))

    head_before = flp.git("rev-parse", "HEAD")
    gap_path = tmp_path / "gaps" / "test-replay-slug.json"
    _write_gap_file(gap_path, ref_commit=head_before)

    # Don't actually run pytest against the project during the unit test;
    # the fake claude doesn't edit anything so an in-memory pass is the
    # truthful answer.
    monkeypatch.setattr(flp, "run_pytest", lambda: (0, ""))

    # Stub the worktree snapshot/restore so a test running on a dirty
    # development checkout doesn't blow away the user's in-progress edits.
    # We still want to test that they're called; the recorded calls list
    # gives us a hook to assert that.
    calls: list[str] = []
    monkeypatch.setattr(flp, "_snapshot_worktree", lambda: (calls.append("snap"), ("dummy", ""))[1])
    monkeypatch.setattr(flp, "_restore_worktree", lambda head: calls.append("restore"))

    record_dir = tmp_path / "runs"
    rc = flp.main([
        "--replay", "test-replay-slug",
        "--record", str(record_dir),
        "--claude-cmd", json.dumps(fake_claude),
        "--description", "smoke",
        "--allow-dirty",
    ])

    assert rc == 0

    # Replay must have snapshotted + restored.
    assert calls == ["snap", "restore"]
    # HEAD is unmoved (replay never commits).
    assert flp.git("rev-parse", "HEAD") == head_before

    tsv = (record_dir / "runs.tsv").read_text(encoding="utf-8").splitlines()
    assert tsv[0] == flp.RUNS_TSV_HEADER
    assert len(tsv) == 2
    cells = dict(zip(flp.RUNS_TSV_HEADER.split("\t"), tsv[1].split("\t"), strict=True))
    assert cells["gap_kind"] == "parse"
    assert cells["gap_label"] == "unmodeled-rule:examplerule"
    assert cells["card_name"] == "Replay Smoke Card"
    assert cells["outcome"] == "pass"
    assert "variant=baseline" in cells["description"]
    assert "replay=test-replay-slug" in cells["description"]
    assert "smoke" in cells["description"]
    # Costs / counts arrived from the fake claude.
    assert cells["num_turns"] == "3"
    assert float(cells["cost_usd"]) == pytest.approx(0.42)
    # Transcript jsonl is sitting next to the tsv.
    transcripts = list(record_dir.glob("*.jsonl"))
    assert len(transcripts) == 1
    captured = transcripts[0].read_text(encoding="utf-8").splitlines()
    assert len(captured) == len(CANNED_TRANSCRIPT)


def test_replay_aborts_when_ref_commit_drifts(
    tmp_path: Path, fake_claude: list[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A saved gap is only safe to replay against the parser state it was
    captured against. A drifted ref_commit must abort before we burn any
    agent calls."""
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("ARGENTUM_PARSE_CACHE_DIR", str(tmp_path / "pcache"))
    monkeypatch.setenv("ARGENTUM_GAPS_DIR", str(tmp_path / "gaps"))

    gap_path = tmp_path / "gaps" / "test-replay-drift.json"
    _write_gap_file(gap_path, ref_commit="0" * 40)  # definitely not HEAD

    record_dir = tmp_path / "runs"
    rc = flp.main([
        "--replay", "test-replay-drift",
        "--record", str(record_dir),
        "--claude-cmd", json.dumps(fake_claude),
        "--allow-dirty",
    ])

    assert rc == 2
    # No row recorded — we never got past the ref_commit guard.
    if (record_dir / "runs.tsv").exists():
        tsv = (record_dir / "runs.tsv").read_text(encoding="utf-8").splitlines()
        assert tsv == [flp.RUNS_TSV_HEADER]


def test_replay_requires_record_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    rc = flp.main(["--replay", "anything"])
    assert rc == 2
