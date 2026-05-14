#!/usr/bin/env -S uv run python
"""scripts/setup_claude_profile.py — capture current Claude Code creds into a profile dir.

Run once per Claude Code account to enable side-by-side use (e.g. for the
A/B race where the freeform and playbook panes need independent quotas):

    # 1. While logged into account A:
    uv run scripts/setup_claude_profile.py A

    # 2. Switch accounts (claude /login as B).

    # 3. While logged into account B:
    uv run scripts/setup_claude_profile.py B

    # 4. Optionally log back into A so the keychain holds A again.

After step 3, ``~/.claude-A/credentials.json`` and ``~/.claude-B/credentials.json``
both exist. The A/B race (``run_ab_race.py``) sets ``CLAUDE_CONFIG_DIR`` per
pane to point each one at its own profile, so the two subscriptions don't
contend for quota.

The script reads the OAuth bearer token from the macOS keychain entry
``Claude Code-credentials`` (the same source Claude Code itself uses) and
writes it as JSON to ``~/.claude-<name>/credentials.json`` with mode 0600.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


def _read_keychain_creds() -> dict[str, Any] | None:
    """Return the parsed Claude Code keychain JSON or None on any error.

    ``security find-generic-password -s "Claude Code-credentials" -w`` dumps
    the raw stored blob; Claude Code stores it as JSON of the form
    ``{"claudeAiOauth": {"accessToken": "...", ...}}``.
    """
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument(
        "name",
        help="Profile name. Lands at <base>/.claude-<name>/credentials.json.",
    )
    ap.add_argument(
        "--base", type=Path, default=Path.home(),
        help="Base directory; profile lands at <base>/.claude-<name>/ (default: $HOME).",
    )
    args = ap.parse_args(argv)

    creds = _read_keychain_creds()
    if creds is None:
        print(
            "error: no Claude Code credentials in keychain "
            "(service: 'Claude Code-credentials').\n"
            "       Run `claude /login` and try again.",
            file=sys.stderr,
        )
        return 2
    token = creds.get("claudeAiOauth", {}).get("accessToken")
    if not isinstance(token, str) or not token:
        print(
            "error: keychain entry exists but has no claudeAiOauth.accessToken.",
            file=sys.stderr,
        )
        return 2

    profile_dir = args.base / f".claude-{args.name}"
    profile_dir.mkdir(parents=True, exist_ok=True)
    out = profile_dir / "credentials.json"
    out.write_text(json.dumps(creds, indent=2) + "\n", encoding="utf-8")
    os.chmod(out, 0o600)

    print(f"wrote {out} (mode 0600)")
    print(f"  token suffix: ...{token[-12:]}")
    print(f"  to use:       CLAUDE_CONFIG_DIR={profile_dir} claude -p ...")
    print(f"                CLAUDE_CONFIG_DIR={profile_dir} <playbook command>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
