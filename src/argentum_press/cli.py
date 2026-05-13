"""argentum-press command-line entry point."""

from __future__ import annotations

import argparse
import sys


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
        help="Path to the argentum-engine checkout.",
    )
    add.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process at most N cards (default: all).",
    )

    args = parser.parse_args(argv)

    if args.command == "add-card":
        # Pipeline wiring lands in task #26.
        print(f"add-card: set={args.set} project_dir={args.project_dir} limit={args.limit}")
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
