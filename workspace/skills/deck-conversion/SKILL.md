---
name: deck-conversion
description: Use when asked to convert a deck or presentation to a slide template.
tools: [convert_presentation]
---
# Converting a deck to a template

1. Call convert_presentation with the template the user names. Templates are in workspace/templates/.
2. The team uses light section dividers: always pass layouts {"section": "light"}.
3. In your answer, give the path of the converted deck and name any slide that has items in not_carried_over.
