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

- `INBOX.md` — one-line GTD pointers at issue files.
- `issues/*.md` — one file per open thread (parser gaps, AST
  coverage, architectural calls).
- `scripts/fix_parser_gaps.py <set> <project-dir>` — Python
  orchestrator. Each iteration spawns
  `python -m argentum_press._fix_loop_gap` as a fresh subprocess so the
  agent's last edit to grammar/transformer/lowerer takes effect
  (in-process re-import would keep the stale module via `sys.modules`).
  The worker streams NDJSON progress + a final result event. The
  orchestrator computes per-gap context deterministically (rule
  definition, handler map, engine DSL hints, file sizes, recent
  commits), hands it to a fresh `claude -p`, runs pytest after the
  agent exits and aborts on red. Commits per iteration; bails on
  no-progress (same label twice). Flags: `--dry-run`, `--max-iter N`,
  `--no-commit`, `--allow-dirty`.
- `argentum_press.parse_cache` — opt-in disk cache
  (`ARGENTUM_PARSE_CACHE=1`) for `ParseResult`, keyed by
  `sha256(name + oracle_text)`. The fix-loop turns it on; tests and
  `add-set` get the uncached path. After each parse-kind fix the
  orchestrator calls `invalidate_label(L)` to drop matching entries;
  everything else stays cached. Earley parses are 1–40s/card, so the
  cache is the difference between re-scanning the whole set every
  iteration and only re-parsing the cards the latest fix affects.

The orchestrator owns commits — the per-iteration `claude -p`
sessions must not commit (they get a fresh context each time and
have no view of the prior iteration's work).
