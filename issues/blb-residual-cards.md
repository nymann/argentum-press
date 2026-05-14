# BLB residual: 3 cards still stuck after parser absorption

After commit `921a501` (which absorbed the parser and added a few
small transformer handlers surfaced by the smoke), `argentum-press
add-set blb` still defers three cards. Two need grammar work that
is entangled with the parked LALR conversion in mtgcompiler; one
needs an AST extension + chained transformer handlers that the
lowerer wouldn't emit anyway.

## A-Heartfire Hero — grammar gap on time-clause

Oracle text:
```
Valiant — Whenever Heartfire Hero becomes the target of a spell or
ability you control for the first time each turn, put a +1/+1 counter
on it.
When Heartfire Hero dies, it deals damage equal to its power to each
opponent.
```

The Alchemy `A-` prefix bug was fixed in `921a501` (preprocessor now
strips it before name → `~` substitution). The card now parses past
col 20 but fails at col 79 on:

```
parse-error: No terminal matches 't' in the current parser context
```

Col 79 lands inside `for the first time each turn,` — the grammar
doesn't model the `for the first time each turn` modifier on a
trigger. Adding it is grammar work, not transformer work.

## Byrke, Long Ear of the Law — grammar gap, can't parse at all

Oracle text:
```
Vigilance
When Byrke enters, put a +1/+1 counter on each of up to two target
creatures.
Whenever a creature you control with a +1/+1 counter on it attacks,
double the number of +1/+1 counters on it.
```

Lark bottoms out with `Unexpected end-of-input. Expected one of: …`.
Probable cause: `up to two target creatures` (the `up to N target X`
quantifier) and/or `with a +1/+1 counter on it` (the `with-counter`
postfix predicate). The grammar has skeletal support for counters
but not in this composition.

Same parked LALR/disambiguation work as `mtgcompiler/issues/parser-performance.md`.

## Bria, Riptide Rogue — transformer cascade

Oracle text:
```
Prowess (Whenever you cast a noncreature spell, this creature gets
+1/+1 until end of turn.)
Other creatures you control have prowess. (If a creature has multiple
instances of prowess, each triggers separately.)
Whenever you cast a noncreature spell, target creature you control
can't be blocked this turn.
```

Lark parses it. The transformer has a chain of missing rule
handlers. Each handler added in `921a501` (`controlpostfix`,
`hasstatement`, `beingstatement`, `abilitysequencestatement`)
unblocks one layer and surfaces the next:

```
unmodeled-rule:controlpostfix       (fixed)
unmodeled-rule:beingstatement       (fixed via hasstatement)
unmodeled-rule:abilitysequencestatement   (fixed)
unmodeled-rule:castexpression       (current stop)
```

`castexpression` next would need a new `CastExpression` dataclass
(no node exists for it today) — see `issues/ast-coverage-gaps.md`.
After that, `castpostfix`, `gaincontrolexpression`, and the
"can't be blocked" predicate are all likely to surface.

Worth fixing only if we also decide what `KotlinLowerer` should
emit for "Other creatures you control have prowess" — argentum-engine
doesn't have a Kotlin DSL surface for granted abilities yet, so
the card would just shift from parse-fail to bucket-2 emit-gap.
That shift is itself valuable (richer diagnostic) but it's an
explicit choice — see `issues/lowerer-emit-gap-strategy.md`.

## Where they live

argentum-engine implemented all three manually upstream while the
parser was being absorbed, so they're already in the engine; this
issue is only about argentum-press becoming self-sufficient for
*future* sets of similar shape.
