"""First-failure diagnostic for the parser + lowerer.

Walks a Scryfall set serially and returns the *first* card that either
fails to parse or hits a lowerer :class:`~argentum_press.lowerer.EmitterGap`.
Designed for a bash fix-loop:

.. code-block:: bash

    while true; do
      gap=$(argentum-press diagnose spm --project-dir ../argentum-engine \\
            | jq -r '.gap.label // empty')
      [ -z "$gap" ] && break
      claude -p "fix this parser gap: $gap ..."
    done

Unlike :class:`~argentum_press.pipeline.AddSetPipeline`, this short-circuits
on the first failure rather than scanning the whole set — so even on a
fresh set with hundreds of unparseable cards it returns in seconds.

Already-implemented cards and basic lands are skipped (same triage rules
as the pipeline) so the gap we surface is always something a fix would
actually unblock.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

from . import existing
from .classify import Bucket1, Bucket2, classify
from .lowerer import KotlinLowerer
from .parse_cache import cached_parse
from .parser import ParseErrorDetails, ast as ast_module
from .template import is_basic_land


@dataclass(frozen=True, slots=True)
class Gap:
    """One card the parser/lowerer cannot handle yet."""

    kind: str
    """``"parse"`` (transformer/grammar) or ``"lower"`` (EmitterGap)."""

    card_name: str
    oracle_text: str
    label: str
    """For ``kind="parse"`` this is ``ParseError.message`` (e.g.
    ``"unmodeled-rule:fightexpression"`` or ``"parse-error:..."``). For
    ``kind="lower"`` it's the qualified class name of the missing AST
    node (e.g. ``"argentum_press.parser.ast.abilities.ActivatedAbility"``)."""

    parse_details: ParseErrorDetails | None = None
    """Rich Lark exception data when ``label`` starts with ``parse-error:`` -
    surfaces line/col, expected-tokens list, and a context marker so the
    fix-loop orchestrator can render an actionable parse-error block without
    re-running the parser. Not serialised by :meth:`to_json_dict` (the CLI
    output stays compact); the orchestrator imports :class:`Gap` directly
    and reads the field as a Python object."""

    def to_json_dict(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "card_name": self.card_name,
            "oracle_text": self.oracle_text,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class DiagnoseReport:
    set_code: str
    scanned: int
    """How many cards were tried before we hit a gap (or exhausted the set)."""

    gap: Gap | None
    ast: str | None = None
    """Pretty-printed AST repr. Only populated when the CLI's ``--ast`` flag
    is set; ``None`` for the set-walk path (where dumping every card's AST
    would balloon the JSON for no benefit). Multi-line — consumers pipe
    through ``jq -r '.ast'`` to get readable output."""

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "set_code": self.set_code,
            "scanned": self.scanned,
            "gap": self.gap.to_json_dict() if self.gap is not None else None,
        }
        if self.ast is not None:
            payload["ast"] = self.ast
        return json.dumps(payload, indent=2)


def inspect_card(
    card: dict[str, Any], lowerer: KotlinLowerer
) -> tuple[Gap | None, ast_module.Card | None]:
    """Run parse + classify against one card, returning ``(gap, ast)``.

    ``gap`` is None when the card is bucket-1 (parses and lowers cleanly).
    ``ast`` is None only when parse itself failed; for lower-gaps the AST
    is fully built and returned alongside the gap so callers can inspect
    where in the tree the missing handler sits.

    Skips no triage — the caller decides whether to apply
    ``existing.implemented_cards_in_set`` / ``is_basic_land`` filters before
    calling this.

    Routes through :func:`argentum_press.parse_cache.cached_parse`, which is a
    transparent passthrough to :func:`argentum_press.parser.parse` unless the
    caller opts into disk caching via ``ARGENTUM_PARSE_CACHE=1``.
    """
    result = cached_parse(card)
    if not result.ok:
        assert result.error is not None
        return (
            Gap(
                kind="parse",
                card_name=card["name"],
                oracle_text=card.get("oracle_text", "") or "",
                label=result.error.message,
                parse_details=result.error.details,
            ),
            None,
        )

    assert result.ast is not None
    match classify(result.ast, lowerer):
        case Bucket1():
            return (None, result.ast)
        case Bucket2(missing_node=node):
            return (
                Gap(
                    kind="lower",
                    card_name=card["name"],
                    oracle_text=card.get("oracle_text", "") or "",
                    label=node,
                ),
                result.ast,
            )


def gap_for_card(card: dict[str, Any], lowerer: KotlinLowerer) -> Gap | None:
    """Run parse + classify against one card. Returns the Gap that surfaces, or
    None if the card is bucket-1 (parses and lowers cleanly).

    Thin wrapper around :func:`inspect_card` for callers that don't need the
    AST (e.g. the set walk in :func:`find_first_gap`).
    """
    gap, _ = inspect_card(card, lowerer)
    return gap


def format_ast(card_ast: ast_module.Card) -> str:
    """Pretty-print the AST for the ``--ast`` flag using depth-based
    indentation. Standard ``pprint`` indents each field at the column where
    its name appears, which balloons rapidly with deeply-nested dataclasses
    (a ten-level-deep tree pushes content past column 200). This formatter
    uses fixed two-space indents per depth level so output stays readable
    regardless of nesting depth, and inlines shallow nodes that fit on one
    line for compactness."""
    return _format_node(card_ast, depth=0)


_INLINE_BUDGET = 100


def _format_node(node: Any, depth: int) -> str:
    indent = "  " * depth
    child_indent = "  " * (depth + 1)

    if is_dataclass(node) and not isinstance(node, type):
        cls = type(node).__name__
        fs = fields(node)
        if not fs:
            return f"{cls}()"
        inline = f"{cls}({', '.join(f'{f.name}={getattr(node, f.name)!r}' for f in fs)})"
        if len(inline) <= _INLINE_BUDGET and "\n" not in inline:
            return inline
        lines = [f"{cls}("]
        for f in fs:
            rendered = _format_node(getattr(node, f.name), depth + 1)
            lines.append(f"{child_indent}{f.name}={rendered},")
        lines.append(f"{indent})")
        return "\n".join(lines)

    if isinstance(node, tuple):
        if not node:
            return "()"
        comma = "," if len(node) == 1 else ""
        inline = f"({', '.join(repr(v) for v in node)}{comma})"
        if len(inline) <= _INLINE_BUDGET and "\n" not in inline:
            return inline
        lines = ["("]
        for v in node:
            lines.append(f"{child_indent}{_format_node(v, depth + 1)},")
        lines.append(f"{indent})")
        return "\n".join(lines)

    return repr(node)


def find_first_gap(
    cards: list[dict[str, Any]],
    project_dir: Path,
    set_code: str,
    *,
    progress: Callable[[int, int, dict[str, Any]], None] | None = None,
    on_complete: Callable[[int, int, dict[str, Any], Gap | None], None] | None = None,
    skip_names: set[str] | None = None,
) -> DiagnoseReport:
    """Walk ``cards`` in order, returning the first parse/lower failure.

    Cards already implemented in ``project_dir`` and basic lands are skipped.
    ``scanned`` counts only cards actually fed through parse — i.e. it
    excludes the skipped ones — so the caller can tell whether a ``gap is
    None`` result means "set is clean" vs "set was empty after triage".

    ``progress``, if given, is called as
    ``progress(scanned, candidate_count, card)`` *before* each card is parsed
    so the caller can render the current card while the slow Earley parse is
    running. The card dict (not just the name) is passed so the caller can
    consult the parse cache or other per-card state.

    ``on_complete``, if given, is called as
    ``on_complete(scanned, candidate_count, card, gap)`` *after* each parse
    so the caller can render success/failure. ``gap`` is None when the card
    parsed and lowered cleanly. The walk short-circuits on the first non-None
    gap, so ``on_complete`` fires exactly once for the failing card.

    ``candidate_count`` is fixed for the whole walk so callers can render a
    percentage; skipped cards are not counted toward either number.

    ``skip_names`` is the capture-batch hook: callers that have already
    captured a gap for a given card pass its name here so the next scan
    surfaces a *different* card. The fix-loop's main path doesn't use this
    (there's no such thing as an unparsable card in steady state — every
    card is supposed to be fixable). Skipped cards are excluded from
    ``candidate_count`` the same way implemented cards are.
    """
    implemented = existing.implemented_cards_in_set(project_dir, set_code)
    skip = skip_names or set()
    candidates = [
        c for c in cards
        if existing.front_face(c["name"]) not in implemented
        and not is_basic_land(c)
        and c["name"] not in skip
    ]
    lowerer = KotlinLowerer()
    scanned = 0

    for card in candidates:
        scanned += 1
        if progress is not None:
            progress(scanned, len(candidates), card)
        gap = gap_for_card(card, lowerer)
        if on_complete is not None:
            on_complete(scanned, len(candidates), card, gap)
        if gap is not None:
            return DiagnoseReport(set_code=set_code, scanned=scanned, gap=gap)

    return DiagnoseReport(set_code=set_code, scanned=scanned, gap=None)


__all__ = [
    "DiagnoseReport",
    "Gap",
    "find_first_gap",
    "format_ast",
    "gap_for_card",
    "inspect_card",
]
