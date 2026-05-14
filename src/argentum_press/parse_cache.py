"""Disk cache for ``ParseResult``, keyed by ``sha256(name + oracle_text)``.

Opt-in via ``ARGENTUM_PARSE_CACHE=1`` so tests, the ``add-set`` pipeline,
and one-shot CLI calls keep their uncached behavior by default. The fix-loop
orchestrator turns the env var on for itself (and any gap-finding subprocess
it spawns) since that's the workflow where re-parsing the same 170+ cards
across many iterations dominates wall-clock.

Cache layout under :data:`CACHE_ROOT`::

    <root>/_version              one-line text: int matching :data:`CACHE_VERSION`
    <root>/<sha[:2]>/<sha>.pkl   pickled ParseResult (payload)
    <root>/<sha[:2]>/<sha>.meta  one-line text index: "pass\\t" or "fail\\t<label>"

The .meta sidecar lets :func:`invalidate_label` find affected entries
without unpickling every payload — a hot path after each fix-loop iteration.

The ``_version`` sidecar guards against schema drift: if the pickled
``ParseResult`` shape or the gap-label format changes, bump
:data:`CACHE_VERSION` and the next read wipes the cache automatically.
Without this we silently serve stale labels after a parser change (the
shipped trigger: an earlier label-format fix appeared not to take effect
because cached pickles from before the fix still carried the old
``error.message``).

Invalidation policy is "label-targeted, trust-additive": after the agent
fixes a parser/transformer gap with label ``L``, the orchestrator calls
``invalidate_label(L)`` so cards previously cached as failing with that
label get re-parsed. Other entries stay valid. If an agent edit silently
regresses a previously-passing card, that escapes the cache; pytest is the
backstop and the user can :func:`clear` to start fresh.
"""

from __future__ import annotations

import hashlib
import os
import pickle
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .parser import ParseResult


# Bump when the pickled ParseResult shape, the gap-label format, or any
# other on-disk schema changes. Mismatched entries are wiped on the next
# cache access — cheaper for the user than having to remember to call
# `clear()` after every parser edit that affects what we serialize.
CACHE_VERSION = 2


def _cache_root() -> Path:
    """Resolve the cache directory at call time.

    Read every call (not module-load) so tests can flip
    ``ARGENTUM_PARSE_CACHE_DIR`` per-test without re-importing.
    """
    override = os.environ.get("ARGENTUM_PARSE_CACHE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".cache" / "argentum-press" / "parse-cache"


def _enabled() -> bool:
    return os.environ.get("ARGENTUM_PARSE_CACHE") == "1"


def _key(card: dict[str, Any]) -> str:
    name = card.get("name") or ""
    text = card.get("oracle_text") or ""
    return hashlib.sha256(f"{name}\x00{text}".encode("utf-8")).hexdigest()


def _paths(key: str) -> tuple[Path, Path]:
    d = _cache_root() / key[:2]
    return d / f"{key}.pkl", d / f"{key}.meta"


def _ensure_version() -> None:
    """Wipe the cache when its on-disk schema version doesn't match
    :data:`CACHE_VERSION`.

    Stat'd on every call; ~microseconds when in sync. The alternative — a
    module-global "already-checked" flag — would survive across tests using
    different cache dirs and silently skip the check on a fresh dir.
    """
    root = _cache_root()
    vfile = root / "_version"
    try:
        existing: int | None = int(vfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        existing = None
    if existing == CACHE_VERSION:
        return
    if root.is_dir():
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    vfile.write_text(str(CACHE_VERSION), encoding="utf-8")


def cached_parse(card: dict[str, Any]) -> ParseResult:
    """Return the ``ParseResult`` for ``card``, using the disk cache when enabled.

    Transparent passthrough to :func:`argentum_press.parser.parse` when
    ``ARGENTUM_PARSE_CACHE`` is unset — so importing this function instead of
    ``parser.parse`` is safe everywhere; only the orchestrator opts in.
    """
    from .parser import parse

    if not _enabled():
        return parse(card)

    _ensure_version()
    key = _key(card)
    pkl, meta = _paths(key)
    if pkl.exists():
        try:
            with pkl.open("rb") as f:
                return pickle.load(f)
        except (pickle.UnpicklingError, EOFError, OSError):
            # Corrupted entry — drop both files and re-parse.
            pkl.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)

    result = parse(card)
    _write(pkl, meta, result)
    return result


def _meta_line(result: ParseResult) -> str:
    if result.ok:
        return "pass\t\n"
    assert result.error is not None  # invariant: not ok => error set
    return f"fail\t{result.error.message}\n"


def _write(pkl: Path, meta: Path, result: ParseResult) -> None:
    pkl.parent.mkdir(parents=True, exist_ok=True)
    pkl_tmp = pkl.with_suffix(".pkl.tmp")
    meta_tmp = meta.with_suffix(".meta.tmp")
    with pkl_tmp.open("wb") as f:
        pickle.dump(result, f)
    meta_tmp.write_text(_meta_line(result), encoding="utf-8")
    # Pickle first so a concurrent reader never sees a meta pointing at a
    # missing payload; meta second because invalidate_label reads it.
    pkl_tmp.replace(pkl)
    meta_tmp.replace(meta)


def invalidate_label(label: str) -> int:
    """Remove cache entries whose stored result carries ``label``.

    Returns the count of removed entries. Safe to call when the cache
    directory does not exist (returns 0). Walks all ``.meta`` files; sub-second
    on local disk even for a 25k-card cache.
    """
    if not _enabled():
        return 0
    _ensure_version()
    root = _cache_root()
    if not root.is_dir():
        return 0
    target = f"fail\t{label}"
    removed = 0
    for meta in root.rglob("*.meta"):
        try:
            line = meta.read_text(encoding="utf-8").rstrip("\n")
        except OSError:
            continue
        if line == target:
            meta.with_suffix(".pkl").unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            removed += 1
    return removed


def clear() -> None:
    """Remove the entire parse cache."""
    shutil.rmtree(_cache_root(), ignore_errors=True)


__all__ = ["cached_parse", "clear", "invalidate_label"]
