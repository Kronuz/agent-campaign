# {campaign}: plan

The owner writes Goals, Non-goals and Reserved decisions; the lead keeps the rest current. Quest
state is in `state.json` (`campaign status`, the quest log); this file holds what the state cannot.

## Goals

- <what success means, measurably>

## Non-goals

- <what this campaign will not do>

## Constraints

- <platforms, compatibility, licensing, hardware, deadlines>

## Reserved decisions

The owner decides these; everything else the lead decides and records below.

- Goals, non-goals and significant scope changes.
- Removing or narrowing user-visible behavior.
- Public APIs and formats the owner reserves: <list>.
- Commitments to other teams, new external services or repositories, anything that costs money.

## Documents

What the product is goes to `docs/` of the repository it describes, never to `.campaign/`: designs
(`docs/design/`), research (`docs/research/`), measured results (`docs/perf/`). Notes about running
the campaign (handoffs, lead prompts) go to `.campaign/<name>/`: a file anywhere else is code to
`land`, so every quest in flight would have to rebase over it.

## Milestones

| Milestone | Quests | Validation (a check that runs on `main`) |
|---|---|---|
| <M1> | <ids> | <command> |

## Decisions

A `[decision]` criterion's evidence names a row's ID here.

| ID | Date | Decision | By | Why |
|---|---|---|---|---|
