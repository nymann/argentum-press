#!/usr/bin/env -S uv run python
# pyright: basic
"""CLI for the unmodeled-rule playbook.

Usage::

    uv run scripts/run_playbook_unmodeled.py \\
        --label "unmodeled-rule:somerulename" \\
        --oracle-text "..." \\
        --project-dir /Users/knj/code/github.com/nymann/argentum-engine

Pipes through to :func:`argentum_press.playbook.unmodeled_rule.main`.
"""
from __future__ import annotations

import sys

from argentum_press.playbook.unmodeled_rule import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
