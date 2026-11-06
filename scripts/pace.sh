#!/usr/bin/env bash
# shellcheck disable=SC2154 # ent, rem, reset, pat, r5 and rw are assigned by eval from jq
# Reports quota use per harness, for decisions; it never sets how many agents run. Each harness's
# status line writes a snapshot: Claude Code's to ~/.scratch/quota/claude.json (5h and weekly
# windows); Copilot's limit is the month's premium requests, read from `gh api
# /copilot_internal/user` (cached QUOTA_PLAN_TTL seconds, 300), else a daily QUOTA_CREDITS_BUDGET.
set -uo pipefail
usage() {
	cat <<'USAGE'
pace: quota use per harness, as information.

Usage:
  pace [HARNESS]      each window: used, the use at its reset at the current average rate,
                      and the time to reset

HARNESS is claude, copilot, codex or all (default: QUOTA_HARNESS, else all).
A snapshot older than QUOTA_STALE seconds (900) is reported as old.
Codex reads local CODEX_HOME/sessions logs (default ~/.codex/sessions).

pace informs decisions (will this batch exhaust the week before the milestone?). Unused
quota is not a reason to start work.
USAGE
}
qdir="$HOME/.scratch/quota"
harness=${QUOTA_HARNESS:-}
case "${1:-}" in claude | copilot | codex | all) harness=$1; shift ;; esac
harness=${harness:-all}
if [ "$harness" = all ]; then  # one line per harness with a snapshot, each paced on its own
	case "${1:-}" in -h | --help | help) usage; exit 0 ;; esac
	found=0
	for h in claude copilot codex; do
		[ "$h" = codex ] || [ -f "$qdir/$h.json" ] || continue
		found=1
		QUOTA_HARNESS=$h bash "$0" "$@" | sed "s/^/$h: /"
	done
	[ $found = 1 ] || echo "no snapshot: a harness's status line writes one (Claude Code's or Copilot CLI's)"
	exit 0
