"""Fix-loop gap-finding subprocess.

Invoked by ``scripts/fix_parser_gaps.py`` as

    python -m argentum_press._fix_loop_gap <set_code> <project_dir>

The orchestrator runs this fresh per iteration so each pass re-imports
parser/transformer/lowerer and picks up the agent's last edit. Without
the subprocess hop, Python's module cache would feed each new iteration
the *previous* parser, the agent's fix would never take effect inside
``find_first_gap``, and the loop would trip its "no progress" abort.

Output is newline-delimited JSON on stdout::

    {"type": "log",    "msg": "..."}     # progress, streamed
    {"type": "result", "gap": ..., "ast": ..., "parse_error_block": ...}

The single ``result`` event is the last line. ``gap`` is null when the set
is clean.

Inherits ``ARGENTUM_PARSE_CACHE`` from the parent process so cache hits
in the worker (just-written entries from a previous iteration's worker)
short-circuit the slow Earley parse.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _emit(event: dict[str, Any]) -> None:
    print(json.dumps(event), flush=True)


def _log(msg: str, color: str | None = None) -> None:
    """Emit a log event. ``color`` is a name ("green", "red", "yellow")
    the orchestrator maps to the corresponding ANSI sequence (or to no
    color on non-TTY / NO_COLOR). Default rendering is DIM."""
    event: dict[str, Any] = {"type": "log", "msg": msg}
    if color is not None:
        event["color"] = color
    _emit(event)


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())


def _format_parse_error_block(details: Any) -> str | None:
    """Format a :class:`ParseErrorDetails` into the prompt-ready string the
    orchestrator used to compute inline. Returns None when ``details`` is
    None (which is the case for unmodeled-rule/lark-error gaps that don't
    carry rich Lark exception data)."""
    if details is None:
        return None
    expected = ", ".join(details.expected[:30]) or "(empty - Earley parser didn't expose a candidate set)"
    return (
        f"  position:    line {details.line}, col {details.column} (pos_in_stream={details.pos_in_stream})\n"
        f"  unexpected:  {details.unexpected}\n"
        f"  expected:    {expected}\n"
        f"  context:\n"
        f"{_indent(details.context, '    ')}\n"
        f"  preprocessed text Lark saw (post _preprocess):\n"
        f"{_indent(details.preprocessed_text, '    ')}\n"
        f"  full lark message:\n"
        f"{_indent(details.raw_message, '    ')}"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(
            "usage: python -m argentum_press._fix_loop_gap <set_code> <project_dir>",
            file=sys.stderr,
        )
        return 2
    set_code, project_dir_s = argv
    project_dir = Path(project_dir_s)

    from .catalog import ScryfallCatalog
    from .diagnose import find_first_gap, format_ast, inspect_card
    from .lowerer import KotlinLowerer
    from .parse_cache import is_cached

    with ScryfallCatalog() as catalog:
        _log(f"fetching {set_code} from Scryfall...")
        cards = catalog.fetch(set_code)
        cache_state = (
            catalog.last_cache_state.source if catalog.last_cache_state else "?"
        )
        _log(f"fetched {len(cards)} cards (catalog cache={cache_state})")

        def _before(scanned: int, total: int, card: dict) -> None:
            # Only emit a "parsing..." line for cards that aren't in the
            # cache — those are the ones about to sit through a 1-40s
            # Earley parse and the user wants to see what's blocking.
            # Cache hits go straight to the after-parse checkmark below.
            if is_cached(card):
                return
            _log(f"[{scanned:>3}/{total}] {card['name']}  (parsing...)")

        def _after(scanned: int, total: int, card: dict, gap: Any) -> None:
            mark = "✗" if gap is not None else "✓"
            color = "red" if gap is not None else "green"
            _log(f"[{scanned:>3}/{total}] {mark} {card['name']}", color=color)

        _log("scanning for first gap...")
        report = find_first_gap(
            cards, project_dir, set_code,
            progress=_before, on_complete=_after,
        )

        if report.gap is None:
            _emit({
                "type": "result",
                "gap": None,
                "ast": None,
                "parse_error_block": None,
                "scanned": report.scanned,
            })
            return 0

        _log(f"gap found after scanning {report.scanned} card(s)")
        match = next(
            (c for c in cards if c["name"] == report.gap.card_name), None
        )
        ast_text: str | None = None
        if match is not None:
            _, card_ast = inspect_card(match, KotlinLowerer())
            if card_ast is not None:
                ast_text = format_ast(card_ast)

    pe_block = _format_parse_error_block(report.gap.parse_details)

    _emit({
        "type": "result",
        "gap": {
            "kind": report.gap.kind,
            "label": report.gap.label,
            "card_name": report.gap.card_name,
            "oracle_text": report.gap.oracle_text,
        },
        "ast": ast_text,
        "parse_error_block": pe_block,
        "scanned": report.scanned,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
