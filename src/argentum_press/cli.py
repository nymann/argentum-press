"""argentum-press command-line entry point.

Usage:

    argentum-press add-set <code> --project-dir <path> [--limit N]
                                  [--java-home PATH] [--skip-verify]

The flow has four phases (see pipeline.AddSetPipeline):
  1. triage      — subtract cards argentum-engine already has
  2. classify    — bucket 1 (we can emit) vs bucket 2 (needs engine extension)
  3. emit        — write bucket-1 .kt files
  4. verify      — single gradle compileKotlin run (optional)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

from .catalog import ScryfallCatalog
from .lowerer import KotlinLowerer
from .parser import ParseResult
from .pipeline import AddSetPipeline, FilesystemWriter, Parser, PipelineReport
from .reporter import ConsoleReporter
from .verify import CompileVerifier


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="argentum-press",
        description="Compile MTG cards into argentum-engine Kotlin DSL.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser(
        "add-set",
        help="Fetch a Scryfall set and emit missing cards into argentum-engine.",
    )
    add.add_argument("set", help="Scryfall set code, e.g. 'blb'.")
    add.add_argument(
        "--project-dir",
        required=True,
        type=Path,
        help="Path to the argentum-engine checkout.",
    )
    add.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N cards (default: all).",
    )
    add.add_argument(
        "--java-home",
        default=None,
        help="Override JAVA_HOME for the gradle compile verifier. Default: "
        "best-effort discovery of an OpenJDK 21 install under Homebrew.",
    )
    add.add_argument(
        "--skip-verify",
        action="store_true",
        help="Skip phase 4 (gradle compileKotlin). Useful for fast iteration "
        "while inspecting bucket-1 output.",
    )
    add.add_argument(
        "--refresh",
        action="store_true",
        help="Force a re-fetch from Scryfall, bypassing the on-disk cache at "
        "~/.cache/scryfall.",
    )
    add.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 1,
        help="Process workers for the classify phase. mtgcompiler's Earley "
        "parser is ~1s/card so this phase dominates wall-clock; cpu_count is "
        "the default. Set 1 to disable parallelism (useful for debuggers).",
    )

    diag = subparsers.add_parser(
        "diagnose",
        help="Find the first parse/lower gap in a set and emit it as JSON.",
        description="Walk the set serially and stop at the first card the "
        "parser/lowerer cannot handle yet. JSON to stdout is shaped for a "
        "bash fix-loop (see argentum_press.diagnose).",
    )
    diag.add_argument("set", help="Scryfall set code, e.g. 'spm'.")
    diag.add_argument(
        "--project-dir",
        required=False,
        default=None,
        type=Path,
        help="Path to the argentum-engine checkout (used to skip already-"
        "implemented cards during triage). Required unless --card is set.",
    )
    diag.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Look at most N cards (default: all). Diagnose short-circuits "
        "on the first gap, so --limit is mainly useful for tests.",
    )
    diag.add_argument(
        "--refresh",
        action="store_true",
        help="Force a re-fetch from Scryfall, bypassing the on-disk cache.",
    )
    diag.add_argument(
        "--card",
        default=None,
        help="Diagnose only the named card (matched by name; falls back to "
        "the front-face name for split/transform cards). Bypasses the "
        "already-implemented and basic-land triage filters — i.e. always "
        "reports the parse/lower outcome of this specific card. Useful for "
        "the fix-loop: after editing, confirm the gap label moved.",
    )
    diag.add_argument(
        "--ast",
        action="store_true",
        help="Include a pretty-printed AST of the parsed card in the JSON "
        "output (as the top-level ``ast`` field). Multi-line — pipe through "
        "``jq -r '.ast'`` for readable output. Useful when the gap label is "
        "a structural node (CompoundStatement, IfStatement, etc.) and you "
        "need to see where in the tree the missing handler sits. Requires "
        "--card.",
    )

    args = parser.parse_args(argv)

    if args.command == "add-set":
        return _run_add_set(args)
    if args.command == "diagnose":
        return _run_diagnose(args)

    return 1


def _run_add_set(args: argparse.Namespace) -> int:
    parser_impl = _resolve_parser()
    if parser_impl is None:
        print(
            "mtgcompiler is not yet exposing the parse() interface argentum-press "
            "expects. Adapt it (or wire a stub) before running add-set.",
            file=sys.stderr,
        )
        return 2

    verifier = None
    if not args.skip_verify:
        verifier = CompileVerifier(args.project_dir, java_home=args.java_home)

    reporter = ConsoleReporter()

    with ScryfallCatalog(force_refresh=args.refresh) as catalog:
        pipeline = AddSetPipeline(
            catalog=catalog,
            parser=parser_impl,
            lowerer=KotlinLowerer(),
            writer=FilesystemWriter(),
            project_dir=args.project_dir,
            set_code=args.set,
            verifier=verifier,
            reporter=reporter,
            workers=args.workers,
        )
        report = pipeline.run(limit=args.limit)

    _print_final_summary(report)
    return 1 if report.compile_stderr else 0


def _run_diagnose(args: argparse.Namespace) -> int:
    from . import existing
    from .diagnose import (
        DiagnoseReport,
        find_first_gap,
        format_ast,
        inspect_card,
    )

    if args.card is None and args.project_dir is None:
        print("--project-dir is required unless --card is set.", file=sys.stderr)
        return 2
    if args.ast and args.card is None:
        print("--ast requires --card.", file=sys.stderr)
        return 2

    with ScryfallCatalog(force_refresh=args.refresh) as catalog:
        cards = catalog.fetch(args.set)
        if args.limit is not None:
            cards = cards[: args.limit]

        if args.card is not None:
            match = next(
                (
                    c
                    for c in cards
                    if c["name"] == args.card
                    or existing.front_face(c["name"]) == args.card
                ),
                None,
            )
            if match is None:
                print(
                    f"card {args.card!r} not found in set {args.set!r}.",
                    file=sys.stderr,
                )
                return 2
            gap, card_ast = inspect_card(match, KotlinLowerer())
            ast_repr = format_ast(card_ast) if args.ast and card_ast is not None else None
            report = DiagnoseReport(
                set_code=args.set, scanned=1, gap=gap, ast=ast_repr
            )
        else:
            report = find_first_gap(cards, args.project_dir, args.set)

    print(report.to_json())
    return 0


def _resolve_parser() -> Parser | None:
    """Return the builtin argentum_press.parser as the serial-path parser.

    The Parser protocol is satisfied by a thin object delegating to
    argentum_press.parser.parse — same shape the worker subprocess uses."""
    from argentum_press import parser as _parser

    class _BuiltinParser:
        def parse(self, card: dict[str, Any]) -> ParseResult:
            return _parser.parse(card)

    return _BuiltinParser()


def _print_final_summary(report: PipelineReport) -> None:
    """The reporter has already streamed per-phase output during the run; this
    is the closing summary that ranks bucket-2 gaps so the user knows what to
    fix next."""
    print(f"\n=== summary: set={report.set_code} ===")
    print(f"  already implemented:   {len(report.already_implemented):>4}")
    print(f"  deferred (parse):      {len(report.deferred_parse):>4}")
    print(f"  bucket 2 (needs ext.): {len(report.bucket_2):>4}")
    print(f"  bucket 1 emitted:      {len(report.emitted):>4}")
    basics = report.emitted_basic_lands
    if basics is not None:
        print(f"  basic-land printings:  {basics.count:>4}  -> {basics.path}")

    if report.bucket_2:
        print("\n  Bucket-2 ranked by missing argentum primitive:")
        gaps_by_node: dict[str, int] = {}
        for gap in report.bucket_2:
            gaps_by_node[gap.missing_node] = gaps_by_node.get(gap.missing_node, 0) + 1
        for node_type, count in sorted(gaps_by_node.items(), key=lambda kv: -kv[1]):
            print(f"    {count:>4}  {node_type}")


if __name__ == "__main__":
    sys.exit(main())