fi
if [ "$harness" = codex ]; then
	case "${1:-}" in
	-h | --help | help) usage; exit 0 ;;
	"") ;;
	*) usage >&2; exit 2 ;;
	esac
	pace_source=${BASH_SOURCE[0]}
	while [ -L "$pace_source" ]; do
		pace_dir=$(cd -P "$(dirname "$pace_source")" && pwd)
		pace_source=$(readlink "$pace_source")
		[[ "$pace_source" = /* ]] || pace_source="$pace_dir/$pace_source"
	done
	exec python3 "$(dirname "$pace_source")/codex_quota.py"
fi
snap="${QUOTA_SNAPSHOT:-$qdir/$harness.json}"
stale=${QUOTA_STALE:-900}
now=$(date +%s)

if [ ! -f "$snap" ]; then
	note="no $harness snapshot ($harness's status line writes $snap)"
	echo "$note"
	exit 0
fi
# Copilot: the monthly plan from GitHub's API when it answers, reported like the weekly window;
# else a daily window from QUOTA_CREDITS_BUDGET.
if jq -e '.harness == "copilot"' "$snap" >/dev/null 2>&1; then
	used=$(jq -r '.credits_today // .ai_used // 0' "$snap") at=$(jq -r '.at // 0' "$snap")
	plan="$qdir/copilot-plan.json"
	if [ ! -s "$plan" ] || [ $((now - $(stat -f %m "$plan" 2>/dev/null || stat -c %Y "$plan"))) -gt "${QUOTA_PLAN_TTL:-300}" ]; then
		gh api /copilot_internal/user 2>/dev/null | jq -c '{at: now | floor, reset: .quota_reset_date,
			p: .quota_snapshots.premium_interactions | {entitlement, remaining, unlimited}}' >"$plan.$$" 2>/dev/null \
			&& [ -s "$plan.$$" ] && mv "$plan.$$" "$plan" || rm -f "$plan.$$"
	fi
	if [ -s "$plan" ] && jq -e '.p.entitlement > 0 and (.p.unlimited | not)' "$plan" >/dev/null 2>&1; then
		eval "$(jq -r '@sh "ent=\(.p.entitlement) rem=\(.p.remaining) reset=\(.reset) pat=\(.at)"' "$plan")"
		resets=$(date -j -f %Y-%m-%d "$reset" +%s 2>/dev/null || date -d "$reset" +%s)
		start=$(date -j -v-1m -f %Y-%m-%d "$reset" +%s 2>/dev/null || date -d "$reset -1 month" +%s)
		pct=$(awk -v e="$ent" -v r="$rem" 'BEGIN { printf "%d", 100 * (e - r) / e }')
		jq -n --argjson at "$pat" --argjson u "$pct" --argjson r "$resets" \
			'{at: $at, rate_limits: {seven_day: {used_percentage: $u, resets_at: $r}}}' >"$qdir/.copilot-window.json"
		snap="$qdir/.copilot-window.json" QUOTA_WINDOW_WEEK=$((resets - start)) QUOTA_LABEL_WEEK=month
		QUOTA_NOTE="; $((ent - rem)) of $ent premium requests used"
	else
		budget=${QUOTA_CREDITS_BUDGET:-}
		if [ -z "$budget" ]; then
			echo "copilot: ${used} credits today; no plan from gh api and no QUOTA_CREDITS_BUDGET"; exit 0
		fi
		midnight=$(date -j -v+1d -v0H -v0M -v0S +%s 2>/dev/null || date -d 'tomorrow 00:00' +%s)
		pct=$(awk -v u="$used" -v b="$budget" 'BEGIN { printf "%d", 100 * u / b }')
		jq -n --argjson at "$at" --argjson u "$pct" --argjson r "$midnight" \
			'{at: $at, rate_limits: {five_hour: {used_percentage: $u, resets_at: $r}}}' >"$qdir/.copilot-window.json"
		snap="$qdir/.copilot-window.json" QUOTA_WINDOW_5H=86400 QUOTA_LABEL_5H=day
	fi
fi
eval "$(jq -r '
	@sh "at=\(.at // 0)",
	@sh "u5=\(.rate_limits.five_hour.used_percentage // "")",
	@sh "r5=\(.rate_limits.five_hour.resets_at // "")",
	@sh "uw=\(.rate_limits.seven_day.used_percentage // "")",
	@sh "rw=\(.rate_limits.seven_day.resets_at // "")",
	@sh "us=\(.rate_limits.spend_limit.used_percentage // "")"
' "$snap")"

hm() { local s=$1; [ "$s" -lt 0 ] && s=0; printf '%dh%02dm' $((s / 3600)) $((s % 3600 / 60)); }
# window USED RESET LENGTH: prints "used projected left": projected is the use at the reset if
# the window keeps its average rate so far
window() {
	local left=$(($2 - now))
	[ $left -lt 0 ] && left=0
	[ $left -gt $3 ] && left=$3
	awk -v u="$1" -v l="$left" -v w="$3" 'BEGIN { e = w - l; printf "%d %d %d\n", u, (e > 0 ? u * w / e : u), l }'
}
read -r used5 proj5 left5 <<<"$( [ -n "$u5" ] && window "$u5" "$r5" "${QUOTA_WINDOW_5H:-18000}" || echo "- - -")"
read -r usedw projw leftw <<<"$( [ -n "$uw" ] && window "$uw" "$rw" "${QUOTA_WINDOW_WEEK:-604800}" || echo "- - -")"
seg() { [ "$2" = - ] && return; printf '%s%s %s%% (at this rate %s%% by its reset in %s)' "${sep:-}" "$1" "$2" "$3" "$(hm "$4")"; sep="; "; }
summary="$(seg "${QUOTA_LABEL_5H:-5h}" "$used5" "$proj5" "$left5"; seg "${QUOTA_LABEL_WEEK:-weekly}" "$usedw" "$projw" "$leftw")${us:+; spend ${us}%}${QUOTA_NOTE:-}"

case "${1:-}" in
"") echo "$summary"; [ $((now - at)) -gt "$stale" ] && echo "snapshot is $(hm $((now - at))) old"; exit 0 ;;
-h | --help | help) usage; exit 0 ;;
*) usage >&2; exit 2 ;;
esac
