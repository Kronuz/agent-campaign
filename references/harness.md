# Harnesses

The methodology, `campaign` and the state are the same everywhere; only these operations differ.
Capabilities checked against vendor docs on 2026-11-05; "unverified" means documented but not
exercised on this machine.

| Operation | Claude Code | Copilot CLI | Codex |
|---|---|---|---|
| Agent types | `~/.claude/agents/campaign-{contributor,reviewer}.md`: model and effort pinned | `~/.copilot/agents/*.agent.md`, generated; a file pins the model, not the effort | `~/.codex/agents/*.toml`: generated fields match the documented schema; symlink verified 2026-11-05. Named loading awaits a fresh session |
| Launch with a model and effort | Agent tool: `subagent_type`, `model`, `effort` | `task`: `agent_type`, `model` (`claude-opus-5.5`, `claude-sonnet-5.5`), `reasoning_effort` | `collaboration.spawn_agent` with `fork_turns: "none"`, `model`, `reasoning_effort`; task name `<role>_<quest>[_<assignment or lens>]` (`-` as `_`), the only plaintext usage reads |
| A reviewer from another model family | not built in; the owner may run one in another harness | `task` with a GPT-class `model` (`gpt-6.1-sol`) | Codex supplies a different family for Claude-built work; available overrides here are GPT models (verified 2026-11-05) |
| Run in the background | Agent tool background mode; completion notifies the lead | `mode: background`; completion notifies the lead | `spawn_agent` returns immediately; final result arrives in the parent's mailbox (verified 2026-11-05) |
| Resume an agent | SendMessage | `write_agent`, then `read_agent` once after the notice; only background agents take messages: launch contributors in the background, else a resume is a fresh contributor with the Resume prompt | `collaboration.followup_task` restarts an idle agent with retained context; reply received without rereading files (verified 2026-11-05) |
| Lead waits without tokens | `campaign wait --commits` as a background Bash command; it is re-invoked when that exits or an agent finishes | the same in an async shell; both notify it | A finishing agent does not wake it (2026-11-06). Agents out: `wait_agent`, which returns on any mailbox update (every agent's end); only jobs: `campaign wait --commits` in `exec_command`, then keep awaiting that session with `write_stdin` (empty input, a long yield) until it exits; the model samples only between those calls (2026-11-05). Never start a second wait |
| Subagent waits for a job | one `campaign wait --timeout 600`, then ends its turn (a subagent that ends its turn is finished; nothing wakes it) | the same (a background command's notice does not wake a subagent) | the same |
| Periodic report | a scheduled job, recreated each session | `manage_schedule` with a cron, recreated each session | This session exposes no scheduler; use campaign state/sidebar and report at turn boundaries (verified 2026-11-05) |
| Usage (`campaign usage`) | `~/.claude/projects/*/<session>/subagents/agent-*.jsonl` (lead: `<session>.jsonl`): per agent, the launch prompt, model, effort, tokens (each response once, by message id), time; cost unavailable (verified 2026-11-09) | `~/.copilot/session-state/*/events.jsonl`: per agent, the `task` prompt, model, effort, cost, tokens, time (verified 2026-11-05) | Rollout logs: model/effort, tokens/time and lead totals; runs attributed by task name, cost unavailable (2026-11-06) |
| Quota (`pace`) | the snapshot a status line writes to `~/.scratch/quota/claude.json` | session events' `quotaSnapshots` (verified 2026-11-05) | rollout `rate_limits` (verified 2026-11-05) |
| Skills location | `~/.claude/skills` (linked to `~/.agents/skills`) | `~/.agents/skills` | `~/.agents/skills`, symlink and loaded skill verified 2026-11-05 |

## Harness-specific mechanisms

Everything the campaign relies on that one harness provides and the methodology does not: hooks,
status lines, schedulers, session names, message delivery. Each row states the need in
harness-neutral words, so an agent on another harness can build the equivalent from it. A change
to the skill that starts relying on a harness feature adds its row here in the same commit.

| Need | Claude Code (how, where it lives) | Copilot CLI | Codex |
|---|---|---|---|
| A peer's message reaches the lead between tool calls, not only when it stops | PostToolUse hook `~/.claude/hooks/cmux-inbox-drain.sh` (dotfiles:98a72d4): drains the surface's cmux inbox as `additionalContext`, skipped in subagents (`agent_id` set) | unbuilt: `cmux hooks copilot install` exists | unbuilt: `cmux hooks codex install` exists |
| A peer's message reaches a lead that ended its turn | cmux's Claude wrapper injects a Stop hook that blocks the stop with the message | unbuilt (cmux hooks, as above) | unbuilt (cmux hooks, as above) |
| Messages name their sender's campaign | the cmux workspace title is the sender name (`cmux workspace-action --action rename --title '<campaign> · lead'`); a tab rename is not (verified 2026-11-21) | the same through cmux | the same through cmux |
| The owner sees the campaign in the session's own name (desktop, web, mobile list) | set at launch only: `claude -n '<campaign> · lead'` (`--remote-control '<name>'` for a Remote Control session), or by the owner (`/rename`, or renaming it in the Claude app); an explicit name is never replaced by the generated title. No hook field, setting, environment variable or agent command sets it (code.claude.com/docs/en/sessions.md, remote-control.md, hooks.md; checked 2026-11-21); a hook's `terminalSequence` sets only the terminal title | unknown | unknown |
| Quota for `pace` | status line `~/.claude/statusline.sh` (dotfiles) writes `~/.scratch/quota/claude.json` on each render | session events' `quotaSnapshots` | rollout `rate_limits` |
| Sidebar progress (`report`) | cmux status pills and bar; a terminal bar elsewhere | the same | the same (verified 2026-11-05) |
| Periodic report to the owner | a session-only cron job (CronCreate), recreated each session, expiring after 7 days | `manage_schedule` | none: report at turn boundaries |
| Lead woken when a job or agent ends | background Bash command; agent completion notices | async shell notices | see "Lead waits without tokens" above |
| Leads of different campaigns message each other directly | Claude Code cross-session messages: SendMessage to a peer session's `uds:/tmp/cc-socks/<pid>.sock` (ListAgents names them); they arrive as a `cross-session-message`. Beside `campaign message`, whose record is the durable one | unknown | unknown |
| A background contributor reports and is resumed | the subagent's final report arrives through SubagentHandback; an agent that stops with its own background command still running sends an interim "waiting on its own background work" notice and notifies again when it ends; SendMessage resumes it with its context | see "Resume an agent" above | see "Resume an agent" above |
| A file sent to the owner's other devices | SendUserFile | unknown | unknown |

"unbuilt" means the harness has the hook point and the campaign has not used it yet; "unknown"
means nobody has checked.

## Agent types

`agents/campaign-contributor.md` and `campaign-reviewer.md` are the source, linked into
`~/.claude/agents`. `scripts/sync-agents.py` generates the Copilot and Codex files from them;
`--check` reports drift. A new or edited type loads when a session starts; until then, use the
**body fallback**: launch a general-purpose agent whose prompt starts with the type's body (the file
without its front matter), with the model and effort as launch arguments. An assignment is not a
type: an exploration contributor is the contributor type with `Assignment: exploration` in its
prompt.

## Model tiers

SKILL.md names tiers; these are the models they meant when last checked. Pass the model and effort
at every launch: the agent files default to the strong tier at high effort.

| Tier | Claude Code | Copilot CLI | Codex |
|---|---|---|---|
| frontier (targeted hard or sensitive work only) | Fable class | none: use strong | none: use strong |
| strong | Opus class | `claude-opus-5.5` | Astra |
| economical | Sonnet class | `claude-sonnet-5.5` | Sol |
| another family, for critical reviews | (another harness) | `gpt-6.1-sol` | (the session's GPT models) |

## Limitations

- No harness lets a subagent sleep on a long command cheaply: a waiting turn rereads its context.
  Hence jobs, a short bounded wait, and resumption by the lead.
- In Claude Code and Copilot CLI, subagents run inside the lead's process and die with it. Only
  jobs outlive the lead, and they are supervised by their runners.
- Copilot agent files cannot pin effort: pass `reasoning_effort` on every launch. The skill's model
  tiers are the owner's standing instruction, so naming the model at launch is intended.
- Experimental agent teams (Claude Code) and `/fleet` (Copilot) suit short parallel work inside one
  session; their members are not restored on resume, so quests never depend on them.
- Codex CLI 0.166.0 was exercised on macOS on 2026-11-05. Named type discovery and automatic file
  overrides require a fresh session and remain unexercised. This session has no named-type field
  in `spawn_agent`; use the body fallback rather than claiming the TOML was loaded.

## Codex mapping and runtime checks

Measured 2026-11-05 on a real campaign: of 10.34M cumulative tokens in ten minutes, 51.5% were the
lead's; three Astra/high contributors used 5.01M. Quota weights are unknown. These are operating
hypotheses pending comparable completed work, not model rankings.

| Assignment or role | Codex starting point |
|---|---|
| Implementation, exploration, maintenance, documentation | Sol medium |
| Design, performance, reviewer | Sol high; Astra high for a concrete unresolved mechanism or independent ruling |
| Research, design freeze | Astra high; max only for a bounded question that warrants it |
| Lead | Sol medium, small context; measured separately from contributors |

A generated Codex type has no `model`, so it runs on the session's; its `model_reasoning_effort`
overrides the effort given at launch. An assignment that needs another effort than its type's file
(exploration at medium on the contributor file, which says high) therefore uses the body fallback.
Sol/medium and Astra/high launches work; named loading remains unexercised. `sync-agents` keeps
`max` in the generated files (`tests/test_agents.py`); never lower a requested `max`. Cost zero
means unavailable, never free.

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
