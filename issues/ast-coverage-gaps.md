# AST coverage gaps surfaced by the absorption

When the AST port and Lark→AST transformer landed in `756d5ae`, the
subagents flagged a handful of places where Reed's original modeling
didn't quite line up with what the grammar emits today. None are
blocking the current pipeline (the transformer raises
`LoweringIncomplete` and the pipeline buckets it cleanly), but each
one is a small extension that would let real cards round-trip.

## Missing fields on existing dataclasses

- **`CardDrawExpression.player`**. Reed's `MgCardDrawExpression`
  carried a `player` field; the dataclass port dropped it. Cards
  like "target opponent draws a card" lose the player on the way
  through. Add `player: Expression | None = None`; the transformer
  already extracts it for `drawexpression`.

- **`RevealExpression.subject`**. Empty dataclass today; the grammar
  emits a subject (`reveal <declref>`). The unparser has a `TODO`
  marker on it for the same reason. Add `subject: Expression`.

- **`GainLoseExpression` / `AddRemoveExpression` body fields**. Reed
  left these stubbed in the source and the port carried that forward.
  The unparser has explicit TODO markers; calling `unparse()` on
  either raises. Add fields per Reed's grammar usage.

## `ReinforceAbility.caliber` synthesised from nothing

`ReinforceAbility(caliber, cost)` exists, but the grammar rule
`kwreinforce: "reinforce" cost` only captures the cost (not the
caliber number). The transformer synthesises a placeholder
`NumberValue("x", CUSTOM)` for `caliber`. Either:

- (a) Update the grammar to capture `reinforce N — cost` properly
  (real reinforce syntax is `Reinforce N—{cost}`), then read both,
  or
- (b) Make `ReinforceAbility.caliber: Expression | None = None` and
  let the transformer pass `None` honestly.

## No dedicated dataclasses for these shapes

- **`CastExpression`**. Grammar has it
  (`castexpression: playerdeclref? "cast"["s"] declarationorreference …`)
  but the AST has no node. Bria's parse cascades through this.

- **`CastPostfix`** (`spells you cast`). Same story — grammar emits,
  AST doesn't model. Would naturally pair with `CastExpression`.

- **`GetsPTExpression`**. The grammar's `getsptexpression` (the
  `<subject> gets +N/+M` shape) currently surfaces as a
  `DescriptionExpression([subject, PTExpression])` from the
  transformer, which preserves surface text but loses semantics.
  Reed had `MgGetPTExpression(subject, pt_change, duration)` — port
  it.

- **`WithoutExpression`**. Reed had one; the AST port collapsed it
  into `WithExpression` as a stand-in. The transformer treats them
  as the same, which is lossy for any card that uses both. Add a
  dedicated dataclass.

## How to triage

Suggested order if we ever do this in one pass:

1. The three "missing field" fixes (10 minutes each, mechanical).
2. `WithoutExpression` (separates a lossy alias).
3. `GetsPTExpression` (replaces the `DescriptionExpression` hack).
4. `CastExpression` + `CastPostfix` (unblocks Bria's cascade — see
   `issues/blb-residual-cards.md`).
5. The `ReinforceAbility.caliber` grammar update (real grammar work).
