#!/usr/bin/env bash
# Shows pipeline progress, status pills and notifications in the cmux sidebar when running inside
# cmux. Elsewhere progress goes to the terminal (OSC 9;4 bar when stderr is a terminal, else a
# text line on stderr), notifications to the desktop, and status pills nowhere.
# `report --help` is the manual.
set -uo pipefail

in_cmux() { [ -n "${CMUX_WORKSPACE_ID:-}" ] && command -v cmux > /dev/null; }
fraction() {  # N/M, a percentage (60%) or a fraction (0.6) -> a fraction 0..1
	case "$1" in
	*/*) awk -v n="${1%/*}" -v m="${1#*/}" 'BEGIN { printf "%.4f", (m > 0 ? n / m : 0) }' ;;
	*%) awk -v p="${1%\%}" 'BEGIN { printf "%.4f", p / 100 }' ;;
	*) awk -v f="$1" 'BEGIN { printf "%.4f", f }' ;;
	esac
}
osc() { [ -t 2 ] && printf '\033]9;4;%s\033\\' "$1" >&2; }  # Ghostty, WezTerm, Windows Terminal, iTerm2

usage() {
	cat <<'USAGE'
report: report long-running work where the owner will see it.

Usage:
  report set N/M|PCT%|FRACTION [LABEL]   show a progress bar (e.g. 3/5 "phase 2 gates")
  report clear                           remove the progress bar
  report status KEY VALUE [ICON]         set a sidebar pill (cmux only; e.g. icon hammer)
  report unstatus KEY                    remove a sidebar pill
  report notify TITLE [BODY]             send a notification

Inside cmux ($CMUX_WORKSPACE_ID set) everything goes to the workspace's sidebar and
notification feed. Elsewhere: set/clear draw an OSC 9;4 terminal progress bar when stderr is a
terminal, else print "progress: 60% phase 2 gates" on stderr; notify uses the desktop
(osascript on macOS, notify-send on Linux) or stderr; status pills are skipped.
Every command with valid arguments exits 0, so a failing display never fails the work it
reports on.
USAGE
}

case "${1:-}" in
set)
	f=$(fraction "${2:?report set: N/M, PCT% or FRACTION}") label=${3:-}
	if in_cmux; then cmux set-progress "$f" ${label:+--label "$label"} > /dev/null 2>&1
	else
		pct=$(awk -v f="$f" 'BEGIN { printf "%d", f * 100 + 0.5 }')
		osc "1;$pct" || printf 'progress: %s%%%s\n' "$pct" "${label:+ $label}" >&2
	fi
	;;
clear) if in_cmux; then cmux clear-progress > /dev/null 2>&1; else osc 0; fi ;;
status) in_cmux && cmux set-status "${2:?KEY}" "${3:?VALUE}" ${4:+--icon "$4"} > /dev/null 2>&1 ;;
unstatus) in_cmux && cmux clear-status "${2:?KEY}" > /dev/null 2>&1 ;;
notify)
	title=${2:?report notify: TITLE} body=${3:-}
	if in_cmux; then cmux notify --title "$title" ${body:+--body "$body"} > /dev/null 2>&1
	elif [ "$(uname)" = Darwin ]; then
		osascript - "$title" "$body" > /dev/null 2>&1 <<-'APPLESCRIPT'
			on run argv
				display notification (item 2 of argv) with title (item 1 of argv)
			end run
		APPLESCRIPT
	elif command -v notify-send > /dev/null; then notify-send "$title" "$body" 2> /dev/null
	else printf 'notify: %s%s\n' "$title" "${body:+: $body}" >&2; fi
	;;
-h | --help | help) usage ;;
*) usage >&2; exit 2 ;;
esac
exit 0
