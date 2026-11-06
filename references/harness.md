# Harnesses

The methodology, `campaign` and the state are the same everywhere; only these operations differ.
Capabilities checked against vendor docs on 2026-11-05; "unverified" means documented but not
exercised on this machine.

| Operation | Claude Code | Copilot CLI | Codex |
|---|---|---|---|
| Agent types | `~/.claude/agents/{adventurer,paladin}.md`: model and effort pinned | `~/.copilot/agents/*.agent.md`, generated; a file pins the model, not the effort | `~/.codex/agents/*.toml`: generated fields match the documented schema; symlink verified 2026-11-05. Named loading awaits a fresh session |
| Launch with a model and effort | Agent tool: `subagent_type`, `model`, `effort` | `task`: `agent_type`, `model` (`claude-opus-5.5`, `claude-sonnet-5.5`), `reasoning_effort` | `collaboration.spawn_agent` with `fork_turns: "none"`, `model`, `reasoning_effort`; task name `<role>_<quest>[_<class or lens>]` (`-` as `_`), the only plaintext usage reads |
| A paladin from another model family | not built in; the patron may run one in another harness | `task` with a GPT-class `model` (`gpt-6.1-sol`) | Codex supplies a different family for Claude-built work; available overrides here are GPT models (verified 2026-11-05) |
| Run in the background | Agent tool background mode; completion notifies the game master | `mode: background`; completion notifies the game master | `spawn_agent` returns immediately; final result arrives in the parent's mailbox (verified 2026-11-05) |
| Resume an agent | SendMessage | `write_agent`, then `read_agent` once after the notice; only background agents take messages, so without background mode launch a fresh adventurer with the Resume prompt | `collaboration.followup_task` restarts an idle agent with retained context; reply received without rereading files (verified 2026-11-05) |
| Game master waits without tokens | `campaign wait --commits` as a background Bash command; it is re-invoked when that exits or an agent finishes | the same in an async shell; both notify it | A finishing agent does not wake it (2026-11-06). Agents out: `wait_agent`, which returns on any mailbox update (every agent's end); only jobs: `campaign wait --commits` in `exec_command` |
| Subagent waits for a job | one `campaign wait --timeout 600`, then ends its turn (a subagent that ends its turn is finished; nothing wakes it) | the same (a background command's notice does not wake a subagent) | the same |
| Periodic report | a scheduled job, recreated each session | `manage_schedule` with a cron, recreated each session | This session exposes no scheduler; use campaign state/sidebar and report at turn boundaries (verified 2026-11-05) |
| Usage (`campaign usage`) | not read yet | `~/.copilot/session-state/*/events.jsonl`: per agent, the `task` prompt, model, effort, cost, tokens, time (verified 2026-11-05) | Rollout logs: model/effort, tokens/time and lead totals; runs attributed by task name, cost unavailable (2026-11-06) |
| Quota (`pace`) | the snapshot a status line writes to `~/.scratch/quota/claude.json` | session events' `quotaSnapshots` (verified 2026-11-05) | rollout `rate_limits` (verified 2026-11-05) |
| Skills location | `~/.claude/skills` (linked to `~/.agents/skills`) | `~/.agents/skills` | `~/.agents/skills`, symlink and loaded skill verified 2026-11-05 |

## Agent types

`agents/adventurer.md` and `paladin.md` are the source, linked into `~/.claude/agents`. `scripts/sync-agents.py`
generates the Copilot and Codex files from them; `--check` reports drift. A new or edited type loads
when a session starts; until then, launch a general-purpose agent with the type's body prepended
and its model and effort set.

## Limitations

- No harness lets a subagent sleep on a long command cheaply: a waiting turn rereads its context.
  Hence jobs, a short bounded wait, and resumption by the game master.
- In Claude Code and Copilot CLI, subagents run inside the game master's process and die with it. Only
  jobs outlive the game master, and they are supervised by their runners.
- Copilot agent files cannot pin effort: pass `reasoning_effort` on every launch. The skill's model
  table is the patron's standing instruction, so naming the model at launch is intended.
- Experimental agent teams (Claude Code) and `/fleet` (Copilot) suit short parallel work inside one
  session; their members are not restored on resume, so quests never depend on them.
- Codex CLI 0.166.0 was exercised on macOS on 2026-11-05. Named type discovery and automatic file
  overrides require a fresh session and remain unexercised. This session has no named-type field
  in `spawn_agent`; use the body fallback rather than claiming the TOML was loaded.

## Codex mapping and runtime checks

Measured 2026-11-05: the resumed interval consumed 10.34M cumulative tokens, 51.5% in the game
master; three Astra/high developers consumed 5.01M. Quota weights are unknown. These are operating
hypotheses pending comparable completed work, not model rankings (`docs/codex-trial.md`).

| Class/role | Codex starting point |
|---|---|
| Implementation, Scout, Squire, Bard | Sol medium; one implementation trial before parallel expansion |
| Sage, Artificer, paladin | Sol high; Astra high for a concrete unresolved mechanism or independent ruling |
| Wizard, design freeze | Astra high; max only for a bounded question that warrants it |
| Game master | Sol medium, small context; measured separately from developers |

Generated types inherit the model; file effort overrides launch settings. Use the body fallback
for a different class effort. Sol/medium and Astra/high launches work; named loading remains
unexercised. Preserve max when requested. Cost zero means unavailable, never free.

Verified 2026-11-05: global guidance is `$CODEX_HOME/AGENTS.override.md`, otherwise `AGENTS.md`
(default `~/.codex`). The terminal exposes
`CMUX_WORKSPACE_ID`, and `report set` and `report status` work; with both cmux ids unset,
`report status` exits 0 and prints nothing. Agent completion and `followup_task` resumption both
work; `send_message` alone does not start an idle turn. Overrides need a fresh-context spawn
(`fork_turns: "none"`).

Sources: [custom agents](https://learn.chatgpt.com/docs/agent-configuration/subagents),
[skills](https://learn.chatgpt.com/docs/build-skills),
[global guidance](https://learn.chatgpt.com/docs/agent-configuration/agents-md). Documentation
confirms locations and fields; it does not substitute for the runtime checks marked above.

A job submitted from a task worktree resolves `--commit` (including `HEAD`) and its default
subdirectory in that worktree; without `--commit` it runs the quest branch, so task evidence
names the task's commit. Check migrated evidence with `campaign job show` before trusting it.
