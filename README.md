# agent-campaign

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)

A party of AI agents for long coding projects: quests in worktrees, fresh reviews, checks on the
exact code that lands.

`campaign` is a skill and a small tool for running a multi-day coding project with AI agents in
[Claude Code](https://www.anthropic.com/claude-code),
[GitHub Copilot CLI](https://github.com/features/copilot/cli) or
[Codex](https://github.com/openai/codex). A game master session splits your goals into quests,
adventurer agents each take one quest in their own git worktree, and paladins (fresh agents that
did not write the code) review what is risky. The tool keeps the quest log, runs builds, tests and
benchmarks as jobs on exact commits, and lands only code whose checks passed on that exact code.
Reach for it when a project needs several agents over days; a one-file fix needs none of it.

## Example

The tool on its own, on a repository with one quest. In a campaign, the game master runs these
commands and an adventurer makes the commit.

```sh
campaign init --name wordcount
campaign new fix-split --title "Split on any whitespace" --risk low --files wc.py \
    --check 'unit=python3 test_wc.py'
campaign start fix-split       # a branch and a worktree with the quest's journal
# ... the fix is committed in that worktree ...
campaign verify fix-split      # queues the check as a job at the branch head
campaign wait --quest fix-split
campaign land fix-split
campaign status
```

```text
fix-split: quest/fix-split in ~/code/wordcount-wt/fix-split
unit: 261105-173156-b42243
261105-173156-b42243  done exit 0 in 1.0s  fix-split check unit at 1e4edc15de90
fix-split: landed 1e4edc15de90 on main (merge df838f0e0a76)
fix-split  landed  low  Split on any whitespace
  landed: df838f0e0a76
```

In a session opened in the project, you say:

```text
Use campaign. Turn the goals in .campaign/<campaign>/PLAN.md into quests and start the first ready ones.
```

## Features

- Four roles: you (the patron), a game master, adventurers and paladins, as two agent types. An
  adventurer's class (Sage, Wizard, Artificer, Scout, Bard, Squire) sets its method and model; a
  paladin's lens (correctness, security, durability, design, system) sets its review.
- Review by risk: a low-risk quest lands on green checks, a normal one gets one review, a critical
  one also gets a paladin from another model family.
- Evidence keyed by code: a check result is reused for identical code and toolchain and no longer
  counts once the code changes.
- Landing checks dependencies, the quest's file set, every gate check on the exact code, every
  required review and CI when configured, then fast-forwards `main` or merges only campaign files.
- Jobs instead of waiting agents: one queue per machine, supervised runners with heartbeats, and
  `quiet` holds (light, strict, exclusive) so builds and benchmarks share a machine.
- Direct path first: gate checks block a landing; deferred checks (other platforms, soaks, full
  matrices) run after it at low priority and block the milestone.
- Several campaigns per machine, with cross-campaign warnings, `--after NAME:ID` and a lock per
  shared repository; `campaign usage` reports tokens, cost and time per quest, role and class.

## Install

```sh
git clone https://github.com/Kronuz/agent-campaign.git ~/code/agent-campaign
~/code/agent-campaign/install.sh
```

`install.sh` links the skill into `~/.agents/skills/campaign` (and `~/.claude/skills`), the tools
`campaign`, `quiet`, `pace` and `report` into `~/.local/bin`, and the agent types into
`~/.claude/agents`, and generates the Copilot and Codex agent files for the harnesses installed. It
never replaces a file it did not link; `install.sh --check` reports what is missing. Agent types
load when a session starts.

**Requirements:** Python 3.9 or newer (standard library only), git 2.31 or newer and bash. One of Claude Code, GitHub Copilot CLI or Codex; [cmux](https://cmux.dev) is optional
and shows the campaign in its sidebar.

## Usage

The game master drives the tool; these are the commands it reaches for most.

| Command | What it does |
|---|---|
| `campaign init` | starts a campaign in the home repository: `.campaign/<name>/PLAN.md` and `state.json`; a home may hold several |
| `campaign migrate` | moves an older campaign's `docs/campaign/` to `.campaign/<name>/`, once no quest is in flight |
| `campaign new ID` | plans a quest: risk, file set, gate checks (`--check`), deferred checks (`--later`), dependencies |
| `campaign start ID` | creates `quest/<campaign>/ID` in its own worktree, with a journal |
| `campaign verify ID` | queues the quest's gate checks as jobs at its branch head |
| `campaign accept ID LENS` | records a paladin's verdict for the quest's current content |
| `campaign land ID` | lands the quest when every criterion, task, check and review holds for the exact code |
| `campaign status`, `check` | each quest and what it still needs (what `land` would refuse on), and the contradictions between state, git and jobs |
| `campaign brief [--json]` | the same for agents: one line (or JSON field) per thing to act on; in a quest's worktree, that quest |
| `campaign note NAME:ID "text"` | passes a finding to a quest from anywhere on the machine; it blocks landing until answered |
| `campaign job submit` | runs any command as a job at a commit, at a `quiet` level |
| `campaign usage` | agent runs and job time per quest, role and class |

[`SKILL.md`](SKILL.md) is what agents read; [`references/`](references) holds the prompts, the
review and measurement rules, the host tools and the per-harness notes.

## How it works

State lives in `.campaign/<name>/` of the home repository and only the tool writes it, from the main
checkout, under a lock. Every change is a commit that says what happened (`campaign: start`, `accept`,
`land ... at <commit>`), so `git log -- .campaign/<name>` is the campaign's ledger; each quest's
journal is its builder's, and `PLAN.md`'s Decisions table is the game master's. Agents submit work as jobs and end their turn; one worker per machine runs
the queue in priority order under `quiet`, and the game master waits on results with one background
command. A check result is keyed by the machine, the toolchain, the command and the commit's code
without the campaign's own files, which is what lets `land` prove that the code on `main` is the
code that was tested. [`docs/`](docs) holds the design record of version 2.0: the audit of 1.x,
the research, the architecture and the validation.

## Limitations

- The test suite runs on macOS and Linux; FreeBSD's core pinning is untested.
- `campaign usage` reads Copilot CLI's and Codex's session logs; Claude Code's are not read yet.
- `pace` reads Claude Code's quota from a status line's snapshot and does not see purchased credits.
- Landing is one repository at a time: a change across repositories lands in order, never
  atomically.
- A quest in a repository other than the campaign's home commits its journal there, so the
  journal reaches that repository's history.
- `quiet` and the job queue coordinate one machine; remote hosts run their own.

## Building and testing

```sh
cd tests && python3 run.py          # about three minutes, four tests at a time
ruff check scripts tests && pyrefly check
```

Run the suite inside a `quiet light` hold when other agents share the machine. `run.py -j 1`
runs one test at a time, and `python3 -m unittest` still works.

## License

MIT, see [LICENSE](LICENSE). Ideas from [Superpowers](https://github.com/obra/superpowers),
[Beads](https://github.com/gastownhall/beads), [Gas Town](https://github.com/gastownhall/gastown)
and [Overstory](https://github.com/jayminwest/overstory) are credited in
[`docs/research.md`](docs/research.md). The story behind the design:
[A Smaller Party](https://kronuz.github.io/blog/agent-campaign-redesign/).
