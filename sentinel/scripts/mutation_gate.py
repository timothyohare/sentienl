#!/usr/bin/env python3
"""Mutation-testing gate for Sentinel's collector/dispatcher/core logic.

Runs mutmut against `sentinel/`, exports its CI/CD stats, and fails (exit 1)
if the mutation score drops below MIN_SCORE. Untested code (mutmut's
"no tests" status) counts against the score deliberately — a survivor with
no test touching it at all is the same risk as one with a weak test.

Bound as `mutation` in .claude/harness.json for
`code-build-harness/harness/gates/mutation.mjs`. That gate only understands
Stryker's JSON report shape for its (optional) survivor-detail enrichment;
this script's own pass/fail exit code is what actually gates.

MIN_SCORE is 0.0: this repo had no mutation testing before this gate was
wired in (2026-08-03). First baseline run: 27.5% (killed=1433,
survived=1856, no_tests=1919, total=5208) — left as a no-op floor
deliberately, not raised to ~27. Raising MIN_SCORE is a real policy
decision — how much of the 1919 no-coverage mutants is acceptable, which
modules matter most — that deserves an actual look at `mutmut results`
first, not a threshold picked
to match whatever the first run happened to produce. Mirrors rotrade's
scripts/mutation_gate.py in structure; do not lower a threshold once set,
to make a later regression pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

MIN_SCORE = 0.0
STATS_PATH = Path("mutants/mutmut-cicd-stats.json")


def main() -> int:
    subprocess.run([sys.executable, "-m", "mutmut", "run"], check=False)
    subprocess.run([sys.executable, "-m", "mutmut", "export-cicd-stats"], check=False)

    if not STATS_PATH.exists():
        print(f"gate-mutation: {STATS_PATH} not found — did mutmut run?", file=sys.stderr)
        return 1

    stats = json.loads(STATS_PATH.read_text())
    killed = stats.get("killed", 0)
    survived = stats.get("survived", 0)
    no_tests = stats.get("no_tests", 0)
    total = killed + survived + no_tests
    score = (killed / total * 100) if total else 0.0

    print(f"mutation score: {score:.1f}% (killed={killed}, survived={survived}, "
          f"no_tests={no_tests}, total={total})")

    if score < MIN_SCORE:
        print(f"gate-mutation: {score:.1f}% < required {MIN_SCORE}% — "
              f"strengthen tests for surviving/untested mutants (see `mutmut results`, "
              f"`mutmut show <id>`). Do not lower MIN_SCORE to pass.", file=sys.stderr)
        return 1

    print(f"gate-mutation: {score:.1f}% >= required {MIN_SCORE}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
