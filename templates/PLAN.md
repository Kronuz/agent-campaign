# {campaign}: plan

The patron writes Goals, Non-goals and Reserved decisions; the game master keeps the rest
current. Quest state is in `state.json` (`campaign status`, the quest log); this file holds what
the state cannot.

## Goals

- <what success means, measurably>

## Non-goals

- <what this campaign will not do>

## Constraints

- <platforms, compatibility, licensing, hardware, deadlines>

## Reserved decisions

The patron decides these; everything else the game master decides and records below.

- Goals, non-goals and significant scope changes.
- Removing or narrowing user-visible behavior.
- Public APIs and formats the patron reserves: <list>.
- Commitments to other teams, new external services or repositories, anything that costs money.

## Documents

What the product is goes to `docs/` of the repository it describes, never to `.campaign/`: designs
(`docs/design/`), research (`docs/research/`), measured results (`docs/perf/`), and a how-it-works
note per landed mechanism (`docs/mechanisms/`, from the skill's `templates/mechanism.md`). The
game master updates the book outline at each milestone: chapters by component, each mapped to its
notes, designs and commits, and the mechanisms still missing a note.

| Repository | Its `docs/` holds |
|---|---|
| <home> | <this workspace: machines, setup, runbooks> |
| <project> | <the project's designs and notes; the book outline, docs/book/OUTLINE.md> |
| <library> | <the library's own designs and notes> |

## Milestones

| Milestone | Quests | Validation (a check that runs on `main`) |
|---|---|---|
| <M1> | <ids> | <command> |

## Decisions

| ID | Date | Decision | By | Why |
|---|---|---|---|---|
