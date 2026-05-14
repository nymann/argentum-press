Fix one grammar gap in argentum-press.

CARD
  name: {{card_name}}
  oracle text (raw):
{{oracle_text_indented_4}}

GAP  kind=parse  label={{label}}
  Lark itself rejected the preprocessed text. Either the grammar is
  missing a rule branch, or an existing rule needs a new alternative.

PARSE ERROR DETAIL (extracted from the Lark exception; no need to
re-run the parser)
{{parse_error_block}}

GRAMMAR RULE INDEX (top of grammar.py; rule name -> 1-based line)
{{grammar_index_excerpt}}

FILE SIZES
{{file_sizes}}

RECENT COMMITS TOUCHING grammar.py
{{recent_commits_indented_2}}
{{common_tail}}
