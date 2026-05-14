# experiments/

Branch-scoped fix-loop measurement. Inspired by the autoresearch pattern
(`/Users/knj/code/github.com/karpathy/autoresearch`) but adapted to our shape:
the human edits the prompt; the inner agent runs the lever; the orchestrator
records every iteration so a `git checkout` worth of work can be A/B'd
against a different prompt variant.

## Layout

```
experiments/
├── gaps/              # captured fix-loop inputs (one JSON per slug)
│   └── <slug>.json
└── runs/              # per-experiment-tag directories from --record
    └── <tag>/
        ├── runs.tsv                                 # one row per iteration
        ├── summary.tsv                              # aggregated per gap
        └── <iso>-<iter>-<slug>.jsonl                # agent transcript
        └── <iso>-<iter>-<slug>.scan.jsonl           # gap-finder transcript
```

## Workflows

### Capture a gap

```sh
uv run scripts/fix_parser_gaps.py spm ../argentum-engine \
  --capture-gap my-slug
```

This runs the gap finder once, writes `experiments/gaps/my-slug.json` (with
the ref commit baked in so replays can refuse to run against a drifted
parser state), and exits without invoking claude.

### Record a live run

```sh
uv run scripts/fix_parser_gaps.py spm ../argentum-engine \
  --record experiments/runs/<tag>/
```

Same fix-loop behaviour as before, plus per-iteration NDJSON + a `runs.tsv`
row. Recording is opt-in; without `--record` nothing extra is written.

### Replay a captured gap

```sh
uv run scripts/fix_parser_gaps.py --replay my-slug \
  --record experiments/runs/<tag>/
```

Pulls the gap from `experiments/gaps/my-slug.json`, renders the prompt,
invokes claude, runs pytest, records the row — then `git reset --hard` to
the pre-claude HEAD and `git clean -fd` to wipe any new files. The point
is reproducible deltas: replay the same gap 3+ times under different
prompt variants and compare the resulting cost / wall / num_turns columns.

### Multi-replay experiment

```sh
uv run scripts/run_experiment.py --tag spm-baseline --repeats 3 \
  --description "baseline prompt, 2026-05-14"
```

See `scripts/run_experiment.py --help`.

### Compare two experiment tags

```sh
uv run scripts/diff_experiments.py \
  experiments/runs/spm-baseline/summary.tsv \
  experiments/runs/spm-h1-no-handler-map/summary.tsv
```

## Gap JSON schema

```json
{
  "set_code": "spm",
  "gap": {
    "kind": "parse",
    "label": "unmodeled-rule:colorandexpr",
    "card_name": "Friendly Neighborhood",
    "oracle_text": "..."
  },
  "ast_text": "...optional, populated for lower gaps...",
  "parse_error_block": "...optional, populated for parse-error gaps...",
  "ref_commit": "<sha>"
}
```

Hand-editing is fine, just keep `ref_commit` honest — replays refuse to run
when HEAD doesn't match.
