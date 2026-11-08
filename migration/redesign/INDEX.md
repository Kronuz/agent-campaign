# The redesign record

The redesign of the `campaign` skill, November 2026. The skill is usable without these files;
they explain why it is the way it is.

The record was written while 2.0 was built inside my private dotfiles, so its paths and commits
point there: `home/.agents/skills/campaign/` is this repository's root, `home/.claude/agents/` is
`agents/`, `docs/campaign-2/` is this directory, and a `dotfiles:` commit is in that private
history.

| Deliverable | File |
|---|---|
| A. Audit of campaign 1.x, with the records of four projects | [audit.md](audit.md) |
| B. External research: what was adopted, adapted, rejected | [research.md](research.md) |
| C. Architecture, with the vocabulary map (package = quest, lead = game master, ...) | [architecture.md](architecture.md) |
| The one independent architecture challenge and its dispositions | [challenge.md](challenge.md) |
| D. Implementation | [the repository root](../..) |
| E. Migration guide | [../checklist.md](../checklist.md) |
| F. Validation results | [validation.md](validation.md) |
| G. Final assessment | [assessment.md](assessment.md) |
| The first real campaign on 2.0, run in Codex | [codex-trial.md](codex-trial.md) |

These records call the skill "2.0" and its predecessor "1.x", and those dated before 2026-11-08 use
the earlier themed names: patron (owner), game master (lead), adventurer (contributor), paladin
(reviewer), class (assignment), lore (lessons). The preflight review that renamed them is
[../preflight.md](../preflight.md).
