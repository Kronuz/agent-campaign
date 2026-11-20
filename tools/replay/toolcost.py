"""What each read-only v2 command costs on this machine's real state (A5), and a v3 hot-path prototype.

Commands run against a copy of the job store's records (CAMPAIGN_JOBS points at it: `status` reaps,
and the store must not change) and the real homes, without a cmux workspace (no sidebar writes).
Each runs under `/usr/bin/time -l` for wall, user, sys, peak memory and context switches; `status`
and `brief` also run under cProfile, which counts the processes, stats and opens they make.
"""

from __future__ import annotations

import io
import json
import os
import pstats
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import sources as src
from sources import Json

CAMPAIGN = shutil.which("campaign") or str(Path.home() / ".local" / "bin" / "campaign")
QUIET = shutil.which("quiet") or str(Path.home() / ".local" / "bin" / "quiet")


COPIED = ("spec.json", "state.json", "for.json", "heartbeat", "claim")


def copy_store(out: Path) -> Path:
    """The job store's records (spec, state, for.json, heartbeat, claim; keys/ links) under OUT/jobs-copy,
    file by file: the store's build slots and logs hold millions of files a walk would visit."""
    dest = out / "jobs-copy"
    shutil.rmtree(dest, ignore_errors=True)
    (dest / "keys").mkdir(parents=True)
    for d in src.JOBS.iterdir():
        if not re.fullmatch(r"\d{6}-\d{6}-[0-9a-f]{6}", d.name):
            continue
        (dest / d.name).mkdir()
        for f in COPIED:
            if (d / f).is_file():
                shutil.copy2(d / f, dest / d.name / f)
    for link in (src.JOBS / "keys").iterdir() if (src.JOBS / "keys").is_dir() else []:
        if link.is_symlink():
            (dest / "keys" / link.name).symlink_to(os.readlink(link))
    return dest


TIME_L = {
    "real": re.compile(r"([\d.]+) real"),
    "user": re.compile(r"([\d.]+) user"),
    "sys": re.compile(r"([\d.]+) sys"),
    "maxrss": re.compile(r"(\d+)\s+maximum resident set size"),
    "vcsw": re.compile(r"(\d+)\s+voluntary context switches"),
    "ivcsw": re.compile(r"(\d+)\s+involuntary context switches"),
    "instructions": re.compile(r"(\d+)\s+instructions retired"),
}


def hold_off_worker(store: Path) -> None:
    """A fresh worker heartbeat in the copy: `status`, `wait` and others start a worker when none
    beats and jobs are queued, and one serving the copy would run its queued jobs for real."""
    beat = store / ".worker" / "heartbeat"
    beat.parent.mkdir(exist_ok=True)
    beat.touch()


def copy_workers(store: Path) -> list[int]:
    """Processes whose environment points CAMPAIGN_JOBS at the copy (none should outlive a command)."""
    ps = subprocess.run(["ps", "-E", "-A", "-o", "pid=,command="], capture_output=True, text=True, errors="replace")
    mark = f"CAMPAIGN_JOBS={store}"
    return [int(x.split()[0]) for x in ps.stdout.splitlines() if mark in x and "toolcost" not in x]


def timed(argv: list[str], cwd: Path, env: dict[str, str]) -> Json:
    hold_off_worker(Path(env["CAMPAIGN_JOBS"]))
    res = subprocess.run(
        ["/usr/bin/time", "-l", *argv], cwd=cwd, env=env, capture_output=True, text=True, errors="replace"
    )
    row: Json = {"exit": res.returncode, "out_bytes": len(res.stdout)}
    for k, rx in TIME_L.items():
        m = rx.search(res.stderr)
        row[k] = float(m[1]) if m else None
    return row


def commands(home: Path, quest: str, done_job: str) -> list[tuple[str, list[str]]]:
    return [
        ("status", [CAMPAIGN, "status"]),
        ("brief (all)", [CAMPAIGN, "brief"]),
        ("brief QUEST", [CAMPAIGN, "brief", quest]),
        ("check", [CAMPAIGN, "check"]),
        ("get QUEST", [CAMPAIGN, "get", quest, "status"]),
        ("job list", [CAMPAIGN, "job", "list"]),
        ("job show", [CAMPAIGN, "job", "show", done_job]),
        ("wait (finished job)", [CAMPAIGN, "wait", done_job, "--timeout", "1"]),
        ("quiet check", [QUIET, "check"]),
        ("python3 -c pass", [sys.executable, "-c", "pass"]),
        ("import campaign_tool", [sys.executable, "-c", "import campaign_tool"]),
    ]


