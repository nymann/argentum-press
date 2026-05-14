"""Lower-gap playbook: structured DAG replacement for the freeform fix-loop.

The single freeform ``claude -p`` invocation in ``scripts/fix_parser_gaps.py``
is replaced (for ``kind="lower"`` gaps) by a fixed sequence of orchestrator
steps interleaved with a small number of tool-use-constrained LLM calls.
See ``experiments/playbook-design.html`` for the full DAG and rationale.

Public surface:

* :func:`argentum_press.playbook.lower.run` — end-to-end driver.
* :mod:`argentum_press.playbook.context` — L0/L1/L2 context gathering.
* :mod:`argentum_press.playbook.llm` — Anthropic SDK wrapper.
* :mod:`argentum_press.playbook.edits` — libcst edit application.
* :mod:`argentum_press.playbook.cache` — L3 disk cache.
* :mod:`argentum_press.playbook.heuristics` — L5a pattern selection.
"""
