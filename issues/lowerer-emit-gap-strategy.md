# Lowerer diagnostic strategy: emit-gap vs unmodeled-rule

## Decision (2026-05-14): A'

Go with **A'** — the stub strategy, but with a three-bucket diagnostic
instead of folding stub-rooted gaps into the existing `EmitterGap`.

- `__default__` in `CardTransformer` wraps unmodeled *leaf* rules in
  `UnmodeledExpression(rule_name, raw)`. Union-type rules
  (`beingstatement`, etc.) stay passthroughs via an explicit whitelist;
  they are not stubs.
- A new `TransformerGap` kind, distinct from `EmitterGap`, fires when
  the lowerer encounters an `UnmodeledExpression`. Diagnose surfaces
  three buckets: parse-fail (lark choked), transformer-gap (AST has a
  stub), emitter-gap (AST modeled, no Kotlin handler).
- `scripts/fix-parser-gaps.sh` learns the third kind and dispatches
  `transformer-gap` → `transformer.py`, `emitter-gap` → `lowerer.py`.
  Without this update the loop would mis-route stub-rooted gaps to
  the lowerer.

**Gating**: don't ship until the AST extensions in
`issues/ast-coverage-gaps.md` have landed. Otherwise half a typical
set's cards become low-info `TransformerGap` entries, which is worse
than today's clean parse-fail report.

**Why A' over plain A**: lumping stub gaps and real emit-gaps into one
bucket hides which file to edit. Three kinds preserves the
"label tells you the fix site" property that the existing pipeline
already has for parse vs emit.

## The choice

When `argentum-press` encounters a card whose Lark parse tree
contains a grammar rule the transformer doesn't yet handle, the
**current behavior** is:

1. Transformer raises `LoweringIncomplete("unmodeled-rule:X")`.
2. `parse()` wraps this in `ParseError(kind="incomplete", message="unmodeled-rule:X")`.
3. The pipeline buckets it as **deferred-parse**, with the full
   `oracle_text` discarded — even the parts of the card the
   transformer *did* understand.

The **proposed alternative** is to wrap every unmodeled rule in a
generic stub node (e.g. `UnmodeledExpression(rule_name=X, raw=<lark_subtree>)`)
inside the `__default__` method of the `CardTransformer`. Then:

1. `parse()` succeeds and returns a `Card` whose ability list
   contains some real dataclasses plus some `UnmodeledExpression`
   stubs.
2. `KotlinLowerer` registers an `EmitterGap` handler on
   `UnmodeledExpression` that names the rule.
3. The pipeline buckets the card as **bucket-2 emit-gap** with a
   specific missing-primitive label like `unmodeled-rule:castexpression`.

## Why this matters

The "deferred-parse vs bucket-2 emit-gap" distinction is the
diagnostic the user reads at the end of every set run. Today every
unmodeled rule looks the same (lumped under "deferred (parse)").
Moving them to bucket-2 means:

- The summary's "ranked by missing argentum primitive" report
  shows exactly which transformer handlers are blocking the most
  cards.
- Partial knowledge about a card (its keyword abilities,
  recognised effects, etc.) is preserved instead of thrown away.
- The fix loop becomes mechanical: see the top-ranked
  `unmodeled-rule:X`, add the handler, re-run.

## Why it's not obviously correct

- The stub AST is lossy. Any caller that walks the AST has to be
  aware that some nodes carry zero semantic info beyond a rule name.
- Some grammar rules are union types (e.g. `beingstatement` is
  just `isstatement | hasstatement | …`) — these should be
  passthroughs, not stubs. The `__default__` would have to be
  smart about that.
- Reed's `lark.Transformer` machinery has well-defined behavior
  around `__default__`; we'd want to be explicit rather than rely
  on the catch-all.
- The lowerer's `EmitterGap` already serves a related purpose
  (known AST shape but no Kotlin emit). Conflating both into one
  bucket might mask that distinction.

## Decision needed

Two paths forward:

- **A.** Implement the stub strategy. Most BLB-like cascade
  failures (Bria) become bucket-2 instead of parse-fail. Worth
  doing once the AST extensions in
  `issues/ast-coverage-gaps.md` have stabilised, so the
  stubs only catch the *rare* grammar rules we haven't modeled
  yet.

- **B.** Keep current behavior. Each unmodeled rule remains a
  parse failure until a real handler is added. Simpler invariant
  ("the AST always means something"), worse diagnostics.

I'd lean A but only after some AST extensions land — otherwise
half the cards in a typical set become low-info bucket-2 entries,
which is worse than today's clean "parse failed" report.