def profile(argv: list[str], cwd: Path, env: dict[str, str], out: Path, name: str) -> Json:
    """cProfile of one command in a child: top functions by own time, and calls of the costly primitives."""
    prof = out / f"{name}.prof"
    hold_off_worker(Path(env["CAMPAIGN_JOBS"]))
    subprocess.run([sys.executable, "-m", "cProfile", "-o", str(prof), *argv], cwd=cwd, env=env, capture_output=True)
    st = pstats.Stats(str(prof))
    stats: dict[tuple[str, int, str], tuple[int, int, float, float, object]] = st.stats  # type: ignore[attr-defined]
    prim = {  # label: (file suffix, function name) for Python code; ("~", substring) for a built-in
        "subprocess (Popen)": ("subprocess.py", "__init__"),
        "fork_exec": ("~", "fork_exec"),
        "stat": ("~", "posix.stat"),
        "lstat": ("~", "posix.lstat"),
        "open": ("~", "io.open"),
        "listdir": ("~", "posix.listdir"),
        "scandir": ("~", "posix.scandir"),
        "json.loads": ("json/__init__.py", "loads"),
        "read": ("~", "posix.read"),
        "waitpid": ("~", "posix.waitpid"),
        "select.poll": ("~", "select.poll"),
    }
    counts: Json = {}
    for label, (fname, func) in prim.items():
        n, t = 0, 0.0
        for (f, _, fn), (_, nc, tt, ct, _) in stats.items():
            hit = func in fn if fname == "~" else fn == func and f.endswith(fname)
            if hit:
                n += nc
                t += tt if fname == "~" else ct
        counts[label] = {"calls": n, "seconds": round(t, 3)}
    buf = io.StringIO()
    pstats.Stats(str(prof), stream=buf).sort_stats("tottime").print_stats(15)
    top = [line.strip() for line in buf.getvalue().splitlines() if re.match(r"\s*\d", line)][:15]
    cum = io.StringIO()
    pstats.Stats(str(prof), stream=cum).sort_stats("cumulative").print_stats("campaign_tool", 25)
    tops = [x.strip() for x in cum.getvalue().splitlines() if "campaign_tool.py:" in x][:25]
    return {"primitives": counts, "top_tottime": top, "top_cumulative_in_tool": tops, "total_s": round(st.total_tt, 2)}  # type: ignore[attr-defined]


# ---------------------------------------------------------------- the v3 hot-path prototype


SCHEMA = """
create table jobs (id text primary key, campaign text, quest text, level text, state text, key text,
  rkey text, commit_ text, cmd text, submitted real, began real, ended real);
create index jobs_quest on jobs (campaign, quest, state);
create index jobs_key on jobs (key);
create table quests (campaign text, id text, status text, primary key (campaign, id));
"""
HOT = """
select q.id, j.state, count(j.id) from quests q left join jobs j on j.campaign = q.campaign and j.quest = q.id
where q.campaign = ? and q.status in ('active', 'review', 'blocked') group by q.id, j.state
"""


def build_db(path: Path, jobs: list[src.Job], states: dict[str, Json]) -> None:
    path.unlink(missing_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    db.executemany(
        "insert into jobs values (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (j.id, j.campaign, j.quest, j.level, j.state, j.key, j.rkey, j.commit, j.cmd, j.submitted, j.began, j.ended)
            for j in jobs
        ],
    )
    db.executemany(
        "insert into quests values (?,?,?)",
        [(c, q, r.get("status", "")) for c, st in states.items() for q, r in st.get("quests", {}).items()],
    )
    db.commit()
    db.close()


