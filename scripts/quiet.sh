#!/usr/bin/env bash
# Shares one machine between agents' builds, tests and measurements through hold files in
# ~/.scratch/quiet/. Levels: light (builds, tests, fuzz; a few at once, nice 19), strict (a
# measurement pinned to its own cores, light work beside it), exclusive (a measurement alone on
# the machine). `quiet --help` is the manual. A hold lives while its process runs and before its
# deadline; dead or expired holds are removed on sight.
set -uo pipefail
# exclusive is a strict hold that takes no core set
exclusive=0
[ "${QUIET_LEVEL:-}" = exclusive ] && QUIET_LEVEL=strict exclusive=1
dir="${QUIET_DIR:-$HOME/.scratch/quiet}"  # QUIET_MINUTE (seconds per minute, 60) exists for tests
mkdir -p "$dir/pills"

ncpu=$(sysctl -n hw.ncpu 2>/dev/null || nproc --all 2>/dev/null || echo 4)
# Core partitioning: a strict hold is pinned to QUIET_STRICT_CPUS (taskset on Linux, cpuset on
# FreeBSD) and light holds started while one is live or queued take the other cores. The halves
# still share L3 and memory bandwidth (a 32 MB random walk slowed 58% beside a 4-job build on an
# 8-core VM), which is why cache-sensitive numbers take exclusive.
expand_list() {  # CPU list $1 -> one CPU per line
	local p i ps
	IFS=, read -ra ps <<< "$1"
	for p in "${ps[@]}"; do
		case $p in *-*) seq "${p%-*}" "${p#*-}" ;; *) echo "$p" ;; esac
	done
}
complement() {  # CPU list $1 -> the CPUs of this machine not in it, as a list
	local set i out=""
	set=",$(expand_list "$1" | paste -sd, -),"
	for i in $(seq 0 $((ncpu - 1))); do case "$set" in *",$i,"*) ;; *) out="$out${out:+,}$i" ;; esac; done
	echo "$out"
}
# The partition value of a strict hold is a CPU list (Linux taskset, FreeBSD cpuset). macOS has
# no affinity: beside a measurement, 4 build-like loads cost 7-8% (Intel) and 5-10% (Apple
# Silicon) of CPU-bound timing at nice 19 or background QoS alike, and nothing when SIGSTOPped,
# so a strict hold there is exclusive or pauses light holds (QUIET_PAUSE=1).
case "$(uname -s)" in
Darwin) pin_tool=none ;;
FreeBSD) pin_tool=cpuset ;;
*) pin_tool=taskset ;;
esac
light_of() { complement "$1"; }  # the CPUs light holds get while a strict hold uses $1
pin_cmd() {  # value $1 -> prints the command words that confine a job to it
	case "$1" in
	"") ;;
	*) if [ "$pin_tool" = cpuset ]; then echo cpuset -l "$1"; else echo taskset -c "$1"; fi ;;
	esac
}
# Local VMs: pinning a command never reaches a VM's vCPU threads, so a strict hold on Linux
# moves the VMs themselves. QUIET_VM names the measured VM (pids or a pgrep -f pattern): it goes to
# the strict set; every other qemu process of this user goes to the light set; each thread's mask
# is saved and restored when the hold ends. Elsewhere there is nothing to pin: strict on a host
# with a busy VM needs exclusive.
vm_pids() {  # $1: pids or a pgrep -f pattern -> pids
	case "$1" in *[!0-9,\ ]*) pgrep -u "$(id -u)" -f "qemu-system.*$1" 2>/dev/null ;; *) tr ', ' '\n\n' <<< "$1" | grep . ;; esac
}
vm_move() {  # set, pids...: records each thread's mask in the hold file, then pins it to set
	local set=$1 pid t m err; shift
	for pid in "$@"; do
		for t in /proc/"$pid"/task/*; do
			t=${t##*/}
			grep -q "^vm=$t:" "$hold_file" 2>/dev/null && continue
			m=$(taskset -p "$t" 2>/dev/null | awk '{ print $NF }')
			[ -n "$m" ] || continue
			echo "vm=$t:$m" >> "$hold_file"
			err=$(taskset -cp "$set" "$t" 2>&1 > /dev/null) || echo "quiet: cannot pin thread $t to $set: $err" >&2
		done
	done
}
vm_check() {  # set, pids...: warns when a VM has more vCPU threads than the set has CPUs
	local set=$1 have pid n; shift
	have=$(expand_list "$set" | wc -l)
	for pid in "$@"; do
		n=$(grep -l '^CPU ' /proc/"$pid"/task/*/comm 2> /dev/null | wc -l)
		[ "$n" -le "$have" ] || echo "quiet: VM $pid has $n vCPUs on $have strict CPUs; expect lock-holder preemption in the guest" >&2
	done
}
vm_recover() {  # hold file: puts back the masks it recorded
	local e
	for e in $(field "$1" vm); do [ -d /proc/"${e%%:*}" ] && taskset -p "${e#*:}" "${e%%:*}" > /dev/null 2>&1; done
	return 0
}
resume_file() {  # hold file: SIGCONTs the processes it paused
	local p
	for p in $(field "$1" paused); do kill -s CONT "$p" 2> /dev/null; done
	return 0
}
drop() {  # removes hold file $1 after undoing what it did to other processes
	resume_file "$1"; vm_recover "$1"; rm -f "$1"
}
hold_file=
core_set() {  # default strict set: the upper half of the physical cores (SMT siblings stay together)
	local g=() x i half out="" p=""
	[ -r /sys/devices/cpu_core/cpus ] && p=",$(expand_list "$(cat /sys/devices/cpu_core/cpus)" | paste -sd, -),"
	while read -r x; do
		[ -z "$p" ] || case "$p" in *",${x%%[,-]*},"*) ;; *) continue ;; esac
		g+=("$x")
	done < <(cat /sys/devices/system/cpu/cpu[0-9]*/topology/thread_siblings_list 2> /dev/null |
		sort -u | awk '{ split($0, a, /[,-]/); print a[1], $0 }' | sort -n | cut -d' ' -f2-)
	half=$((${#g[@]} / 2))
	if [ ${#g[@]} -lt 2 ]; then echo "$((ncpu / 2))-$((ncpu - 1))"; return; fi
	for ((i = half; i < ${#g[@]}; i++)); do out="$out${out:+,}${g[$i]}"; done
	echo "$out"
}
strict_set() {  # the partition value a strict hold would use; empty when not partitioning
	[ "$exclusive" = 1 ] && return
	[ "$pin_tool" = none ] && return
	command -v "$pin_tool" > /dev/null 2>&1 && [ "$ncpu" -ge 2 ] || return
	local s=${QUIET_STRICT_CPUS:-$(core_set)}
	[ -n "$(complement "$s")" ] && [ "$(complement "$s")" != "$(seq -s, 0 $((ncpu - 1)))" ] && echo "$s"
}
load1() {  # the 1-minute load average, as an integer
	local l
	l=$(sysctl -n vm.loadavg 2>/dev/null | awk '{ print $2 }')
	[ -n "$l" ] || l=$(cut -d' ' -f1 /proc/loadavg 2>/dev/null)
	echo "${l%%.*}"
}
free_gb() { df -Pk "$HOME" | awk 'NR == 2 { print int($4 / 1048576) }'; }
field() { sed -n "s/^$2=//p" "$1" 2> /dev/null; }  # a hold dropped by another call reads as empty
live() {  # file: true when its pid runs (0: no pid) and its deadline is ahead
	local pid until
	pid=$(field "$1" pid) until=$(field "$1" until)
	[ -n "$until" ] && [ "$(date +%s)" -lt "$until" ] || return 1
	[ "${pid:-0}" = 0 ] || kill -0 "$pid" 2>/dev/null
}
clock() { date -r "$1" +%T 2>/dev/null || date -d @"$1" +%T; }
holds() {  # prints live holds, removes the rest; returns 1 for none, 2 for light only, 0 for strict
	local any=1 f level who
	reap_pills
	for f in "$dir"/*; do
		[ -f "$f" ] || continue
		case "$f" in *.tmp | *.pending | *.starving | */.lock) continue ;; esac
		if live "$f"; then
			level=$(field "$f" level) who=$(field "$f" who) cpus=$(field "$f" cpus)
			level=${level:-strict}
			label=$level; [ "$level" = strict ] && [ -z "$cpus" ] && label=exclusive
			[ "$(field "$f" pause)" = 1 ] && label="strict, pausing light holds"
			echo "$(basename "$f") [$label${cpus:+ cpus $cpus}]: $(field "$f" what) (since $(field "$f" since), until $(clock "$(field "$f" until)")${who:+, by $who})"
			# a strict hold pinned to its own cores leaves the rest to light holds
			if [ "$level" = light ] || [ -n "$cpus" ]; then [ $any = 1 ] && any=2; else any=0; fi
		else
			drop "$f"
		fi
	done
	return $any
}
waiting() {  # prints the requests still waiting for a hold (strict and exclusive, and starving builds)
	local f pid
	for f in "$dir"/*.pending "$dir"/*.starving; do
		[ -f "$f" ] || continue
		pid=$(field "$f" pid)
		[ -n "$pid" ] && kill -0 "$pid" 2> /dev/null || continue
		echo "${f##*/} [waiting, $(field "$f" level)]: $(field "$f" what) (since $(field "$f" since))"
	done
}
count() {  # level: the number of live holds at that level, excluding NAME $2
	local n=0 f
	for f in "$dir"/*; do
		[ -f "$f" ] || continue
		case "$f" in *.tmp | *.pending | *.starving | */.lock) continue ;; esac
		[ "$(basename "$f")" = "${2:-}" ] && continue
		live "$f" || { drop "$f"; continue; }
		[ "$(field "$f" level)" = "$1" ] && n=$((n + 1))
	done
	echo $n
}
strict_state() {  # prints "block" when a live or waiting strict hold other than NAME $1 needs
	# the whole machine, "cpus:LIST" when one runs pinned to LIST, else "none"; with $2 set
	# (a starving light request), waiting strict requests are ignored
	local f c found=none
	for f in "$dir"/*; do
		[ -f "$f" ] || continue
		case "$f" in *.tmp | *.starving | */.lock) continue ;; esac
		[ -n "${2:-}" ] && case "$f" in *.pending) continue ;; esac
		[ "$(basename "$f" .pending)" = "${1:-}" ] && continue
		live "$f" || { drop "$f"; continue; }
		case "$f" in *.pending) ;; *) [ "$(field "$f" level)" = light ] && continue ;; esac
		c=$(field "$f" cpus)
		if [ -z "$c" ]; then echo block; return; fi
		found=cpus:$c
	done
	echo "$found"
}
unpinned_light() {  # the number of live light holds other than NAME $1 not pinned to CPUs $2
	local n=0 f
	for f in "$dir"/*; do
		[ -f "$f" ] || continue
		case "$f" in *.tmp | *.pending | *.starving | */.lock) continue ;; esac
		[ "$(basename "$f")" = "${1:-}" ] && continue
		live "$f" || { drop "$f"; continue; }
		[ "$(field "$f" level)" = light ] && [ "$(field "$f" cpus)" != "$2" ] && n=$((n + 1))
	done
	echo $n
}
write() {  # name, minutes, what, pid
	printf 'what=%s\nsince=%s\nuntil=%s\npid=%s\nhost=%s\nlevel=%s\nwho=%s\ncpus=%s\npause=%s\n' "$3" "$(date +%T)" \
		"$(($(date +%s) + ${QUIET_MINUTE:-60} * $2 + $(grace)))" "$4" "$(hostname -s)" "${QUIET_LEVEL:-strict}" \
		"${QUIET_WHO:-$USER in $(basename "$PWD") (ppid $PPID)}" "${hold_cpus:-}" "${hold_pause:-}" > "$dir/$1.tmp" &&
		mv "$dir/$1.tmp" "$dir/$1"
}
grace() {  # seconds a bounded command's hold outlives its deadline, covering TERM then KILL
	if [ "${QUIET_LEVEL:-strict}" = strict ] || [ "${QUIET_KILL:-0}" = 1 ]; then echo 15; else echo 0; fi
}
starving() {  # true when a light request other than NAME $1 has waited past QUIET_STARVE_MIN
	local f
	for f in "$dir"/*.starving; do
		[ -f "$f" ] || continue
		live "$f" || { rm -f "$f"; continue; }
		[ "$(basename "$f" .starving)" = "${1:-}" ] || return 0
	done
	return 1
}
pause_ok() { [ "${QUIET_PAUSE:-}" = 1 ] && [ "$exclusive" = 0 ]; }
unpausable_light() {  # the number of live light holds other than NAME $1 that can't be paused
	local n=0 f
	for f in "$dir"/*; do
		[ -f "$f" ] || continue
		case "$f" in *.tmp | *.pending | *.starving | */.lock) continue ;; esac
		[ "$(basename "$f")" = "${1:-}" ] && continue
		live "$f" || { drop "$f"; continue; }
		[ "$(field "$f" level)" = light ] && [ "$(field "$f" pausable)" != 1 ] && n=$((n + 1))
	done
	echo $n
}
stop_tree() {  # file, pid: SIGSTOPs pid's descendants top-down, recording each first
	local c
	for c in $(pgrep -P "$2"); do  # a stopped parent can't fork, so its children are all found
		echo "paused=$c" >> "$1"
		kill -s STOP "$c" 2> /dev/null && stop_tree "$1" "$c"
	done
}
pause_light() {  # NAME: SIGSTOPs the job trees of the live, pausable light holds
	local f
	for f in "$dir"/*; do
		[ -f "$f" ] || continue
		case "$f" in *.tmp | *.pending | *.starving | */.lock) continue ;; esac
		[ "$(basename "$f")" = "$1" ] && continue
		live "$f" && [ "$(field "$f" level)" = light ] && [ "$(field "$f" pausable)" = 1 ] || continue
		stop_tree "$dir/$1" "$(field "$f" pid)"
	done
}
locked() {  # runs "$@" holding the directory's lock, so check-then-hold is atomic
	local n=0
	until mkdir "$dir/.lock" 2>/dev/null; do
		n=$((n + 1))
		# a lock older than a minute belongs to a process that died inside it
		[ $n -gt 600 ] && { rm -rf "$dir/.lock"; n=0; }
		sleep 0.1
	done
	"$@"; local r=$?
	rmdir "$dir/.lock"
	return $r
}
acquire() {  # name, minutes, what, pid: holds once its level's rule allows
	if [ "${QUIET_LEVEL:-strict}" = strict ]; then  # queue ahead of new light holds
		printf 'pid=%s\nuntil=%s\ncpus=%s\nwhat=%s\nsince=%s\nlevel=%s\n' "$4" "$(($(date +%s) + 60 * ($2 + 120)))" "$(strict_set)" \
			"$3" "$(date +%T)" "${QUIET_LEVEL:-strict}" > "$dir/$1.pending"
	fi
	local start; start=$(date +%s)
	while true; do
		# a light request that waited long enough goes before new strict holds (bounded service);
		# it says so before taking the lock, which busy pollers may hold most of the time
		if [ "${QUIET_LEVEL:-strict}" = light ] && [ ! -e "$dir/$1.starving" ] &&
			[ $(($(date +%s) - start)) -ge $((${QUIET_MINUTE:-60} * ${QUIET_STARVE_MIN:-10})) ]; then
			printf 'pid=%s\nuntil=%s\nwhat=%s\nsince=%s\nlevel=light\n' "$4" "$(($(date +%s) + 86400))" "$3" "$(date +%T)" > "$dir/$1.starving"
		fi
		if locked try_hold "$@" "$start"; then rm -f "$dir/$1.pending" "$dir/$1.starving"; return 0; fi
		sleep "${QUIET_POLL:-5}"
	done
}
older_pending() {  # name: true when a live strict request queued before NAME's still waits (FIFO)
	local mine f t
	mine=$(stat -f %m "$dir/$1.pending" 2>/dev/null || stat -c %Y "$dir/$1.pending" 2>/dev/null) || return 1
	for f in "$dir"/*.pending; do
		[ -f "$f" ] && [ "$f" != "$dir/$1.pending" ] || continue
		live "$f" || continue
		t=$(stat -f %m "$f" 2>/dev/null || stat -c %Y "$f" 2>/dev/null) || continue
		[ "$t" -lt "$mine" ] && return 0
	done
	return 1
}
try_hold() {  # name, minutes, what, pid, start
	[ "$(free_gb)" -ge "${QUIET_DISK_MIN_GB:-5}" ] || return 1
	if [ "${QUIET_LEVEL:-strict}" = strict ]; then
		[ "$(count strict "$1")" = 0 ] || return 1
		older_pending "$1" && return 1  # strict requests take turns in arrival order
		starving "$1" && return 1  # a light request that waited too long goes first
		hold_cpus=$(strict_set) hold_pause=
		if [ -n "$hold_cpus" ]; then  # only light holds on other cores may overlap
			[ "$(unpinned_light "$1" "$(light_of "$hold_cpus")")" = 0 ] || return 1
		elif pause_ok; then  # light holds are paused instead of waited out
			hold_pause=1
			[ "$(unpausable_light "$1")" = 0 ] || return 1
		else
			[ "$(count light "$1")" = 0 ] || return 1
			# let the load settle, but not forever: other sessions' work isn't ours to wait out
			if [ "$(load1)" -ge $((ncpu / 4 > 1 ? ncpu / 4 : 1)) ] &&
				[ $(($(date +%s) - $5)) -lt $((60 * ${QUIET_LOAD_WAIT:-10})) ]; then return 1; fi
		fi
	else
		st=$(strict_state "$1" "$([ -e "$dir/$1.starving" ] && echo starving)")
		[ "$st" = block ] && return 1
		case "$st" in cpus:*) hold_cpus=$(light_of "${st#cpus:}") ;; *) hold_cpus= ;; esac
		[ "$(count light "$1")" -lt "${QUIET_LIGHT_SLOTS:-$((ncpu / 4 > 1 ? ncpu / 4 : 1))}" ] || return 1
		[ "$(load1)" -lt "${QUIET_MAX_LOAD:-$((ncpu * 3 / 4))}" ] || return 1
	fi
	write "$1" "$2" "$3" "$4"
}

# Sidebar pills and notifications through report (cmux, else the desktop); a no-op without it.
report() { command -v report > /dev/null && command report "$@" 2> /dev/null; return 0; }
# Each pill has a marker in pills/ naming its run's pid, so a pill whose run died without its
# trap (SIGKILL) is cleared on sight, like a dead hold.
pill() {  # value, icon: sets this run's pill
	printf 'pid=%s\nuntil=%s\nws=%s\n' $$ "$(($(date +%s) + 60 * (minutes + 120)))" "${CMUX_WORKSPACE_ID:-}" > "$dir/pills/$name"
	report status "quiet-$name" "$1" "$2"
}
unpill() { report unstatus "quiet-$name"; rm -f "$dir/pills/$name"; }
reap_pills() {  # clears the pills of dead runs, in the workspace each was set in
	local f
	for f in "$dir"/pills/*; do
		[ -f "$f" ] && ! live "$f" || continue
		CMUX_WORKSPACE_ID=$(field "$f" ws) report unstatus "quiet-$(basename "$f")"
		rm -f "$f"
	done
}
finished() {  # exit code: clears the hold's pill; notifies when it ran QUIET_NOTIFY_AFTER or more
	local secs=$(($(date +%s) - started))
	unpill
	[ "$secs" -ge "${QUIET_NOTIFY_AFTER:-300}" ] || return 0
	report notify "quiet $name: $( [ "$1" = 0 ] && echo "done" || echo "failed (exit $1)")" \
		"$what after $((secs / 60))m$((secs % 60))s"
}
# A measurement is valid only inside its isolation, so a strict or exclusive command (and a light
# one under QUIET_KILL=1) runs in its own process group and is stopped at the hold's deadline.
bounded() {  # cmd...: runs it until the hold's deadline; 124 when the deadline stopped it
	local left deadline code
	left=$(($(field "$hold_file" until) - $(grace) - $(date +%s)))
	[ "$left" -gt 0 ] || left=1
	deadline=$(($(date +%s) + left))
	set -m
	"$@" &
	child=$!
	( sleep "$left"; kill -TERM -"$child" 2> /dev/null; sleep 10; kill -KILL -"$child" 2> /dev/null ) > /dev/null 2>&1 < /dev/null &
	timer=$!
	disown "$timer" 2> /dev/null  # no "Killed" notice when it is stopped
	set +m
	wait "$child"; code=$?
	# SIGKILL: a TERMed subshell can still run its next command before its inherited trap is reset
	kill -KILL -"$timer" 2> /dev/null; timer=''
	if [ "$(date +%s)" -ge "$deadline" ]; then
		echo "quiet: $name reached its $minutes-minute deadline and was stopped; its result is invalid (split longer work)" >&2
		code=124
	fi
	child=''
	return "$code"
}

usage() {
	cat <<'USAGE'
quiet: share one machine between agents' builds, tests and measurements.

Usage:
  quiet LEVEL NAME MINUTES WHAT -- CMD...   wait for a turn at LEVEL, then run CMD
  quiet run NAME MINUTES WHAT -- CMD...     the same, at $QUIET_LEVEL (default strict)
  quiet hold NAME MINUTES WHAT [PID]        hold the machine exclusively until released,
                                            the deadline, or PID exits
  quiet release NAME                        end a hold
  quiet list                                show the live holds: level, what, who, until
  quiet check                               exit 0 free; 2 busy, work can still start;
                                            1 blocked: exclusive hold or disk full
  quiet wait [--light]                      block until free (--light: until light
                                            work can start)

Levels:
  light      Builds, tests, fuzz. A few run at once (a quarter of the CPUs), at nice 19.
             While a strict hold runs they use only the cores it leaves free.
  strict     A measurement on its own cores (the upper half). One at a time; light
             work keeps running on the other half. Expect some noise from the shared
             cache and memory bandwidth.
  exclusive  A measurement with the machine to itself: waits for every hold and for
             the load to settle, and nothing else starts until it ends. Use it for
             cache-sensitive A/Bs and numbers you publish.

NAME names the hold, MINUTES bounds it (it also ends when CMD exits), WHAT says what
it is for. The level is recorded in the hold, so every agent's quiet honors it.
A strict or exclusive hold lasts at most QUIET_MAX_MIN minutes (30), and its command
is stopped at the deadline (exit 124): a measurement never runs on unheld. A light
command keeps running unheld past its deadline unless QUIET_KILL=1.

Platforms: Linux pins cores with taskset, FreeBSD with cpuset. Macs and single-CPU machines
can't split cores: strict acts as exclusive, or with QUIET_PAUSE=1 stops light holds (SIGSTOP)
while it runs and resumes them after.
Local VMs: on Linux, strict also moves qemu VMs (QUIET_VM names the measured one), so a
measurement inside a VM can be strict; elsewhere VM threads can't be pinned, so use exclusive.

Examples:
  quiet light build 30 "cargo test" -- cargo test -j4
  quiet strict smoke 20 "detector smoke" -- ./smoke.sh
  quiet exclusive ab 20 "gate A/B" -- ./bench.sh

Environment:
  QUIET_LEVEL          level for `run` (light, strict, exclusive; default strict)
  QUIET_STRICT_CPUS    the strict core set (default: upper half of the physical cores,
                       SMT siblings together, e.g. 4-7)
  QUIET_PAUSE          1: on a host that can't split cores, strict pauses the light holds
                       instead of waiting for them (not exclusive, not VMs; light holds
                       started from a terminal are not pausable and are waited for)
  QUIET_VM             strict on Linux: the local VM being measured (pids or a pgrep -f
                       pattern); its threads join the strict set, other qemu VMs of this
                       user move to the light set, and both are restored after
  QUIET_LIGHT_SLOTS    light holds at once (default: CPUs / 4)
  QUIET_MAX_LOAD       no new light hold above this load (default: CPUs * 3 / 4)
  QUIET_LOAD_WAIT      minutes exclusive waits for the load to settle (default 10)
  QUIET_MAX_MIN        longest strict or exclusive hold, in minutes (default 30)
  QUIET_KILL           1: stop a light command at its deadline too (job runners set it)
  QUIET_STARVE_MIN     a light request waiting this long goes before new strict holds (10)
  QUIET_DISK_MIN_GB    no new hold below this free disk (default 5)
  QUIET_DISK_WARN_GB   `check` warns below this (default 20)
  QUIET_WHO            holder name in `list` (default: user, directory, parent pid)
  QUIET_NOTIFY_AFTER   notify (via `report`) when a run hold took this many seconds or
                       more (default 300); its sidebar pill shows queued, then running
  QUIET_DIR            state directory (default ~/.scratch/quiet)

quiet coordinates this machine only; a remote host runs its own quiet.
USAGE
}
case "${1:-}" in
light | strict | exclusive)  # quiet LEVEL NAME ...: run at that level, whatever the environment says
	case "$1" in
	light) QUIET_LEVEL=light ;;
	strict) QUIET_LEVEL=strict ;;
	exclusive) QUIET_LEVEL=strict exclusive=1 ;;
	esac
	shift; set -- run "$@"
	;;
esac
case "${1:-}" in
run)
	name=$2 minutes=$3 what=$4; shift 4; [ "${1:-}" = -- ] && shift
	case "$minutes" in '' | *[!0-9]* | 0) echo "quiet: MINUTES must be a positive integer, not '$minutes'" >&2; exit 2 ;; esac
	if [ "${QUIET_LEVEL:-strict}" = strict ] && [ "$minutes" -gt "${QUIET_MAX_MIN:-30}" ]; then
		echo "quiet: a strict or exclusive hold lasts at most ${QUIET_MAX_MIN:-30} minutes; split the run into shorter pieces" >&2
		exit 2
	fi
	reap_pills
	pill "queued: $what" hourglass
	trap 'unpill; rm -f "$dir/$name.starving"' EXIT  # a hold killed while queued leaves nothing
	trap 'exit 143' TERM; trap 'exit 130' INT; trap 'exit 129' HUP
	acquire "$name" "$minutes" "$what" $$
	hold_file=$dir/$name watchdog='' started=$(date +%s) child='' timer=''
	pill "${QUIET_LEVEL:-strict}: $what" hammer
	trap 'r=$?; [ -n "$child" ] && kill -TERM -"$child" 2> /dev/null; [ -n "$timer" ] && kill -KILL -"$timer" 2> /dev/null
		finished $r; [ -n "$watchdog" ] && kill "$watchdog" 2> /dev/null; drop "$hold_file"; rm -f "$hold_file.pending"' EXIT
	cpus=$(field "$hold_file" cpus)
	read -ra pin <<< "$(pin_cmd "$cpus")"
	if [ "${QUIET_LEVEL:-strict}" = strict ] && [ "$(field "$hold_file" pause)" = 1 ]; then
		pause_light "$name"
		# resumes the paused jobs if this hold dies without its trap, or at its deadline; its own
		# process group (set -m) outlives a kill of the caller's group
		set -m
		( until=$(field "$hold_file" until)
			while kill -0 $$ 2> /dev/null && [ "$(date +%s)" -lt "$until" ]; do sleep 2; done
			resume_file "$hold_file" ) > /dev/null 2>&1 < /dev/null &
		watchdog=$!
		set +m
	fi
	if [ "${QUIET_LEVEL:-strict}" = strict ] && [ "$pin_tool" = taskset ] && [ -n "$cpus" ] && [ -d /proc ]; then
		measured=$(vm_pids "${QUIET_VM:-}")
		others=$(pgrep -u "$(id -u)" -f 'qemu-system' 2>/dev/null | grep -vxF -f <(printf '%s\n' $measured) || true)
		[ -n "$measured" ] && vm_check "$cpus" $measured
		[ -n "$measured" ] && vm_move "$cpus" $measured
		[ -n "$others" ] && vm_move "$(light_of "$cpus")" $others
		if [ -n "$measured$others" ]; then sleep 1; vm_move "$cpus" $measured; vm_move "$(light_of "$cpus")" $others; fi
	fi
	if [ "${QUIET_LEVEL:-strict}" = light ]; then
		# a job reading the terminal can't be stopped under its shell; the rest can be paused
		[ -t 0 ] || echo "pausable=1" >> "$hold_file"
		if [ "${QUIET_KILL:-0}" = 1 ]; then bounded nice -n 19 ${pin[@]+"${pin[@]}"} "$@"
		else nice -n 19 ${pin[@]+"${pin[@]}"} "$@"; fi
	else bounded ${pin[@]+"${pin[@]}"} "$@"; fi
	;;
hold) locked write "$2" "$3" "$4" "${5:-0}" ;;
release) drop "$dir/$2" ;;
check)
	holds
	case $? in 1) code=0 ;; 2) code=2 ;; *) code=1 ;; esac
	# Free GiB on $HOME's volume: below QUIET_DISK_MIN_GB builds fail oddly, so it blocks.
	free=$(free_gb)
	if [ "$free" -lt "${QUIET_DISK_MIN_GB:-5}" ]; then
		echo "disk: ${free} GiB free on $HOME's volume, below ${QUIET_DISK_MIN_GB:-5}: free space first"
		code=1
	elif [ "$free" -lt "${QUIET_DISK_WARN_GB:-20}" ]; then
		echo "disk: ${free} GiB free on $HOME's volume, below ${QUIET_DISK_WARN_GB:-20}: delete build dirs you no longer need"
		[ "$code" = 0 ] && code=2
	fi
	l=$(load1)
	if [ "$l" -ge "${QUIET_MAX_LOAD:-$((ncpu * 3 / 4))}" ]; then
		echo "load: $l on $ncpu CPUs, above ${QUIET_MAX_LOAD:-$((ncpu * 3 / 4))}: start no new build or test until it drops"
		[ "$code" = 0 ] && code=2
	fi
	exit "$code"
	;;
list) holds; waiting; exit 0 ;;
wait)
	while true; do
		holds > /dev/null
		r=$?
		[ $r = 1 ] && break
		[ $r = 2 ] && [ "${2:-}" = --light ] && break
		sleep 30
	done
	;;
-h | --help | help) usage; exit 0 ;;
*)
	usage >&2
	exit 2
	;;
esac
