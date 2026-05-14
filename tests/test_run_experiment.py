"""Tests for the experiment runner + diff tool (Phase 2).

The end-to-end test stubs ``_replay_once`` so we don't shell out to a real
fix_parser_gaps.py subprocess (which would itself try to git-reset the
worktree — destructive during development). The substitute appends fake
runs.tsv rows directly, exercising the rest of the script: gap discovery,
summary aggregation, and the diff side-by-side output.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import fix_parser_gaps as flp  # noqa: E402
import run_experiment as rxp  # noqa: E402
import diff_experiments as dxp  # noqa: E402


def _make_gap(gap_dir: Path, slug: str) -> None:
    """Drop a captured-gap file under ``gap_dir`` so _list_gaps picks it up."""
    gap_dir.mkdir(parents=True, exist_ok=True)
    (gap_dir / f"{slug}.json").write_text(
        '{"set_code": "test", "gap": {"kind": "parse", "label": "unmodeled-rule:'
        + slug + '", "card_name": "C", "oracle_text": "x"}, '
        '"ast_text": null, "parse_error_block": null, "ref_commit": ""}\n',
        encoding="utf-8",
    )


def _fake_row(slug: str, *, cost: float, turns: int, wall: float,
              reads: int = 1, edits: int = 0, outcome: str = "pass") -> str:
    """Build a single TSV row matching RUNS_TSV_HEADER."""
    return "\t".join([
        "20260514T120000Z",
        "deadbeef",
        "deadbeef",
        "parse",
        f"unmodeled-rule:{slug}",
        "Card",
        "1",
        f"{cost:.6f}",
        str(turns),
        f"{wall:.3f}",
        str(reads),  # n_reads
        "0",         # n_greps
        str(edits),  # n_edits
        "0",         # n_writes
        "0",         # n_bash
        outcome,
        "smoke",
    ])


def _stub_replay(record_dir: Path, slug: str) -> int:
    """Append a deterministic row to runs.tsv. Two calls per slug → median
    falls cleanly on a known value, so the aggregator's output is testable."""
    runs_tsv = record_dir / "runs.tsv"
    if not runs_tsv.exists():
        runs_tsv.parent.mkdir(parents=True, exist_ok=True)
        runs_tsv.write_text(flp.RUNS_TSV_HEADER + "\n", encoding="utf-8")
    with runs_tsv.open("a", encoding="utf-8") as f:
        f.write(_fake_row(slug, cost=0.10, turns=5, wall=12.0) + "\n")
    return 0


def test_list_gaps_filters_by_user_supplied_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gap_dir = tmp_path / "gaps"
    _make_gap(gap_dir, "alpha")
    _make_gap(gap_dir, "beta")
    _make_gap(gap_dir, "gamma")
    monkeypatch.setenv("ARGENTUM_GAPS_DIR", str(gap_dir))

    assert rxp._list_gaps(None) == ["alpha", "beta", "gamma"]
    assert rxp._list_gaps(["alpha", "gamma"]) == ["alpha", "gamma"]


def test_list_gaps_rejects_unknown_slugs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    gap_dir = tmp_path / "gaps"
    _make_gap(gap_dir, "alpha")
    monkeypatch.setenv("ARGENTUM_GAPS_DIR", str(gap_dir))

    with pytest.raises(SystemExit) as excinfo:
        rxp._list_gaps(["alpha", "nonexistent"])
    assert excinfo.value.code == 2


def test_aggregate_produces_per_gap_summary() -> None:
    rows = [
        {
            "gap_label": "unmodeled-rule:alpha",
            "outcome": "pass",
            "cost_usd": "0.10", "num_turns": "5", "wall_s": "12.0",
            "n_reads": "2", "n_edits": "1",
        },
        {
            "gap_label": "unmodeled-rule:alpha",
            "outcome": "pass",
            "cost_usd": "0.20", "num_turns": "7", "wall_s": "16.0",
            "n_reads": "3", "n_edits": "2",
        },
        {
            "gap_label": "unmodeled-rule:beta",
            "outcome": "abort_pytest",
            "cost_usd": "0.30", "num_turns": "10", "wall_s": "24.0",
            "n_reads": "5", "n_edits": "0",
        },
    ]
    text = rxp._aggregate(rows)
    lines = text.strip().splitlines()
    header = lines[0].split("\t")
    assert "gap_label" in header
    assert "median_cost_usd" in header
    assert "pass_rate" in header
    body_rows = [dict(zip(header, ln.split("\t"), strict=True)) for ln in lines[1:]]
    by_label = {r["gap_label"]: r for r in body_rows}

    # Alpha: 2 passes, median cost = (0.10 + 0.20) / 2 = 0.15
    assert by_label["unmodeled-rule:alpha"]["n"] == "2"
    assert by_label["unmodeled-rule:alpha"]["pass_rate"] == "1.000"
    assert float(by_label["unmodeled-rule:alpha"]["median_cost_usd"]) == pytest.approx(0.15)

    # Beta: 1 row, all metrics fall back to the single sample.
    assert by_label["unmodeled-rule:beta"]["n"] == "1"
    assert by_label["unmodeled-rule:beta"]["pass_rate"] == "0.000"
    assert float(by_label["unmodeled-rule:beta"]["median_cost_usd"]) == pytest.approx(0.30)


