#!/usr/bin/env -S uv run python
# pyright: basic
"""Run a fix-loop replay experiment across the captured gap library.

For each captured gap under ``experiments/gaps/`` (or a user-supplied
subset), replay it N times against the current parser state. The
underlying lever is whatever the user has staged into the prompt
template (or into the variant flag); this script just runs the same
replay enough times that wall-time / cost / num-turns noise averages out.

Outputs land under ``experiments/runs/<tag>/`` — one ``runs.tsv`` row
per replay (managed by ``fix_parser_gaps.py --record``) plus a
``summary.tsv`` aggregated by gap.

Usage::

    uv run scripts/run_experiment.py --tag baseline --repeats 3
    uv run scripts/run_experiment.py --tag h1-no-handler-map \\
        --gaps colorandexpr modalstatement --repeats 5 \\
        --description "drop handler_map from lower prompt"

Branch discipline mirrors autoresearch: each experiment runs on its own
``fixloop-exp/<tag>`` branch so checkout state, captured-gap commit, and
runs/<tag>/ stay consistent. The script will create the branch if it
doesn't exist; it refuses to start with a dirty worktree (override with
``--allow-dirty``).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "fix_parser_gaps.py"


def _gaps_dir() -> Path:
    override = os.environ.get("ARGENTUM_GAPS_DIR")
    return Path(override) if override else REPO / "experiments" / "gaps"


def _git(*args: str, check: bool = True) -> str:
    r = subprocess.run(
        ["git", *args], cwd=REPO, capture_output=True, text=True, check=check
    )
    return (r.stdout or "").rstrip("\n")


def _ensure_branch(tag: str) -> None:
    expected = f"fixloop-exp/{tag}"
    current = _git("rev-parse", "--abbrev-ref", "HEAD")
    if current == expected:
        return
    # Create the branch if it doesn't already exist; otherwise abort. We do
    # NOT auto-checkout — pulling git state out from under the user mid-run
    # is the worst kind of surprise. Print the suggested command and exit.
    show = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{expected}"],
        cwd=REPO, capture_output=True, text=True, check=False,
    )
    if show.returncode != 0:
        print(
            f"branch '{expected}' does not exist. create it with:\n"
            f"  git checkout -b {expected}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    print(
        f"current branch is '{current}', not '{expected}'. switch with:\n"
        f"  git checkout {expected}",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _list_gaps(filter_: list[str] | None) -> list[str]:
    """Return slugs (without .json) of the captured gaps to use.

    ``filter_`` is the user-supplied subset; None means "everything in
    the gap library". We sort the result so reruns produce identical
    summary.tsv row order, which makes diff_experiments simpler.
    """
    gaps = sorted(p.stem for p in _gaps_dir().glob("*.json"))
    if filter_:
        wanted = set(filter_)
        missing = wanted - set(gaps)
        if missing:
            print(f"unknown gap slug(s): {sorted(missing)}", file=sys.stderr)
            raise SystemExit(2)
        gaps = [g for g in gaps if g in wanted]
    return gaps


def _replay_once(
    *,
    slug: str,
    record_dir: Path,
    description: str,
    prompt_variant: str,
    claude_cmd: str | None,
    extra_argv: list[str],
    skip_pytest: bool = False,
) -> int:
    """Invoke ``fix_parser_gaps.py --replay`` once. Returns its exit code.

    We shell out instead of importing because the orchestrator drives a
    real Popen pipeline (claude + pytest) and benefits from a clean
    sys.modules per replay — the same isolation argument that put gap
    finding behind a subprocess in the first place.
    """
    argv = [
        "uv", "run", "--quiet", "python", str(SCRIPT),
        "--replay", slug,
        "--record", str(record_dir),
        "--description", description,
        "--prompt-variant", prompt_variant,
        # The experiment branch is dirty by design (untracked record dir is
        # under experiments/runs/<tag>/) so the inner replay gets --allow-dirty.
        "--allow-dirty",
    ]
    if claude_cmd is not None:
        argv += ["--claude-cmd", claude_cmd]
    if skip_pytest:
        argv += ["--skip-pytest"]
    argv += extra_argv
    r = subprocess.run(argv, cwd=REPO, check=False)
    return r.returncode


def _read_runs(runs_tsv: Path) -> list[dict[str, str]]:
    if not runs_tsv.is_file():
        return []
    lines = runs_tsv.read_text(encoding="utf-8").splitlines()
    if not lines:
        return []
    header = lines[0].split("\t")
    return [dict(zip(header, ln.split("\t"), strict=True)) for ln in lines[1:]]


def _aggregate(rows: list[dict[str, str]]) -> str:
    """Roll up runs.tsv rows by ``gap_label``.

    Per gap we emit: count, median + p95 cost_usd, median num_turns,
    median wall_s, pass_rate. Median/p95 over a small N are noisy but
    that's the agreed acceptance: 3-5 replays per gap, look at the
    median + p95 to spot whether the variant moved the floor or the
    tail. Compute on the fly so this works for any subset of completed
    rows — the user can run the aggregator mid-experiment too.
    """
    by_gap: dict[str, list[dict[str, str]]] = {}
    for r in rows:
        by_gap.setdefault(r["gap_label"], []).append(r)

    header = (
        "gap_label\tn\tpass_rate\tmedian_cost_usd\tp95_cost_usd\t"
        "median_num_turns\tmedian_wall_s\tmedian_n_reads\tmedian_n_edits"
    )
    out = [header]
    for gap_label in sorted(by_gap):
        group = by_gap[gap_label]
        n = len(group)
        passes = sum(1 for r in group if r["outcome"] == "pass")
        costs = [float(r["cost_usd"]) for r in group]
        turns = [int(r["num_turns"]) for r in group]
        walls = [float(r["wall_s"]) for r in group]
        reads = [int(r["n_reads"]) for r in group]
        edits = [int(r["n_edits"]) for r in group]
        # statistics.median is well-defined for n>=1; p95 with quantiles
        # requires n>=2. Fall back to the median when n==1 so the column
        # is always populated.
        p95_cost = (
            statistics.quantiles(costs, n=20)[18] if len(costs) >= 2 else costs[0]
        )
        out.append("\t".join([
            gap_label,
            str(n),
            f"{passes / n:.3f}",
            f"{statistics.median(costs):.6f}",
            f"{p95_cost:.6f}",
            f"{statistics.median(turns):.1f}",
            f"{statistics.median(walls):.3f}",
            f"{statistics.median(reads):.1f}",
            f"{statistics.median(edits):.1f}",
        ]))
    return "\n".join(out) + "\n"


def _write_summary(record_dir: Path) -> Path:
    rows = _read_runs(record_dir / "runs.tsv")
    text = _aggregate(rows)
    out = record_dir / "summary.tsv"
    out.write_text(text, encoding="utf-8")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tag", required=True,
                    help="Experiment tag. Branch name is fixloop-exp/<tag>; "
                         "record dir is experiments/runs/<tag>/.")
    ap.add_argument("--gaps", nargs="*", default=None,
                    help="Subset of gap slugs to run. Default: all under "
                         "experiments/gaps/.")
    ap.add_argument("--repeats", type=int, default=3,
                    help="Number of replays per gap (default 3).")
    ap.add_argument("--description", default="",
                    help="Propagated into the runs.tsv description column.")
    ap.add_argument("--prompt-variant", default="baseline",
                    help="Prompt variant name. Default: baseline.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the plan and exit without running anything.")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="Skip the worktree-cleanliness + branch checks. "
                         "Tests use this; production should not.")
    ap.add_argument("--claude-cmd", default=None,
                    help="JSON-encoded list of argv strings for a claude shim. "
                         "Tests use this; production leaves it unset.")
    ap.add_argument("--skip-pytest", action="store_true",
                    help="Pass --skip-pytest to each replay (test-only fast path).")
    args = ap.parse_args(argv)

    if not args.allow_dirty:
        if _git("status", "--porcelain"):
            print(
                "working tree is dirty; commit or stash before running.\n"
                "  (pass --allow-dirty to override)",
                file=sys.stderr,
            )
            return 2
        _ensure_branch(args.tag)

    gaps = _list_gaps(args.gaps)
    if not gaps:
        print("no captured gaps to run (experiments/gaps/ is empty?)", file=sys.stderr)
        return 1

    record_dir = REPO / "experiments" / "runs" / args.tag
    record_dir.mkdir(parents=True, exist_ok=True)

    plan = [(g, r) for g in gaps for r in range(args.repeats)]
    print(f"experiment tag={args.tag}  gaps={len(gaps)}  repeats={args.repeats}  "
          f"total={len(plan)}  record_dir={record_dir.relative_to(REPO)}",
          flush=True)
    if args.dry_run:
        for g, r in plan:
            print(f"  would replay: gap={g} attempt={r + 1}/{args.repeats}")
        return 0

    failures = 0
    for g, r in plan:
        print(f"\n--- replay gap={g} attempt={r + 1}/{args.repeats} ---", flush=True)
        rc = _replay_once(
            slug=g,
            record_dir=record_dir,
            description=args.description,
            prompt_variant=args.prompt_variant,
            claude_cmd=args.claude_cmd,
            extra_argv=[],
            skip_pytest=args.skip_pytest,
        )
        if rc != 0:
            failures += 1
            print(f"  replay rc={rc} (continuing — failures recorded in runs.tsv)",
                  file=sys.stderr)

    summary = _write_summary(record_dir)
    print(f"\nsummary: {summary.relative_to(REPO)}  (failures: {failures})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
