# Harnesses

The methodology, `campaign` and the state are the same everywhere; only these operations differ.
Capabilities checked against vendor docs on 2026-11-05; "unverified" means documented but not
exercised on this machine.

| Operation | Claude Code | Copilot CLI | Codex |
|---|---|---|---|
| Agent types | `~/.claude/agents/{adventurer,paladin}.md`: model and effort pinned | `~/.copilot/agents/*.agent.md`, generated; a file pins the model, not the effort | `~/.codex/agents/*.toml`: generated fields match the documented schema; symlink verified 2026-11-05. Named loading awaits a fresh session |
| Launch with a model and effort | Agent tool: `subagent_type`, `model`, `effort` | `task`: `agent_type`, `model` (`claude-opus-5.5`, `claude-sonnet-5.5`), `reasoning_effort` | `collaboration.spawn_agent` with `fork_turns: "none"`, `model`, `reasoning_effort`; accepted 2026-11-05. This session uses the type body in a general prompt |
| A paladin from another model family | not built in; the patron may run one in another harness | `task` with a GPT-class `model` (`gpt-6.1-sol`) | Codex supplies a different family for Claude-built work; available overrides here are GPT models (verified 2026-11-05) |
| Run in the background | Agent tool background mode; completion notifies the game master | `mode: background`; completion notifies the game master | `spawn_agent` returns immediately; final result arrives in the parent's mailbox (verified 2026-11-05) |
| Resume an agent | SendMessage | `write_agent`, then `read_agent` once after the notice; only background agents take messages, so without background mode launch a fresh adventurer with the Resume prompt | `collaboration.followup_task` restarts an idle agent with retained context; reply received without rereading files (verified 2026-11-05) |
| Game master waits for jobs without tokens | `campaign wait ...` as a background Bash command; the game master is re-invoked when it exits | `campaign wait ...` in an async shell; completion notifies the game master | Run `campaign wait` through `exec_command`; retain its session id and await `write_stdin`, without model sampling during the wait. Verified 2026-11-05: bounded wait returned exit 3 after 50 seconds while the job continued |
| Subagent waits for a job | one `campaign wait --timeout 600`, then ends its turn (a subagent that ends its turn is finished; nothing wakes it) | the same (a background command's notice does not wake a subagent) | the same |
| Periodic report | a scheduled job, recreated each session | `manage_schedule` with a cron, recreated each session | This session exposes no scheduler; use campaign state/sidebar and report at turn boundaries (verified 2026-11-05) |
| Usage (`campaign usage`) | not read yet | `~/.copilot/session-state/*/events.jsonl`: per agent, the `task` prompt, model, effort, cost, tokens, time (verified 2026-11-05) | not read yet |
| Skills location | `~/.claude/skills` (linked to `~/.agents/skills`) | `~/.agents/skills` | `~/.agents/skills`, symlink and loaded skill verified 2026-11-05 |

## Agent types

`agents/adventurer.md` and `paladin.md` are the source, linked into `~/.claude/agents`. `scripts/sync-agents.sh`
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

Verified 2026-11-05 against this session's tool schema and local model catalog. The strongest
(Opus) role maps to `gpt-6-astra`; Sonnet Scout/Squire work maps to `gpt-6.1-sol` at medium.
Use medium to high for ordinary implementation, high for Sage/Artificer/Bard and paladins,
and max for Wizard or a design about to freeze. These are role assignments, not a comparative
benchmark. Sol at medium and Astra at high both launched (2026-11-05,
`docs/campaign-2/codex-trial.md`). The generated files intentionally omit `model`, inheriting the session model. A named file's
model or effort overrides launch arguments, so use the body fallback for a class-specific effort
that differs from the file. Preserve `max` in generation; do not silently lower it.

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
