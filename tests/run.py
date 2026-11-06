#!/usr/bin/env python3
"""Runs the test suite in parallel: each test in its own process, several at once, then the tests
marked `alone` (they load the whole machine or time it) one at a time with nothing beside them.

  python3 run.py [-j JOBS] [--durations N] [TEST ...]

TEST narrows the run to ids starting with it (`test_quiet`, `test_jobs.Crashes`). Every test
isolates its own repositories, job queue and quiet directory, so the order does not matter;
`python3 -m unittest` runs the same tests one by one.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import unittest
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent


def cases(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def alone(test: unittest.TestCase) -> bool:
    method = getattr(test, test._testMethodName, None)
    return bool(getattr(type(test), "alone", False) or getattr(method, "alone", False))


def run(test_id: str) -> tuple[str, int, float, str]:
    start = time.time()
    p = subprocess.run(
        [sys.executable, "-m", "unittest", test_id],
        cwd=HERE,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return test_id, p.returncode, time.time() - start, p.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="run.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=min(4, max((os.cpu_count() or 2) // 2, 1)),
        help="tests at once (default 4, the most a shared machine allows a test suite; half the CPUs if fewer)",
    )
    ap.add_argument("--durations", type=int, default=0, metavar="N", help="list the N slowest tests")
    ap.add_argument("only", nargs="*", metavar="TEST", help="run only tests whose id starts with one of these")
    a = ap.parse_args(argv)
    sys.path.insert(0, str(HERE))
    loaded = unittest.TestLoader().discover(str(HERE), pattern="test_*.py", top_level_dir=str(HERE))
    tests = [t for t in cases(loaded) if not a.only or any(t.id().startswith(o) for o in a.only)]
    broken = [t.id() for t in tests if t.id().startswith("unittest.loader")]
    together = [t.id() for t in tests if not alone(t) and t.id() not in broken]
    apart = [t.id() for t in tests if alone(t)]
    start = time.time()
    results: list[tuple[str, int, float, str]] = []
    with ThreadPoolExecutor(max_workers=max(a.jobs, 1)) as pool:
        for result in pool.map(run, together):
            results.append(result)
            print("." if result[1] == 0 else "F", end="", flush=True)
    for test_id in apart:
        results.append(run(test_id))
        print("." if results[-1][1] == 0 else "F", end="", flush=True)
    print()
    failed = [r for r in results if r[1] != 0]
    for test_id, _, _, output in failed:
        print(f"\n{'=' * 70}\nFAIL: {test_id}\n{output.rstrip()}")
    for test_id in broken:
        print(f"\nERROR: could not load {test_id}")
    if a.durations:
        print("\nSlowest tests:")
        for test_id, _, seconds, _ in sorted(results, key=lambda r: -r[2])[: a.durations]:
            print(f"  {seconds:7.2f}s  {test_id}")
    skipped = sum(int(m) for _, _, _, out in results for m in re.findall(r"skipped=(\d+)", out))
    bad = len(failed) + len(broken)
    print(
        f"\nRan {len(results)} test{'' if len(results) == 1 else 's'} in {time.time() - start:.1f}s ({a.jobs} at once, {len(apart)} alone): "
        + (f"FAILED ({bad})" if bad else "OK")
        + (f", {skipped} skipped" if skipped else "")
    )
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
