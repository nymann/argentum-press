# pyright: basic
"""Disk cache for L3 AST-summary calls.

The L3 LLM call summarises an AST dataclass; the input is the class source.
Same source → same summary → never re-call. Files are JSON, named by
``sha256(source)[:32].json`` under ``experiments/playbook-cache/l3/``.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def _cache_root() -> Path:
    return Path(__file__).resolve().parents[3] / "experiments/playbook-cache/l3"


def _key(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()[:32]


def get(source: str, root: Path | None = None) -> dict[str, Any] | None:
    """Return the cached L3 summary for ``source``, or ``None`` on miss."""
    base = root or _cache_root()
    path = base / f"{_key(source)}.json"
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def put(source: str, summary: dict[str, Any], root: Path | None = None) -> Path:
    """Write ``summary`` to the cache and return the file path written."""
    base = root or _cache_root()
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{_key(source)}.json"
    path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return path
