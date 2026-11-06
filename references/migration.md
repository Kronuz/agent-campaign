# Migrating from campaign 1.x

Campaign 2.0 replaces 1.x's roles, documents and host rules. Old project files stay where they
are, as history; nothing rewrites them.

## What changed

| 1.x | 2.0 |
|---|---|
| 14 agent types (explorer, developer, validator, performance engineer, researcher, designer, maintenance engineer, tech writer, code reviewer, security reviewer, architect, rechecker, proof auditor, quartermaster) | `adventurer` and `paladin`; the assignment and the lens carry the difference |
| Workstream ledgers (`docs/status/<ws>.md`), status index, exploration log, `INDEX.md`, status lines, book outline | `.campaign/<name>/state.json` (through `campaign`), quest files, journals on branches; status is derived by `campaign status` |
| Review tiers with floor, adjust, defaults and a proof audit | Four risk levels; one review per needed lens per quest |
| Rechecker after fixes | The game master reruns the paladin's probes as jobs and runs `campaign accept --jobs` |
| Lead, owner, workstream, contributor, reviewer, explorer, ledger, sources and calibration | Game master, patron, quest, adventurer, paladin, scout, journal, lore: the same jobs, named for the campaign the skill is named after |
| Phase gates, gate reports, gate reviews, periodic system reviews | Milestones in `PLAN.md` with a validation job; system review when `campaign check` reports a conflict |
| `quiet` holds taken by waiting agents | Jobs; strict and exclusive commands now stop at their deadline (exit 124) and holds over 30 minutes are refused |
| `pace advise` targets and the two-agent floor | `pace` reports only |
| `lease` (AWS) | Removed; campaign provisions nothing |
| `docs/reviews/<subject>-<kind>.md`, finding IDs per kind | `.campaign/<name>/reviews/<quest>-<lens>.md`, one findings table |
| Branch prefixes `phase*/`, `design/`, `explore/`, `fix/`, ... | `quest/<id>` and `quest/<id>--<task>` (add `quest/**` to CI push triggers) |

## Install

```sh
git clone https://github.com/Kronuz/agent-campaign.git ~/code/agent-campaign
~/code/agent-campaign/install.sh               # links the skill, the tools and the agent types
rm -f ~/.local/bin/lease                       # 1.x's, if still linked
campaign --help && quiet --help | grep MAX_MIN # both 2.0
```

Agent types load when a session starts: restart running game master sessions after installing. Live 1.x
sessions keep the agent types they were launched with until then.

Install on every session of a machine at once: the hold directory (`~/.scratch/quiet`) is shared,
and a 1.x `quiet` would read 2.0's `<name>.starving` marker (a build waiting 10 minutes) as a
strict hold. Since `~/.local/bin/quiet` is one link, this only matters for a `quiet` copied
elsewhere (a remote host or a VM sharing the directory): update those copies too.

## Try it in one session without installing

Point one session at a clone and leave every other session on 1.x:

```sh
git clone https://github.com/Kronuz/agent-campaign.git ~/code/agent-campaign
mkdir -p ~/.scratch/campaign2-try/bin
ln -sf ~/code/agent-campaign/scripts/campaign.py ~/.scratch/campaign2-try/bin/campaign
ln -sf ~/code/agent-campaign/scripts/quiet.py ~/.scratch/campaign2-try/bin/quiet
```

Start the session with: "Use the campaign skill at ~/code/agent-campaign/SKILL.md; prefix every
shell command with `export PATH=~/.scratch/campaign2-try/bin:$PATH;`. The agent types are not
installed: launch a general-purpose agent with ~/code/agent-campaign/agents/<type>.md prepended."
A 1.x `quiet` in another session reads 2.0's starving-build marker as a strict hold while that
build waits; with no other sessions running, install machine-wide instead.

## Start a new project

```sh
cd <repo>                       # the main checkout of the campaign's home repository
campaign init --name <name>     # writes .campaign/<name>/PLAN.md and state.json, commits them
```

Then follow SKILL.md, "Start a campaign".

## Resume a 1.x project

1. Finish or park what is running: let agents commit their ledgers; let holds drain (`quiet list`).
2. `campaign init` in the project's home repository.
3. For each live workstream, create a quest and adopt its branch instead of creating one:
   ```sh
   campaign new <id> --title "<ledger title>" --risk <low|normal|high|critical> \
       --files '<globs the branch touches>' --check 'unit=<the fast test command>'
   git branch quest/<id> <old-branch>      # same commits, new name; the old branch stays
   campaign start <id>                   # reuses quest/<id> and adds a worktree and journal
   ```
   Copy the ledger's done criteria into the quest file as A1, A2, ...; copy "Doing", "Next" and "Blocked
   on" into the journal. Link the old ledger and reviews from the quest file.
4. A review the 1.x branch already passed counts: copy its verdict into
   `.campaign/<name>/reviews/<id>-<lens>.md` (`Verdict:` and `Commit:` lines, a link to the original)
   and `campaign accept` it. A review not yet run follows the 2.0 risk table.
5. Landed workstreams need no quest. Queued ones become `campaign new` quests.
6. `campaign check` should report no errors; `campaign status` is the new status index. The old
   `docs/status/README.md` can get a one-line pointer to `campaign status`.

## Roll back

Reinstall 1.x as it was installed (its installer restores its own links, `lease` included), remove
the `campaign` link and the `adventurer` and `paladin` agent types by hand, and restart sessions.
Projects keep both sets of files; `.campaign/` is ignored by 1.x.
