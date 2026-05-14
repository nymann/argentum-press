#!/usr/bin/env -S uv run python
# pyright: basic
"""CLI for the lower-gap playbook.

Usage::

    uv run scripts/run_playbook_lower.py \\
        --gap-class argentum_press.parser.ast.statements.AtStatement \\
        --project-dir /Users/knj/code/github.com/nymann/argentum-engine

Pipes through to :func:`argentum_press.playbook.lower.main`. Kept as a thin
wrapper so the entry-point name is stable (``scripts/run_playbook_lower.py``)
even if the underlying driver module moves.
"""
from __future__ import annotations

import sys

from argentum_press.playbook.lower import main


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
