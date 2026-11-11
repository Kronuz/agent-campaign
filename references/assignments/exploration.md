# Exploration

An exploration assignment takes one target of an exploration batch, or one quick question.

- The lead takes targets from the hunt list (`.campaign/<campaign>/hunt.md`: the losing rows of a
  fresh benchmark on `main`, a profile's hotspots, thinly proven code, review follow-ups, small
  bugs, each with its evidence) and sends one economical contributor per target at once, each on
  its own quest with disjoint files (`campaign new hunt-<area> --risk low|normal --files <area>`),
  naming the quest in the row's Status.
- Confirm the target still exists on current `main` before anything else; then at most three
  experiments, one hypothesis each. Time things only as strict or exclusive jobs, so the batch never
  fights over the machine.
- A question, or a target in files an active quest holds, gets no quest: an `explore/` branch whose
  note the lead lands, planning its fix as a quest.
- A verified fix continues as its quest, finished by the same contributor while its context is
  small; a cause without a fix goes to a stronger contributor; an empty hunt is dropped ("nothing
  found"). Two empty hunts in an area send the next batch elsewhere. Prompt: `references/prompts.md`.
