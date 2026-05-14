#!/usr/bin/env -S uv run python
# pyright: basic
"""scripts/run_ab_race.py — run the freeform vs playbook fix-loop A/B race.

Creates two worktrees off the current main HEAD on branches ``race-freeform``
and ``race-playbook``, seeds each with a copy of the shared parse cache so the
slow Earley walk isn't repeated, and spawns a tmux session with two
side-by-side panes — each running the respective fix-loop mode against the
same set.

After it returns, attach to the session and watch. Stop both panes (Ctrl-C in
each, or kill the tmux session) when you've had enough, then compare:

    git -C /tmp/race-freeform log --oneline main..race-freeform
    git -C /tmp/race-playbook log --oneline main..race-playbook
    uv run scripts/diff_experiments.py \\
        /tmp/race-freeform/experiments/runs/race/runs.tsv \\
        /tmp/race-playbook/experiments/runs/race/runs.tsv

Re-running this script is destructive: existing worktrees + branches with the
same names get removed first. That's the intended UX — the race is a
disposable comparison.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _git(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO, check=check, capture_output=capture, text=True
    )


def _remove_worktree(path: Path, branch: str) -> None:
    """Best-effort cleanup so re-running the script is idempotent."""
    if path.exists():
        _git("worktree", "remove", "--force", str(path), check=False)
    # `worktree remove` doesn't delete the branch; do it explicitly.
    _git("branch", "-D", branch, check=False)


def _create_worktree(path: Path, branch: str, base: str) -> None:
    _git("worktree", "add", str(path), "-b", branch, base)


def _seed_cache(worktree: Path, cache_source: Path) -> int:
    dest = worktree / ".parse-cache"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(cache_source, dest)
    return sum(1 for _ in dest.rglob("*") if _.is_file())


def _have_session(name: str) -> bool:
    r = subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True, text=True, check=False,
    )
    return r.returncode == 0


def _spawn_panes(session: str, freeform_cmd: str, playbook_cmd: str) -> None:
    """Start a fresh tmux session with two side-by-side panes.

    Targets the session by name (``-t {session}``) rather than indexed
    window/pane addresses like ``{session}:0.0``. tmux configurations vary
    on ``base-index`` (0 vs 1) and the indexed form breaks under
    ``base-index 1``; the name form always resolves to the active pane.

    ``send-keys`` is used rather than the command argument to ``new-session``
    because the latter closes the pane the moment its command exits, which
    we don't want on a no-gaps-remaining or an abort.
    """
    if _have_session(session):
        subprocess.run(["tmux", "kill-session", "-t", session], check=False)
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "-x", "240", "-y", "60"],
        check=True,
    )
    # Initial pane → freeform.
    subprocess.run(
        ["tmux", "send-keys", "-t", session, freeform_cmd, "C-m"],
        check=True,
    )
    # Split horizontally; the new pane becomes the active one.
    subprocess.run(
        ["tmux", "split-window", "-h", "-t", session],
        check=True,
    )
    subprocess.run(
        ["tmux", "send-keys", "-t", session, playbook_cmd, "C-m"],
        check=True,
    )
    subprocess.run(
        ["tmux", "select-layout", "-t", session, "even-horizontal"],
        check=True,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("set_code", help="Scryfall set code (e.g. spm).")
    ap.add_argument(
        "engine_dir", type=Path,
        help="Path to argentum-engine (absolute or relative — will be resolved).",
    )
    ap.add_argument(
        "--cache-source", type=Path,
        default=Path.home() / ".cache" / "argentum-press" / "parse-cache",
        help="Existing parse cache directory to seed both worktrees from "
             "(default: ~/.cache/argentum-press/parse-cache).",
    )
    ap.add_argument(
        "--worktree-base", type=Path, default=Path("/tmp"),
        help="Where to create the two worktrees (default: /tmp).",
    )
    ap.add_argument(
        "--session", default="argentum-race",
        help="tmux session name (default: argentum-race). An existing session "
             "with this name will be killed and recreated.",
    )
    ap.add_argument(
        "--freeform-profile", type=Path, default=None,
        help="Claude Code profile dir for the freeform pane (sets "
             "CLAUDE_CONFIG_DIR=<dir> for that pane). Populate the dir first "
             "via scripts/setup_claude_profile.py. If unset, freeform inherits "
             "whatever's in your keychain.",
    )
    ap.add_argument(
        "--playbook-profile", type=Path, default=None,
        help="Claude Code profile dir for the playbook pane (sets "
             "CLAUDE_CONFIG_DIR=<dir> for that pane). Use a different "
             "subscription than --freeform-profile so the two panes don't "
             "contend for quota and 429 each other. Populate via "
             "scripts/setup_claude_profile.py.",
    )
    args = ap.parse_args(argv)

    dirty = _git("status", "--porcelain").stdout
    if dirty.strip():
        print(
            "main is dirty; commit or stash before running the race:\n"
            f"{dirty.rstrip()}",
            file=sys.stderr,
        )
        return 2

    main_head = _git("rev-parse", "HEAD").stdout.strip()
    engine_abs = args.engine_dir.resolve()
    if not engine_abs.is_dir():
        print(f"engine_dir {engine_abs} is not a directory", file=sys.stderr)
        return 2

    cache_source = args.cache_source
    cache_ok = cache_source.is_dir() and any(cache_source.iterdir())
    if not cache_ok:
        print(
            f"warning: --cache-source {cache_source} is empty or missing; "
            f"both worktrees will scan from scratch (slow Earley path).",
            file=sys.stderr,
        )

    plan = [
        ("race-freeform", "freeform"),
        ("race-playbook", "playbook"),
    ]
    for branch, _mode in plan:
        path = args.worktree_base / branch
        _remove_worktree(path, branch)
        _create_worktree(path, branch, main_head)
        if cache_ok:
            n = _seed_cache(path, cache_source)
            print(f"  {path}: seeded .parse-cache with {n} files")
        else:
            print(f"  {path}: no cache seeded")

    def _build_cmd(path: Path, mode: str, profile: Path | None) -> str:
        # CLAUDE_CONFIG_DIR is set before the run command rather than via
        # `tmux send-keys`-with-env so the var is scoped to this shell line
        # only. Both claude -p (freeform) and the playbook's SDK call honour
        # it (the latter via _read_oauth_token_from_profile in llm.py).
        prefix = (
            f"CLAUDE_CONFIG_DIR={profile} " if profile is not None else ""
        )
        return (
            f"cd {path} && "
            f"{prefix}"
            f"ARGENTUM_PARSE_CACHE=1 "
            f"ARGENTUM_PARSE_CACHE_DIR={path}/.parse-cache "
            f"uv run scripts/fix_parser_gaps.py "
            f"{args.set_code} {engine_abs} "
            f"--mode {mode} "
            f"--record experiments/runs/race/"
        )

    freeform_path = args.worktree_base / "race-freeform"
    playbook_path = args.worktree_base / "race-playbook"
    freeform_cmd = _build_cmd(freeform_path, "freeform", args.freeform_profile)
    playbook_cmd = _build_cmd(playbook_path, "playbook", args.playbook_profile)

    if args.freeform_profile is None or args.playbook_profile is None:
        print(
            "warning: both panes will share whatever auth is in your keychain. "
            "Expect 429 contention on the playbook side. "
            "Use --freeform-profile + --playbook-profile to split.",
            file=sys.stderr,
        )

    _spawn_panes(args.session, freeform_cmd, playbook_cmd)

    print()
    print(f"tmux session '{args.session}' running with two panes.")
    print(f"  attach:  tmux attach -t {args.session}")
    print(f"  kill:    tmux kill-session -t {args.session}")
    print()
    print("Compare after stopping:")
    print(f"  git -C {freeform_path} log --oneline main..race-freeform")
    print(f"  git -C {playbook_path} log --oneline main..race-playbook")
    print(
        "  uv run scripts/diff_experiments.py "
        f"{freeform_path}/experiments/runs/race/runs.tsv "
        f"{playbook_path}/experiments/runs/race/runs.tsv"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
