# Inbox

GTD-style scratchpad. Each line is a one-look pointer at an issue file
under `issues/` — capture here first, triage / schedule / discard later.
Nothing in this file should ever describe an action in detail; that's
what the linked file is for.

- [ ] [BLB residual: 3 cards still stuck after parser absorption](issues/blb-residual-cards.md)
  — A-Heartfire Hero (grammar, "for the first time each turn"), Byrke
  (grammar, Unexpected end-of-input), Bria (transformer cascade,
  unmodeled-rule:castexpression). Two need grammar work entangled with
  the parked LALR conversion; Bria needs new AST dataclasses + chained
  transformer handlers that the lowerer doesn't yet emit.

- [ ] [AST coverage gaps surfaced by the absorption](issues/ast-coverage-gaps.md)
  — Missing fields on existing dataclasses (`CardDrawExpression.player`,
  `RevealExpression.subject`, body-less `GainLoseExpression` and
  `AddRemoveExpression`), `ReinforceAbility.caliber` synthesised because
  the grammar doesn't capture it, no dedicated `CastExpression` /
  `WithoutExpression` / `GetsPTExpression` (transformer fakes them with
  unrelated nodes today).

- [ ] [Parser-layer small fixes carried over from Reed](issues/parser-small-fixes.md)
  — 2026-05-14: prelex arg-order swap, base.py duplicate
  `hasParser`/`getParser`, and grammarian.py hardcoded paths all
  fixed. Remaining: `grammar.py:11` raw-string `SyntaxWarning` —
  deferred until `fix-parser-gaps.sh` exits, since it's on the live
  path.

- [ ] [Lowerer diagnostic strategy: emit-gap vs unmodeled-rule](issues/lowerer-emit-gap-strategy.md)
  — Decided 2026-05-14: A' (stub unmodeled leaf rules; new
  `TransformerGap` kind distinct from `EmitterGap`; diagnose has
  three buckets; `fix-parser-gaps.sh` learns the third kind).
  Gated on `ast-coverage-gaps.md` landing first.

- [ ] [Experiment: combfind retrieval inside playbook mode](issues/combfind-in-playbook.md)
  — Parked 2026-05-14. Port `find_analog` + reindex + v6 honest-grep
  recorder fix from `/private/tmp/combfind-argentum-press-worktree`
  (branch `combfind-integration`, tip `259a2c2`), wire combfind into
  playbook's exemplar gathering behind `--playbook-retriever=combfind`,
  re-A/B the four v6 gaps under `--mode playbook`. Hypothesis:
  semantically-ranked exemplars cut LLM turns and agent-side greps
  inside the DAG too.
