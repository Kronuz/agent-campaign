#!/usr/bin/env bash
# Find what blocks a shared host: holds past their limit or exclusive, requests queued too long,
# queued requests whose caller is gone, light work starved by queued strict holds, load, disk.
# Prints one line per problem, "host|key|message", and exits 1 when there is any, 0 when clean.
#   blockers.sh                          check this host once
#   blockers.sh --remote NAME=SESSION... also check hosts through etctl sessions (each needs
#                                        this script at ~/.local/bin/blockers; --install puts it)
#   blockers.sh --install NAME=SESSION   copy this script to a remote host
#   blockers.sh --watch MIN [--remote ...]  check every MIN minutes, using no tokens; exit 1 with
#                                        the report only when a problem appears that was not in
#                                        the last report, so the lead wakes on news alone
# Thresholds in minutes: BLOCKERS_HOLD_MIN (15), BLOCKERS_WAIT_MIN (45), BLOCKERS_ORPHAN_MIN (120).
set -u
dir=${QUIET_DIR:-$HOME/.scratch/quiet}
HOLD=${BLOCKERS_HOLD_MIN:-15} WAIT=${BLOCKERS_WAIT_MIN:-45} ORPHAN=${BLOCKERS_ORPHAN_MIN:-120}
host=$(hostname -s)
ncpu=$(getconf _NPROCESSORS_ONLN 2>/dev/null || sysctl -n hw.ncpu)

minutes() {  # ps etime [[dd-]hh:]mm:ss -> whole minutes
	echo "$1" | awk -F'[-:]' '{ if (NF == 4) print $1*1440 + $2*60 + $3; else if (NF == 3) print $1*60 + $2; else print $1 + 0 }'
}
field() { sed -n "s/^$2=//p" "$1" 2>/dev/null; }
alive() { [ -n "$1" ] && ps -p "$1" >/dev/null 2>&1; }

check() {  # this host
	local now f name pid lvl cpus until age pp strict_waiting=0 light_waiting=0 oldest_light=0 strict_running=0
	now=$(date +%s)
	[ -d "$dir" ] || dir=/nonexistent
	for f in "$dir"/*; do
		[ -f "$f" ] || continue
		name=$(basename "$f")
		case "$name" in *.tmp | *.starving) continue ;; esac
		pid=$(field "$f" pid) until=$(field "$f" until)
		alive "$pid" && [ -n "$until" ] && [ "$now" -lt "$until" ] || continue
		age=$(( (now - $(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f")) / 60 ))
		pp=$(ps -o ppid= -p "$pid" | tr -d ' ')
		case "$name" in
		*.pending)
			name=${name%.pending}
			[ -z "$(field "$f" cpus)" ] && strict_waiting=$((strict_waiting + 1))
			[ "$age" -ge "$WAIT" ] && echo "$host|wait|$name|strict request $name queued ${age} min (pid $pid)"
			[ "$pp" = 1 ] && [ "$age" -ge "$ORPHAN" ] && echo "$host|orphan|$name|strict request $name queued ${age} min, parent gone: kill $pid unless an agent owns it"
			;;
		*)
			lvl=$(field "$f" level) cpus=$(field "$f" cpus)
			[ "${lvl:-strict}" = strict ] && strict_running=1
			if [ "${lvl:-strict}" = strict ] && [ -z "$cpus" ]; then
				[ "$age" -ge "$HOLD" ] && echo "$host|hold|$name|exclusive hold $name running ${age} min, blocks all work: $(field "$f" what) ($(field "$f" who))"
			elif [ "$age" -ge "$((HOLD * 4))" ]; then
				echo "$host|hold|$name|${lvl} hold $name running ${age} min: $(field "$f" what)"
			fi
			;;
		esac
	done
	# light requests leave no file: a quiet process at level light with no hold file of its name
	while read -r pid pp et rest; do
		name=$(echo "$rest" | awk '{ for (i = 1; i < NF; i++) if ($i == "light") { print $(i + 1); exit } }')
		[ -n "$name" ] && [ ! -f "$dir/$name" ] || continue
		age=$(minutes "$et")
		light_waiting=$((light_waiting + 1)); [ "$age" -gt "$oldest_light" ] && oldest_light=$age
		[ "$age" -ge "$WAIT" ] && echo "$host|wait|$name|light request $name queued ${age} min (pid $pid)"
		[ "$pp" = 1 ] && [ "$age" -ge "$ORPHAN" ] && echo "$host|orphan|$name|light request $name queued ${age} min, parent gone: kill $pid unless an agent owns it"
	done < <(ps -eo pid=,ppid=,etime=,command= | awk '$4 ~ /(^|\/)(bash|sh)$/ && $5 ~ /quiet(\.sh)?$/ && $6 == "light"')
	[ "$strict_waiting" -gt 0 ] && [ "$light_waiting" -gt 0 ] && [ "$oldest_light" -ge 15 ] &&
		echo "$host|starve|-|$light_waiting light requests waiting (oldest ${oldest_light} min) behind $strict_waiting queued exclusive strict requests: move builds to another host"
	local load1; load1=$(uptime | sed 's/.*load averages*: *//' | awk -F'[, ]+' '{ print int($3) }')  # 15-minute average: only sustained overload is a blocker
	# a running strict hold owns the load (a threaded benchmark); flag load only without one
	[ "$load1" -ge "$ncpu" ] && [ "$strict_running" -eq 0 ] && echo "$host|load|-|load $load1 on $ncpu CPUs"
	local free; free=$(df -Pk "$HOME" | awk 'NR == 2 { print int($4 / 1048576) }')
	[ "$free" -lt 5 ] && echo "$host|disk|-|${free} GiB free"
	[ "$load1" -lt $((ncpu / 4)) ] && [ "$strict_waiting" -eq 0 ] && [ "$light_waiting" -eq 0 ] && echo "$host|idle|-|idle: load $load1 on $ncpu CPUs, nothing queued"
	return 0
}

