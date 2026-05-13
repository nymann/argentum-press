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
import sys
from pathlib import Path
from typing import Any

from . import _ast
from .catalog import ScryfallCatalog
from .lowerer import KotlinLowerer
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

    args = parser.parse_args(argv)

    if args.command == "add-set":
        return _run_add_set(args)

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
        )
        report = pipeline.run(limit=args.limit)

    _print_final_summary(report)
    return 1 if report.compile_stderr else 0


def _resolve_parser() -> Parser | None:
    """Look for an installed mtgcompiler that exposes the parse() contract."""
    try:
        import mtgcompiler  # type: ignore[import-not-found]
    except ImportError:
        return None
    parse = getattr(mtgcompiler, "parse", None)
    if parse is None:
        return None

    class _MtgCompilerParser:
        def parse(self, card: dict[str, Any]) -> _ast.ParseResult:
            return parse(card)  # type: ignore[no-any-return]

    return _MtgCompilerParser()


def _print_final_summary(report: PipelineReport) -> None:
    """The reporter has already streamed per-phase output during the run; this
    is the closing summary that ranks bucket-2 gaps so the user knows what to
    fix next."""
    print(f"\n=== summary: set={report.set_code} ===")
    print(f"  already implemented:   {len(report.already_implemented):>4}")
    print(f"  deferred (parse):      {len(report.deferred_parse):>4}")
    print(f"  bucket 2 (needs ext.): {len(report.bucket_2):>4}")
    print(f"  bucket 1 emitted:      {len(report.emitted):>4}")

    if report.bucket_2:
        print("\n  Bucket-2 ranked by missing argentum primitive:")
        gaps_by_node: dict[str, int] = {}
        for gap in report.bucket_2:
            gaps_by_node[gap.missing_node] = gaps_by_node.get(gap.missing_node, 0) + 1
        for node_type, count in sorted(gaps_by_node.items(), key=lambda kv: -kv[1]):
            print(f"    {count:>4}  {node_type}")


if __name__ == "__main__":
    sys.exit(main())
