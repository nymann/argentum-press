# argentum-press

Turns Scryfall card JSON into Kotlin DSL source files for the
sibling project `argentum-engine`. The CLI entry point is
`argentum-press` (see `pyproject.toml`); the main subcommands are
`add-set` (full pipeline) and `diagnose` (first-failure surfacing
for the fix-loop).

## Pipeline

`pipeline.AddSetPipeline` runs three phases:

1. **Classify** (`classify.py`) — parallel via `ProcessPoolExecutor`.
   Each card goes through `parser.parse()` and the lowerer's
   "can-handle" check, landing in one of:
   - **Bucket1** — parses + lowers, ready to render
   - **Bucket2** — parses but lowerer has no handler for some AST
     node (`EmitterGap`); the missing-node label is reported
   - **deferred-parse** — `parser.parse()` returned `ParseError`
2. **Render** — Bucket1 cards go through `template.render` into
   Kotlin source.
3. **Report** — counts, ranked "missing argentum primitive" list,
   etc.

`diagnose` (`diagnose.py`) is the serial first-failure variant the
fix-loop runs.

## Parser pipeline (the interesting part)

```
oracle_text
   │  preprocessor (inlined in transformer.py:_preprocess — A-prefix
   │  workaround, pronoun expansion, name → ~ substitution)
   ▼
lark.Lark(grammar.getGrammar(), …)   ← grammar/grammar.py is a single
   │                                    941-line hardcoded Lark string
   ▼
CardTransformer  (parser/transformer.py — lark.Transformer subclass)
   │  per-rule handlers build AST dataclasses; unmodeled rules raise
   │  LoweringIncomplete("unmodeled-rule:X")
   ▼
parser/ast/*.py  (frozen dataclasses, slots=True)
   │
   ▼
KotlinLowerer  (lowerer.py — @register handlers per AST node;
                missing handler → EmitterGap)
```

### Where to edit when a card fails

| Failure | File |
|---|---|
| `parse-error:...` (lark itself can't tokenize/parse) | `parser/grammar/grammar.py` |
| `unmodeled-rule:X` (lark parses, transformer has no handler) | `parser/transformer.py` (+ maybe new dataclass in `parser/ast/`) |
| `EmitterGap` on a known AST node | `lowerer.py` (`@register` a handler) |

### Grammar edits go in `grammar.py`

`grammar.py`'s `getGrammar()` returns the entire Lark grammar as a
941-line string. It's the sole source. There's no separate modular
source, no regenerate step — edit it directly. (Upstream
`rmmilewi/mtgcompiler` had a Grammarian DSL with one `.grm` file per
concept area; we evaluated importing it in 2026-05 and decided
against — see `issues/parser-small-fixes.md` for the reasoning.)

## Triage workflow

- `INBOX.md` — one-line GTD pointers at issue files
- `issues/*.md` — one file per open thread (parser gaps, AST
  coverage, architectural calls)
- `scripts/fix_parser_gaps.py <set> <project-dir>` — Python
  orchestrator: imports `argentum_press.diagnose` directly, computes
  per-gap context deterministically (structured lark error,
  preprocessed text, grammar rule index + definition for
  `unmodeled-rule:X`, ranked handler map and gap-class definition for
  lower gaps, engine DSL hints, file sizes, recent commits) and hands
  it to a fresh `claude -p` session. Runs pytest itself after the
  agent exits and aborts on red. Commits per iteration. Bails on
  no-progress (same label twice). Flags: `--dry-run`,
  `--max-iter N`, `--no-commit`, `--allow-dirty`.

The orchestrator owns commits — the per-iteration `claude -p`
sessions must not commit (they get a fresh context each time and
have no view of the prior iteration's work).
