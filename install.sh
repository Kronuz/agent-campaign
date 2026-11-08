#!/usr/bin/env bash
# Links the campaign skill, its tools and its agent types into $HOME, and generates the Copilot and
# Codex agent files. It replaces only links into this or another campaign checkout, and dangling
# ones; anything else in the way is reported and left alone.
#   install.sh              link everything
#   install.sh --dry-run    print what it would change, change nothing
#   install.sh --check      exit 1 when a link is missing or points elsewhere
#   install.sh --uninstall  remove what it linked and generated, nothing else
set -euo pipefail
repo=$(cd "$(dirname "$0")" && pwd)
mode=${1:-install}
case $mode in install | --dry-run | --check | --uninstall) ;; *) echo "usage: install.sh [--dry-run|--check|--uninstall]" >&2; exit 2 ;; esac
bad=0

act() {  # a change, or what it would be
	if [ "$mode" = --dry-run ]; then echo "would: $*"; else "$@"; fi
}

campaign_checkout() {  # whether a link target lies in a campaign checkout (this skill, another copy)
	local dir
	for dir in "$(dirname "$1")" "$(dirname "$(dirname "$1")")" "$1"; do
		[ -f "$dir/SKILL.md" ] && grep -qx 'name: campaign' "$dir/SKILL.md" && return 0
	done
	return 1
}

link() {  # target, path
	local now
	now=$(readlink "$2" 2> /dev/null || true)
	if [ "$mode" = --uninstall ]; then
		[ "$now" = "$1" ] && act rm "$2" && echo "removed $2"
		return 0
	elif [ "$now" = "$1" ]; then
		return 0
	elif [ "$mode" = --check ]; then
		echo "missing: $2 -> $1"; bad=1
	elif [ -e "$2" ] && [ ! -L "$2" ]; then
		echo "skipped $2: exists and is not a link" >&2; bad=1
	elif [ -L "$2" ] && [ -e "$2" ] && ! campaign_checkout "$now"; then
		echo "skipped $2: links to $now, not to a campaign checkout" >&2; bad=1
	else
		act mkdir -p "$(dirname "$2")" && act ln -sfn "$1" "$2" && echo "linked $2${now:+ (was $now)}"
	fi
}

link "$repo" "$HOME/.agents/skills/campaign"
if [ -d "$HOME/.claude" ]; then
	# one link when ~/.claude/skills is ~/.agents/skills
	[ "$(cd "$HOME/.claude/skills" 2> /dev/null && pwd -P)" = "$(cd "$HOME/.agents/skills" 2> /dev/null && pwd -P)" ] ||
		link "$repo" "$HOME/.claude/skills/campaign"
	for agent in "$repo"/agents/*.md; do link "$agent" "$HOME/.claude/agents/$(basename "$agent")"; done
fi
for tool in campaign:campaign.py quiet:quiet.py pace:pace.py report:report.py; do
	link "$repo/scripts/${tool#*:}" "$HOME/.local/bin/${tool%%:*}"
done
for old in "$HOME"/.local/bin/* "$HOME"/.claude/agents/*; do  # what this repository renamed or removed
	[ -L "$old" ] && [ ! -e "$old" ] || continue
	case $(readlink "$old") in "$repo"/*) ;; *) continue ;; esac
	if [ "$mode" = --check ]; then echo "stale: $old"; bad=1; else act rm "$old" && echo "removed $old"; fi
done
case $mode in
	--check) "$repo/scripts/sync-agents.py" --check || bad=1 ;;
	--uninstall) act "$repo/scripts/sync-agents.py" --remove ;;
	*) act "$repo/scripts/sync-agents.py" ;;
esac
[ "$mode" = --uninstall ] && exit 0
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "add ~/.local/bin to PATH" >&2 ;; esac
exit $bad
