"""argentum-press command-line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import _ast
from .catalog import ScryfallCatalog
from .lowerer import KotlinLowerer
from .outcome import CardOutcome, DeferredEmitterGap, DeferredParseFailed, Emitted
from .pipeline import AddCardPipeline, FilesystemWriter, Parser
from .verify import CompileOk, CompileVerifier


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="argentum-press",
        description="Compile MTG cards into argentum-engine Kotlin DSL.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    add = subparsers.add_parser(
        "add-card",
        help="Fetch a Scryfall set and emit Kotlin sources into argentum-engine.",
    )
    add.add_argument("--set", required=True, help="Scryfall set code, e.g. 'blb'.")
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
        help="Skip the gradle compileKotlin step. Useful while mtgcompiler "
        "is being adapted and we want to inspect the emitted files first.",
    )

    args = parser.parse_args(argv)

    if args.command == "add-card":
        return _run_add_card(args)

    return 1


def _run_add_card(args: argparse.Namespace) -> int:
    parser_impl = _resolve_parser()
    if parser_impl is None:
        print(
            "mtgcompiler is not yet exposing the parse() interface argentum-press "
            "expects. Adapt it (or wire a stub) before running add-card.",
            file=sys.stderr,
        )
        return 2

    verifier: CompileVerifier | _NoopVerifier
    if args.skip_verify:
        verifier = _NoopVerifier()
    else:
        verifier = CompileVerifier(args.project_dir, java_home=args.java_home)

    with ScryfallCatalog() as catalog:
        pipeline = AddCardPipeline(
            catalog=catalog,
            parser=parser_impl,
            lowerer=KotlinLowerer(),
            verifier=verifier,  # type: ignore[arg-type]
            writer=FilesystemWriter(),
            project_dir=args.project_dir,
            set_code=args.set,
        )
        outcomes = pipeline.run(limit=args.limit)

    _print_report(outcomes, args.set)
    return 0


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


class _NoopVerifier:
    """Stand-in verifier used with --skip-verify."""

    def verify(self) -> CompileOk:
        return CompileOk()


def _print_report(outcomes: list[CardOutcome], set_code: str) -> None:
    emitted = [o for o in outcomes if isinstance(o, Emitted)]
    parse_failed = [o for o in outcomes if isinstance(o, DeferredParseFailed)]
    gaps = [o for o in outcomes if isinstance(o, DeferredEmitterGap)]

    print(f"\n=== argentum-press: set={set_code} ===")
    print(f"  emitted:           {len(emitted):>4}")
    print(f"  deferred (parse):  {len(parse_failed):>4}")
    print(f"  deferred (gap):    {len(gaps):>4}")

    if gaps:
        print("\n  Emitter gaps (next @register handlers to write):")
        unique: dict[str, int] = {}
        for gap in gaps:
            unique[gap.missing_node] = unique.get(gap.missing_node, 0) + 1
        for node_type, count in sorted(unique.items(), key=lambda kv: -kv[1]):
            print(f"    {count:>4}  {node_type}")


if __name__ == "__main__":
    sys.exit(main())