def prototype(out: Path, store: Path) -> Json:
    """Times the hot path (in-flight quests with their jobs by state) four ways, each in a fresh process."""
    homes = src.campaign_homes()
    jobs = src.load_jobs(store)
    states = {n: src.load_state(h, n) for n, h in homes.items()}
    db = out / "v3-proto.sqlite"
    build_db(db, jobs, states)
    log = out / "v3-proto.jsonl"
    with log.open("w") as f:
        for j in jobs:
            f.write(json.dumps({"id": j.id, "c": j.campaign, "q": j.quest, "s": j.state, "k": j.key}) + "\n")
    py_sqlite = (
        "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); "
        f"r=db.execute({HOT!r}, ('xapiand',)).fetchall(); print(len(r))"
    )
    py_scan = (
        "import json,os,sys; d=sys.argv[1]; n=0\n"
        "for x in os.listdir(d):\n"
        "  p=os.path.join(d,x,'state.json')\n"
        "  if os.path.exists(p): json.load(open(p)); json.load(open(os.path.join(d,x,'spec.json'))); n+=1\n"
        "print(n)"
    )
    py_log = "import json,sys; print(sum(1 for line in open(sys.argv[1]) if json.loads(line)))"
    ways = {
        "python + sqlite (indexed query)": [sys.executable, "-c", py_sqlite, str(db)],
        "sqlite3 CLI (compiled, same query)": ["sqlite3", str(db), HOT.replace("?", "'xapiand'")],
        "python: scan every job dir (v2's all_jobs read)": [sys.executable, "-c", py_scan, str(store)],
        "python: read an append-only job log": [sys.executable, "-c", py_log, str(log)],
    }
    res: Json = {"jobs": len(jobs), "db_bytes": db.stat().st_size, "log_bytes": log.stat().st_size, "ways": {}}
    for name, argv in ways.items():
        runs = [timed(argv, out, dict(os.environ)) for _ in range(5)]
        res["ways"][name] = {
            "median_real_ms": round(1000 * sorted(r["real"] or 0 for r in runs)[2], 1),
            "median_user_ms": round(1000 * sorted(r["user"] or 0 for r in runs)[2], 1),
            "median_sys_ms": round(1000 * sorted(r["sys"] or 0 for r in runs)[2], 1),
        }
    t = time.perf_counter()
    con = sqlite3.connect(db)
    for _ in range(100):
        con.execute(HOT, ("xapiand",)).fetchall()
    res["in_process_query_ms"] = round((time.perf_counter() - t) * 10, 3)
    return res


def run(out: Path, reps: int = 3) -> Json:
    out.mkdir(parents=True, exist_ok=True)
    store = copy_store(out)
    home = src.campaign_homes()["xapiand"]
    env = {k: v for k, v in os.environ.items() if not k.startswith("CMUX_")}
    env["CAMPAIGN_JOBS"] = str(store)
    env["PYTHONPATH"] = str(Path(os.path.realpath(CAMPAIGN)).parent)
    done = max(
        d.name for d in store.iterdir() if (d / "state.json").exists() and '"done"' in (d / "state.json").read_text()
    )
    mtime = (home / ".campaign" / "xapiand" / "state.json").stat().st_mtime
    rows: Json = {}
    for name, argv in commands(home, "v2-replay", done):
        runs = [timed(argv, home, env) for _ in range(reps)]
        rows[name] = {
            k: round(sorted(r[k] for r in runs if r[k] is not None)[len(runs) // 2], 3)
            for k in ("real", "user", "sys", "maxrss", "vcsw", "ivcsw", "instructions")
            if any(r[k] is not None for r in runs)
        }
        rows[name]["exit"] = runs[0]["exit"]
    profiles = {
        name: profile(argv, home, env, out, name.split()[0]) for name, argv in commands(home, "v2-replay", done)[:2]
    }
    load = os.getloadavg()
    res: Json = {
        "at": src.iso(time.time()),
        "load": [round(x, 1) for x in load],
        "store_dirs": sum(1 for _ in store.iterdir()),
        "state_json_bytes": {
            n: (h / ".campaign" / n / "state.json").stat().st_size for n, h in src.campaign_homes().items()
        },
        "commands": rows,
        "profiles": profiles,
        "prototype": prototype(out, store),
        "state_json_unchanged": (home / ".campaign" / "xapiand" / "state.json").stat().st_mtime == mtime,
        "processes_on_the_copy_after": copy_workers(store),
    }
    (out / "toolcost.json").write_text(json.dumps(res, indent=1))
    if not res["processes_on_the_copy_after"]:
        shutil.rmtree(store)  # rebuilt on each pass; a copy left behind is a store nothing serves
    return res
