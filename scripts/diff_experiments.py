#!/usr/bin/env -S uv run python
# pyright: basic
"""Side-by-side comparison of two summary.tsv files from run_experiment.

Usage::

    uv run scripts/diff_experiments.py \\
        experiments/runs/baseline/summary.tsv \\
        experiments/runs/h1-no-handler-map/summary.tsv

Per gap that appears in either file we render: A's value, B's value,
delta (B - A), with a +/- marker on whichever direction is "better"
for that metric. Lower is better for cost / turns / wall; +/- markers
follow that convention.

We do NOT attempt significance tests — the experiment is too cheap and
the sample size too small. Eyeball the deltas. If something looks
interesting, raise --repeats and re-run.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


# Lower is better for these columns; we colorize a negative delta (B < A) as
# green (improvement) and positive as red (regression).
LOWER_IS_BETTER = {
    "median_cost_usd",
    "p95_cost_usd",
    "median_num_turns",
    "median_wall_s",
    "median_n_reads",
    "median_n_edits",
}
# Higher is better for these.
HIGHER_IS_BETTER = {"pass_rate"}


def _ansi(code: str) -> str:
    return f"\033[{code}m" if sys.stdout.isatty() else ""


GREEN = _ansi("32")
RED = _ansi("31")
DIM = _ansi("2")
BOLD = _ansi("1")
RESET = _ansi("0")


def _read(path: Path) -> tuple[list[str], dict[str, dict[str, str]]]:
    text = path.read_text(encoding="utf-8").splitlines()
    if not text:
        return [], {}
    header = text[0].split("\t")
    rows: dict[str, dict[str, str]] = {}
    for ln in text[1:]:
        cells = ln.split("\t")
        rec = dict(zip(header, cells, strict=True))
        rows[rec["gap_label"]] = rec
    return header, rows


def _fmt_delta(col: str, a: str, b: str) -> str:
    try:
        av, bv = float(a), float(b)
    except ValueError:
        return f"{a} → {b}"
    delta = bv - av
    if col in LOWER_IS_BETTER:
        marker = f"{GREEN}-{RESET}" if delta < 0 else f"{RED}+{RESET}" if delta > 0 else " "
    elif col in HIGHER_IS_BETTER:
        marker = f"{GREEN}+{RESET}" if delta > 0 else f"{RED}-{RESET}" if delta < 0 else " "
    else:
        marker = " "
    return f"{a} → {b} ({marker}{delta:+.4f})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("a", type=Path, help="baseline summary.tsv")
    ap.add_argument("b", type=Path, help="comparison summary.tsv")
    args = ap.parse_args(argv)

    header_a, rows_a = _read(args.a)
    header_b, rows_b = _read(args.b)
    if not header_a or not header_b:
        print("empty summary file(s); nothing to diff.", file=sys.stderr)
        return 1
    # Use the intersection of columns so a schema bump on one side doesn't
    # render KeyErrors. gap_label is always present (it's the join key) so
    # we drop it from the comparison columns.
    cols = [c for c in header_a if c in header_b and c != "gap_label"]

    all_gaps = sorted(set(rows_a) | set(rows_b))
    print(f"{BOLD}A:{RESET} {args.a}")
    print(f"{BOLD}B:{RESET} {args.b}")
    print()
    for gap in all_gaps:
        ra = rows_a.get(gap)
        rb = rows_b.get(gap)
        if ra is None:
            print(f"{BOLD}{gap}{RESET}  {DIM}(only in B){RESET}")
            continue
        if rb is None:
            print(f"{BOLD}{gap}{RESET}  {DIM}(only in A){RESET}")
            continue
        print(f"{BOLD}{gap}{RESET}")
        for col in cols:
            line = _fmt_delta(col, ra[col], rb[col])
            print(f"  {col:<22} {line}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
