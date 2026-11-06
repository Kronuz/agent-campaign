# {id}: {title}

Risk: {risk}. The game master writes this quest; adventurers read it and never edit it. Its
machine state (status, branch, checks, reviews) is in `state.json`: `campaign status {id}`.

## Outcome

<The behavior that exists when this quest lands, in one paragraph.>

## Acceptance criteria

Each names the evidence that shows it (a test, a check, a job, a review lens).

- A1 <criterion>: <evidence>
- A2 A how-it-works note for each mechanism this adds or changes, in `docs/mechanisms/` of the
  repository it changes (the skill's `templates/mechanism.md`), checked against the code: the note
  and the paladin's reading of it. Delete A2 when the quest adds or changes no mechanism.

## Contracts

<What this quest provides or consumes (APIs, formats, protocols), stated precisely enough that a
quest depending on it can start before it lands.>

## Decisions

<Decisions made, and by whom; open questions, naming the patron or the game master.>

## May land early

<Prefixes of the branch that are safe alone: refactors, tests, unused internal APIs, code behind
a disabled switch. "None" when everything waits for the review.>
