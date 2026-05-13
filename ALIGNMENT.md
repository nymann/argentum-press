# Alignment: argentum-press ↔ mtgcompiler

Response to `mtgcompiler/DESIGN.md`. Short version: **yes, go.** Option A + caveat is right, with these clarifications.

## Architecture (recommendation)

Agreed — Option A, internal `mtgcompiler/_lower.py`, public `mtgcompiler.ast` matches argentum-press's `_ast.py`. One adjustment to the framing: **mtgcompiler is the source of truth** for those classes — `argentum-press/_ast.py` is a transitional copy that will collapse to `from mtgcompiler.ast import *` the moment your package ships them. So don't think of it as mtgcompiler "mirroring" argentum-press; think of it as argentum-press currently shadowing what mtgcompiler will own.

## Q1 — Keyword enum & TriggerCondition can both grow

Make them open from your side. For parametric keywords (`equip {N}`, `ward {N}`, `protection from <X>`) add `parameter: str | None = None` to `KeywordAbility`. argentum-press's lowerer will raise `EmitterGap` for the parametric forms on day one (we don't yet know how to render them in argentum's DSL); that's the right failure mode — keyword is parsed, lowering is the next bucket to fill. For triggers, same deal: grow the enum (`CAST_SPELL`, `LEAVES_BATTLEFIELD`, `BEGINNING_OF_COMBAT`, `END_OF_COMBAT`, ...). Anything you can't classify returns `ParseError("incomplete")`.

## Q2 — StaticAbility: not day one

Return `ParseError(kind="incomplete", message="static-ability")` for `Other creatures you control get +1/+1`-style text. We'll add `StaticAbility(effects: tuple[Effect, ...])` later when an emitter rule actually wants it. Failing in mtgcompiler is correct here — putting it in the AST with no consumer is the abstraction trap.

## Q3 — `ParseError.alternatives`: confirmed dropped

If you hit ambiguity, return `ParseError(kind="ambiguous", message="<short context>")` without alternatives. We'll re-add the field the day a consumer actually needs to pick. Doubt that day comes.

## Q4 — Day-one coverage: your list ✓, plus one

Your eight bullets match `lowerer.py` exactly. Add **`Shuffle ~ into its owner's library` as part of a dies/death trigger** (`ShuffleSelfIntoLibrary`) — Alabaster Dragon-shaped. That's the ninth pattern. Everything else: `ParseError("incomplete")`. Sound minimum.

## Q5 — `slots=True`, no `_mg` escape hatch: confirmed

I don't want backreferences from semantic to syntactic. If we ever need to surface the parse tree for debugging, that's a separate sidecar — `parse(card, return_mg=True) -> (ParseResult, MgTree)` or a flag — not a field on the AST.

## Q6 — `parse(card: dict)` and what `Card` carries

`Card.abilities` stays the whole story. mtgcompiler reads `oracle_text` and `name` (for normalization → `~`) out of the dict; it ignores the rest. **Scryfall metadata stays on the dict**, not on the AST — that's how `pipeline._process` is structured today (`card["name"]`, `card["power"]`, etc. go directly to `template.render`, the AST only contributes the body). Don't pull power/toughness/typeline into `Card`; you'd just be duplicating data the dict already carries unambiguously.

## One thing your doc didn't ask but worth pinning

**Error positions are offsets into the preprocessed text**, not the raw `oracle_text`. mtgcompiler does the preprocessing, so it owns the coordinate system. argentum-press just prints `ParseError.position` verbatim for now.

## Go

The "what I'd do" plan reads correctly to me — replace the syntactic-ish AST scaffold with the `_ast.py`-shaped classes inside `mtgcompiler/ast/__init__.py`, put the Mg→semantic rules in `mtgcompiler/_lower.py`, wire `parse()` + `ParseResult` to match. When you ship, I'll delete `argentum-press/_ast.py` and replace it with the re-export.
