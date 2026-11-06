#!/usr/bin/env bash
# Links the campaign skill, its tools and its agent types into $HOME. Safe to re-run; it never
# replaces a file it did not link.
#   install.sh          link everything, generate the Copilot and Codex agent files
#   install.sh --check  exit 1 when a link is missing or points elsewhere
set -euo pipefail
repo=$(cd "$(dirname "$0")" && pwd)
check=; [ "${1:-}" = --check ] && check=1
bad=0

link() {  # target, path
	if [ "$(readlink "$2" 2> /dev/null)" = "$1" ]; then
		return 0
	elif [ -n "$check" ]; then
		echo "missing: $2 -> $1"; bad=1
	elif [ -e "$2" ] && [ ! -L "$2" ]; then
		echo "skipped $2: exists and is not a link" >&2; bad=1
	else
		mkdir -p "$(dirname "$2")" && ln -sfn "$1" "$2" && echo "linked $2"
	fi
}

link "$repo" "$HOME/.agents/skills/campaign"
if [ -d "$HOME/.claude" ]; then
	# one link when ~/.claude/skills is ~/.agents/skills
	[ "$(cd "$HOME/.claude/skills" 2> /dev/null && pwd -P)" = "$(cd "$HOME/.agents/skills" && pwd -P)" ] ||
		link "$repo" "$HOME/.claude/skills/campaign"
	for agent in "$repo"/agents/*.md; do link "$agent" "$HOME/.claude/agents/$(basename "$agent")"; done
fi
for tool in campaign:campaign.py quiet:quiet.py pace:pace.py report:report.py; do
	link "$repo/scripts/${tool#*:}" "$HOME/.local/bin/${tool%%:*}"
done
for old in "$HOME"/.local/bin/*; do  # a tool this repository renamed or removed leaves a dangling link
	[ -L "$old" ] && [ ! -e "$old" ] || continue
	case $(readlink "$old") in "$repo"/*) ;; *) continue ;; esac
	if [ -n "$check" ]; then echo "stale: $old"; bad=1; else rm "$old" && echo "removed $old"; fi
done
if [ -n "$check" ]; then
	"$repo/scripts/sync-agents.py" --check || bad=1
else
	"$repo/scripts/sync-agents.py"
fi
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "add ~/.local/bin to PATH" >&2 ;; esac
exit $bad
