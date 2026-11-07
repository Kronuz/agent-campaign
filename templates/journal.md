# {id}: journal

State: working
Quest: {brief} (the game master's; never edit it)

The adventurer keeps this journal current and commits it with the work, so a fresh adventurer can
pick the quest up. `campaign status` reads State, the first step under Next, and the Evidence
table, one row per criterion with its job id or test: that table is the quest's progress, and
landing needs every criterion in it. A row naming a job counts while that job, or the same job
rerun (`campaign job rerun`), passed at the quest's current code. Name each job you examined by its
id, failures included; status lists the failed jobs no journal names yet. Answer each message
`campaign brief` lists by naming its id (msg-N) with what you did. State is one of: working,
waiting on jobs <ids>, ready for review, fixing <review>, blocked on <who>: <what> (say which
criteria are done and which wait).

## Doing

<The current step, precisely enough for a fresh adventurer to continue it.>

## Next

1. <step>

## Blocked on

<What, and who decides. "Nothing" when nothing.>

## Evidence

| Criterion | Evidence (job id, test, commit) |
|---|---|
| <A1> | <job id at the current code, or a test that fails without the change> |

## Rulings I made

<Decisions taken inside the quest's scope, so the paladin can check them.>
