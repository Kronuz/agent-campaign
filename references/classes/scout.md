# Scout

A Scout takes one target of a scouting party, or one quick, targeted question.

- One target, one question; confirm it still exists on current `main` before anything else.
- At most three experiments; time things only as strict or exclusive jobs.
- A scout asked a question, with no fix expected, needs no quest: it runs jobs at `main`'s
  commit (`campaign job submit --commit main ...`), reads what it needs, and reports. A fix worth
  making becomes a quest, which the same scout may finish while its context is small.
- The prompt is in `references/prompts.md`, "Scout".
