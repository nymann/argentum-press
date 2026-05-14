Fix one lowerer gap in argentum-press.

CARD
  name: {{card_name}}
  oracle text:
{{oracle_text_indented_4}}

GAP  kind=lower  label={{label}}
  An AST node parsed cleanly but lowerer.py has no @register handler
  for it. Add one handler. Do NOT change the AST or transformer.

GAP AST CLASS DEFINITION (the fields the new handler will receive)
{{gap_class_def}}

PARSED AST FOR THIS CARD (where the gap node sits in the tree)
{{ast_block_indented_2}}

HANDLER MAP (every @<dispatcher>.register line in lowerer.py).
Pick a handler whose AST class is structurally similar to the GAP
AST CLASS above and mirror its body.
{{handler_map_indented_2}}

ENGINE DSL HINTS (Kotlin DSL surface in argentum-engine that already
exists for this kind of effect). Mirror existing DSL - do NOT invent.
{{engine_hints_indented_2}}

FILE SIZES
{{file_sizes}}

RECENT COMMITS TOUCHING lowerer.py
{{recent_commits_indented_2}}
{{common_tail}}
