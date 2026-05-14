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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import existing
from .classify import Bucket1, Bucket2, classify
from .lowerer import KotlinLowerer
from .parser import parse
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

    def to_json(self) -> str:
        return json.dumps(
            {
                "set_code": self.set_code,
                "scanned": self.scanned,
                "gap": self.gap.to_json_dict() if self.gap is not None else None,
            },
            indent=2,
        )


def gap_for_card(card: dict[str, Any], lowerer: KotlinLowerer) -> Gap | None:
    """Run parse + classify against one card. Returns the Gap that surfaces, or
    None if the card is bucket-1 (parses and lowers cleanly).

    Skips no triage — the caller decides whether to apply
    ``existing.implemented_cards_in_set`` / ``is_basic_land`` filters before
    calling this. Used both by :func:`find_first_gap` (inside its set walk)
    and by the ``--card`` CLI flag (single-card reproduction for the
    fix-loop).
    """
    result = parse(card)
    if not result.ok:
        assert result.error is not None
        return Gap(
            kind="parse",
            card_name=card["name"],
            oracle_text=card.get("oracle_text", "") or "",
            label=result.error.message,
        )

    assert result.ast is not None
    match classify(result.ast, lowerer):
        case Bucket1():
            return None
        case Bucket2(missing_node=node):
            return Gap(
                kind="lower",
                card_name=card["name"],
                oracle_text=card.get("oracle_text", "") or "",
                label=node,
            )


def find_first_gap(
    cards: list[dict[str, Any]],
    project_dir: Path,
    set_code: str,
) -> DiagnoseReport:
    """Walk ``cards`` in order, returning the first parse/lower failure.

    Cards already implemented in ``project_dir`` and basic lands are skipped.
    ``scanned`` counts only cards actually fed through parse — i.e. it
    excludes the skipped ones — so the caller can tell whether a ``gap is
    None`` result means "set is clean" vs "set was empty after triage".
    """
    implemented = existing.implemented_cards_in_set(project_dir, set_code)
    lowerer = KotlinLowerer()
    scanned = 0

    for card in cards:
        front = existing.front_face(card["name"])
        if front in implemented:
            continue
        if is_basic_land(card):
            continue

        scanned += 1
        gap = gap_for_card(card, lowerer)
        if gap is not None:
            return DiagnoseReport(set_code=set_code, scanned=scanned, gap=gap)

    return DiagnoseReport(set_code=set_code, scanned=scanned, gap=None)


__all__ = ["DiagnoseReport", "Gap", "find_first_gap", "gap_for_card"]
