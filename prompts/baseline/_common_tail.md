
FILES YOU MAY EDIT
  src/argentum_press/parser/transformer.py   (~1900 lines)
  src/argentum_press/parser/ast/*.py         (frozen-dataclass AST nodes)
  src/argentum_press/parser/grammar/grammar.py (~940 lines; only for parse-error)
  src/argentum_press/lowerer.py              (AST -> Kotlin DSL)

DISCIPLINE
  - All needed signal is above. Don't re-run diagnose; the orchestrator runs
    it again before the next iteration.
  - Don't run pytest more than once unless you've made a follow-up edit.
  - Don't commit; the orchestrator owns commits.
  - For lowerer.py / transformer.py: grep before Read - they're 1k+ lines.
  - The minimum edit to move this gap is the goal. No refactors, no
    drive-by cleanup, no unrelated rule changes.

WORKFLOW
  1. Make the minimum edit.
  2. Run pytest:
     uv run pytest tests/test_diagnose.py tests/test_pipeline.py \
       tests/test_lowerer.py tests/test_classify.py -x -q -n auto
  3. If pytest red, fix and re-run.
