"""argentum-press's parser package.

Public entry points live in :mod:`argentum_press.parser.transformer`:

* :func:`parse` - lower a Scryfall dict or raw oracle text to a
  :class:`ParseResult`.
* :func:`transform` - lower an already-parsed Lark ``cardtext`` tree to a
  :class:`Card`.
* :class:`ParseResult`, :class:`ParseError`, :class:`LoweringIncomplete` -
  return / error shapes.
"""
from argentum_press.parser.transformer import (
    LoweringIncomplete,
    ParseError,
    ParseResult,
    parse,
    transform,
)

__all__ = [
    "LoweringIncomplete",
    "ParseError",
    "ParseResult",
    "parse",
    "transform",
]
