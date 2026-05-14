# Experiment: combfind retrieval inside playbook mode

The `combfind-integration` worktree (`/private/tmp/combfind-argentum-press-worktree`,
branch `combfind-integration`, tip `259a2c2` as of 2026-05-14) took the
opposite path from playbook mode: it **deleted the entire playbook
package** and added a combfind-ranked "closest analog" block to the
freeform `claude -p` prompt instead. Four-gap A/B (`blb-1`, `dft-1`,
`fdn-1`, `spm-1`) showed turns dropping (10→8, 11→8, 20→13) and searches
dropping (4→2, 5→4, 8→5) at ~$0.02–0.10 extra cost per gap and similar
wall-clock.

We don't want to give up playbook's structured DAG + libcst edits. But
the *retrieval* signal — "give the LLM the semantically closest
existing handler rather than the first N grep matches" — is orthogonal
to the DAG and worth trying inside playbook.

This is a parked experiment, not active work. Pick it up when we have
time and a measurable hypothesis.

## Hypothesis

Playbook's L3/L4/L5 (lower) and the unmodeled-rule U-steps would
make fewer LLM turns and fewer agent-side greps if their exemplar
lists were combfind-ranked (closest analog first) instead of
"first-N in file order."

The biggest absolute v6 wins were on **unmodeled-rule** gaps
(`blb-1` `becomesstatement`: 10→8 turns 4→2 greps; `fdn-1`
`paylifeexpression`: 20→13 turns 8→5 greps). Suffix-aware retrieval
in v6 was tuned for these.

## Phases (when we resume)

**Phase 1 — Port the retrieval substrate.** No playbook behavior
change yet.

- Cherry-pick `scripts/find_analog.py` and `tests/test_find_analog.py`
  from `combfind-integration`. Pure library, no playbook coupling.
- Cherry-pick `reindex_combfind()` and the `--combfind-db` flag
  plumbing into `scripts/fix_parser_gaps.py`. Default DB path
  `${XDG_CACHE_HOME}/argentum-press/combfind.db`. Soft-fail when the
  `combfind` binary or DB is missing — orchestrator must keep going.
- Cherry-pick the v6 recorder fix (`n_bash_grep` column +
  `_BASH_GREP_RE` in `experiments/`) so the A/B is honest regardless
  of mode. This is the v6 "honest grep metric" change in
  `259a2c2`.

**Phase 2 — Wire combfind into playbook context-gathering.**

- Add `context.collect_lowerer_exemplars_ranked(label, db_path)` next
  to the existing `collect_lowerer_exemplars()`. Call
  `find_analog._find_analog(bare, None, db_path)` to get the top
  analog, promote that handler/branch to the front of
  `LowererExemplars.register_handlers`, keep the unranked tail behind
  it so existing slicing in `filter_exemplars_for_pattern` still has
  material.
- Plumb optional `combfind_db: str | None` through
  `playbook.lower.run`, `playbook.parse_error.run`,
  `playbook.unmodeled_rule.run`, and `context.gather*`. `None` ⇒
  today's behavior.
- For unmodeled-rule and parse-error gaps, add the analogous
  `recent_transformer_exemplars_ranked()`. This is where v6 saw the
  biggest absolute wins.

**Phase 3 — Make it switchable for A/B.**

- New CLI flag on `scripts/fix_parser_gaps.py`:
  `--playbook-retriever={greplike,combfind}`, default `greplike`.
  Playbook's analog of `--prompt-variant`.
- Strategy constructors pass the flag through to `playbook.*.run`.
- Tag the runs.tsv `description` column with `retriever=combfind`
  so `experiments/ab_replay*.sh` and `scripts/diff_experiments.py`
  can split rows.

**Phase 4 — Measure.**

- Re-capture the four v6 gaps under main HEAD as **playbook** gaps
  (they're freeform on the combfind branch because playbook is gone
  there).
- Run `--mode playbook` with `retriever=greplike` vs
  `retriever=combfind` across those four gaps × N repeats.
- Look at: `n_searches` (honest grep metric), `num_turns`, `wall_s`,
  `cost_usd`, `outcome`.
- If positive, expand to a full-set run (BLB or similar) and decide
  whether combfind becomes the playbook default.

## Pointers

- Combfind worktree: `/private/tmp/combfind-argentum-press-worktree`,
  branch `combfind-integration`, tip `259a2c2`.
- Combfind A/B results: `experiments/runs/multi-v6/` on that branch
  (`blb-1`, `dft-1`, `fdn-1`, `spm-1`, each with `baseline/` and
  `combfind/` subdirs holding `runs.tsv` and a per-gap JSONL).
- Top combfind commits worth understanding before resuming:
  - `259a2c2` — v6 honest grep metric + suffix-aware retrieval.
  - `1125a1c` — v5 hybrid (orchestrator-bake + CLI drill-down).
  - `9ead283` — replace eager block with on-demand CLI.
  - `01cb975` — original combfind prompt variant + concept-map context.

## Decisions deferred

- Which gap kinds to target first (lower / unmodeled-rule /
  parse-error). 2026-05-14 conversation parked this — pick when we
  resume based on where playbook itself is weakest at the time.
- Whether to also pull a `find_analog` result cache. Combfind's
  branch doesn't have one; only consider if cold-start cost shows
  up in measurement.
- Failure behavior when combfind is missing or its DB is empty: silent
  fallback to the unranked exemplars vs hard-fail. Default
  recommendation is silent fallback with a one-line stderr note.
