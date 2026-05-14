#!/usr/bin/env -S uv run python
# pyright: basic
"""CLI for the parse-error playbook.

Usage::

    uv run scripts/run_playbook_parse_error.py \\
        --label "parse-error:<EOF>@tabc" \\
        --oracle-text "..." \\
        --pe-block path/to/pe_block.txt \\
        --project-dir /Users/knj/code/github.com/nymann/argentum-engine

Pipes through to :func:`argentum_press.playbook.parse_error.main`.
"""
from __future__ import annotations

import sys

from argentum_press.playbook.parse_error import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
