Fix one transformer gap in argentum-press.

CARD
  name: {{card_name}}
  oracle text (raw):
{{oracle_text_indented_4}}

GAP  kind=parse  label={{label}}
  Lark parsed the text fine, but the transformer has no method for
  the named rule (raised via __default__ -> LoweringIncomplete).
  Add a transformer method; if a new AST dataclass is needed, add it
  to parser/ast/<file>.py and mirror its frozen/slots neighbors.

GRAMMAR RULE DEFINITION (for the failing rule)
{{rule_def_indented_2}}

WHERE THIS RULE IS USED in grammar.py (parent rules - their
transformer methods are the natural analogs to mirror)
{{rule_uses_indented_2}}

GRAMMAR RULE INDEX (rules near the target; rule name -> line)
{{grammar_index_excerpt}}

FILE SIZES
{{file_sizes}}

RECENT COMMITS TOUCHING transformer.py
{{recent_commits_indented_2}}
{{common_tail}}