def test_main_end_to_end_with_stubbed_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One replay per gap, summary tsv shape correct."""
    monkeypatch.setenv("NO_COLOR", "1")
    gap_dir = tmp_path / "gaps"
    _make_gap(gap_dir, "alpha")
    _make_gap(gap_dir, "beta")
    monkeypatch.setenv("ARGENTUM_GAPS_DIR", str(gap_dir))

    # Redirect the record dir away from the real experiments/runs/ tree so
    # the test stays hermetic. REPO is hardcoded inside rxp.main, so we
    # temporarily monkeypatch it.
    monkeypatch.setattr(rxp, "REPO", tmp_path)

    calls: list[str] = []

    def stub(*, slug: str, record_dir: Path, **_: object) -> int:
        calls.append(slug)
        return _stub_replay(record_dir, slug)

    monkeypatch.setattr(rxp, "_replay_once", stub)

    rc = rxp.main([
        "--tag", "smoke-tag",
        "--repeats", "1",
        "--description", "test",
        "--allow-dirty",
    ])
    assert rc == 0
    assert calls == ["alpha", "beta"]

    record_dir = tmp_path / "experiments" / "runs" / "smoke-tag"
    assert (record_dir / "runs.tsv").is_file()
    summary = (record_dir / "summary.tsv").read_text(encoding="utf-8").splitlines()
    header = summary[0].split("\t")
    assert header[0] == "gap_label"
    assert "median_cost_usd" in header
    # One row per gap label.
    assert len(summary) == 3  # header + 2 rows
    body = [ln.split("\t")[0] for ln in summary[1:]]
    assert body == ["unmodeled-rule:alpha", "unmodeled-rule:beta"]


def test_main_dry_run_does_not_invoke_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    gap_dir = tmp_path / "gaps"
    _make_gap(gap_dir, "only")
    monkeypatch.setenv("ARGENTUM_GAPS_DIR", str(gap_dir))
    monkeypatch.setattr(rxp, "REPO", tmp_path)

    def boom(**_: object) -> int:
        raise AssertionError("dry-run must not call _replay_once")

    monkeypatch.setattr(rxp, "_replay_once", boom)

    rc = rxp.main([
        "--tag", "dry",
        "--repeats", "2",
        "--dry-run",
        "--allow-dirty",
    ])
    assert rc == 0
    out = capsys.readouterr().out
    assert "would replay" in out


# ----------------------------------------------------------------------------
# diff_experiments
# ----------------------------------------------------------------------------


def _write_summary(path: Path, rows: list[dict[str, str]]) -> None:
    header = (
        "gap_label\tn\tpass_rate\tmedian_cost_usd\tp95_cost_usd\t"
        "median_num_turns\tmedian_wall_s\tmedian_n_reads\tmedian_n_edits"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [header]
    for r in rows:
        lines.append("\t".join(r[c] for c in header.split("\t")))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_diff_renders_per_gap_deltas(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    a = tmp_path / "a.tsv"
    b = tmp_path / "b.tsv"
    _write_summary(a, [
        {"gap_label": "g1", "n": "3", "pass_rate": "1.000",
         "median_cost_usd": "0.100000", "p95_cost_usd": "0.120000",
         "median_num_turns": "5.0", "median_wall_s": "12.0",
         "median_n_reads": "2.0", "median_n_edits": "1.0"},
    ])
    _write_summary(b, [
        {"gap_label": "g1", "n": "3", "pass_rate": "1.000",
         "median_cost_usd": "0.080000", "p95_cost_usd": "0.110000",
         "median_num_turns": "4.0", "median_wall_s": "10.0",
         "median_n_reads": "2.0", "median_n_edits": "1.0"},
    ])
    rc = dxp.main([str(a), str(b)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "g1" in out
    assert "median_cost_usd" in out
    # B's cost is lower → the delta line should show -0.0200 somewhere.
    assert "-0.0200" in out


def test_run_experiment_real_subprocess_with_fake_claude(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integration test: run_experiment.py shells out to fix_parser_gaps.py
    --replay, which spawns claude (our fake shim). Verifies the cross-process
    pipeline produces a non-empty runs.tsv and a well-formed summary.tsv.

    We use ARGENTUM_FIX_LOOP_NO_RESTORE=1 so the inner replay doesn't reset
    the worktree (a dev checkout has uncommitted work we want to preserve).
    --skip-pytest keeps the inner call out of the 13-second pytest path.
    """
    import json as json_

    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("ARGENTUM_FIX_LOOP_NO_RESTORE", "1")
    monkeypatch.setenv("ARGENTUM_PARSE_CACHE_DIR", str(tmp_path / "pcache"))

    # Fake claude shim — same shape as test_fix_loop_recording.
    shim = tmp_path / "fake_claude.py"
    shim.write_text(
        "import os, sys\n"
        "_ = sys.stdin.read()\n"
        "for line in os.environ['FAKE_CLAUDE_TRANSCRIPT'].splitlines():\n"
        "    if line.strip():\n"
        "        sys.stdout.write(line + '\\n')\n"
        "        sys.stdout.flush()\n",
        encoding="utf-8",
    )
    transcript = [
        {"type": "system", "subtype": "init", "model": "fake"},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "done"},
        ]}},
        {"type": "result", "subtype": "success",
         "num_turns": 1, "total_cost_usd": 0.05},
    ]
    monkeypatch.setenv(
        "FAKE_CLAUDE_TRANSCRIPT",
        "\n".join(json_.dumps(e) for e in transcript),
    )

    gap_dir = tmp_path / "gaps"
    head = flp.git("rev-parse", "HEAD")
    gap_dir.mkdir(parents=True)
    # Two captured gaps so the summary has at least 2 rows.
    for slug in ("smoke-a", "smoke-b"):
        (gap_dir / f"{slug}.json").write_text(
            json_.dumps({
                "set_code": "test",
                "gap": {
                    "kind": "parse",
                    "label": f"unmodeled-rule:{slug}",
                    "card_name": f"Card {slug}",
                    "oracle_text": "x",
                },
                "ast_text": None,
                "parse_error_block": None,
                "ref_commit": head,
            }) + "\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("ARGENTUM_GAPS_DIR", str(gap_dir))

    # Redirect record dir into tmp_path. Inside run_experiment.main(), REPO
    # is used to compute the experiment dir; monkeypatch it.
    monkeypatch.setattr(rxp, "REPO", tmp_path)

    rc = rxp.main([
        "--tag", "real-subproc",
        "--repeats", "1",
        "--description", "integration",
        "--allow-dirty",
        "--claude-cmd", json_.dumps([sys.executable, str(shim)]),
        "--skip-pytest",
    ])
    assert rc == 0

    record_dir = tmp_path / "experiments" / "runs" / "real-subproc"
    runs = (record_dir / "runs.tsv").read_text(encoding="utf-8").splitlines()
    assert runs[0] == flp.RUNS_TSV_HEADER
    # 2 gaps × 1 repeat = 2 data rows.
    assert len(runs) == 3
    summary = (record_dir / "summary.tsv").read_text(encoding="utf-8").splitlines()
    assert summary[0].startswith("gap_label\t")
    assert len(summary) == 3  # header + 2 gap rows


def test_diff_marks_gaps_present_in_only_one_side(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    a = tmp_path / "a.tsv"
    b = tmp_path / "b.tsv"
    _write_summary(a, [
        {"gap_label": "only_a", "n": "1", "pass_rate": "1.000",
         "median_cost_usd": "0.1", "p95_cost_usd": "0.1",
         "median_num_turns": "1", "median_wall_s": "1",
         "median_n_reads": "1", "median_n_edits": "1"},
    ])
    _write_summary(b, [
        {"gap_label": "only_b", "n": "1", "pass_rate": "1.000",
         "median_cost_usd": "0.2", "p95_cost_usd": "0.2",
         "median_num_turns": "1", "median_wall_s": "1",
         "median_n_reads": "1", "median_n_edits": "1"},
    ])
    rc = dxp.main([str(a), str(b)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "only in A" in out
    assert "only in B" in out