remotes=() mode=once every=0
while [ $# -gt 0 ]; do
	case $1 in
	--remote) shift; while [ $# -gt 0 ] && [ "${1#--}" = "$1" ]; do remotes+=("$1"); shift; done ;;
	--watch) every=$2 mode=watch; shift 2 ;;
	--install) shift  # raw PTY push (the remote-machine skill's method), checked by hash
		n=$(wc -c < "$0" | tr -d ' ') want=$(shasum -a 256 < "$0" | cut -c1-64)
		for r in "$@"; do
			s=${r#*=} c=$(etctl info "${r#*=}" | sed -n 's/^headCursor=//p')
			etctl writeln "$s" "mkdir -p ~/.local/bin; O=\$(stty -g); stty raw -echo; echo READY; head -c $n > ~/.local/bin/blockers; stty \"\$O\"; chmod +x ~/.local/bin/blockers"
			etctl expect "$s" 'READY\r?\n' --cursor "$c" >/dev/null && etctl write "$s" < "$0" >/dev/null && etctl wait "$s" --idle 1.0 --timeout 60 >/dev/null
			got=$(etctl run "$s" '{ shasum -a 256 || sha256sum; } < ~/.local/bin/blockers | cut -c1-64' --framing mark --timeout 20 | tr -d '\r' | tail -1)
			[ "$got" = "$want" ] && echo "${r%%=*}: installed" || echo "${r%%=*}: install FAILED"
		done
		exit 0 ;;
	--local-lines) check; exit 0 ;;
	*) sed -n 2,13p "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
	esac
done

all() {  # every host's lines; an "idle" line only matters when some host has work queued
	local out r
	out=$(check)
	for r in ${remotes[@]+"${remotes[@]}"}; do
		out+=$'\n'$(etctl run "${r#*=}" 'bash ~/.local/bin/blockers --local-lines' --framing mark --timeout 60 2>&1 | tr -d '\r' |
			grep '|' | sed "s/^[^|]*|/${r%%=*}|/" || echo "${r%%=*}|unreachable|-|etctl session ${r#*=} did not answer")
	done
	out=$(printf '%s\n' "$out" | grep -v '^$')
	printf '%s\n' "$out" | grep -qE '\|(wait|starve)\|' || out=$(printf '%s\n' "$out" | grep -v '|idle|')
	printf '%s\n' "$out" | grep -v '^$'
}

# Clears this host's cmux pills of holds that no longer run: quiet removes its pill in an exit trap,
# which a SIGKILL skips. Silent; nothing to report.
reap_pills() {
	[ -n "${CMUX_WORKSPACE_ID:-}" ] && command -v cmux > /dev/null || return 0
	local live k
	live=$(ps -axo command | grep -oE 'quiet (light|strict|exclusive) [A-Za-z0-9_.-]+' | awk '{print $3}' | sort -u)
	for k in $(cmux sidebar-state 2> /dev/null | grep -oE '^  quiet-[A-Za-z0-9_.-]+' | sed 's/^  quiet-//'); do
		printf '%s\n' "$live" | grep -qx "$k" || cmux clear-status "quiet-$k" > /dev/null 2>&1
	done
}

if [ "$mode" = once ]; then
	reap_pills
	out=$(all); [ -z "$out" ] && exit 0
	printf '%s\n' "$out" | cut -d'|' -f1,4 | sed 's/|/: /'; exit 1
fi
seen=$dir/.blockers-seen
while true; do
	reap_pills
	out=$(all)
	keys=$(printf '%s\n' "$out" | cut -d'|' -f1-3 | grep -v '^$' | sort -u)
	new=$(comm -23 <(printf '%s\n' "$keys") <(sort -u "$seen" 2>/dev/null) | grep -v '^$')
	printf '%s\n' "$keys" > "$seen"  # forget what cleared, so a problem that returns wakes again
	if [ -n "$new" ]; then
		echo "blockers at $(date +%H:%M) (new: $(echo "$new" | cut -d'|' -f1-2 | tr '\n' ' '))"
		printf '%s\n' "$out" | cut -d'|' -f1,4 | sed 's/|/: /'
		exit 1
	fi
	sleep $((every * 60))
done
