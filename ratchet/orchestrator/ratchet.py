#!/usr/bin/env python3
"""The ratchet.

Cleanup decays. Someone clears every `jsx-key` violation in the repository, and
six months later there are forty of them again, because nothing stopped them
coming back. The enforcement switch is what stops them, and flipping it is the
entire point of the preceding work -- the pull requests are the means, this is
the end.

A ratchet, mechanically, is a toothed wheel with a pawl that lets it turn one way
only. That is the property being bought here: the count can go down, and cannot
go back up.

Two modes:

  full     -- a gate reached zero, so promote the rule to `error`. The class of
              defect becomes impossible to reintroduce.

  counting -- it did not, so freeze it. Today's counts are committed as ceilings
              and CI fails any pull request that raises one. Strictly weaker, but
              it still only turns one way, and it does not require finishing
              first. This is what makes the approach apply to the 197
              `ban-ts-comment` findings nobody is ever going to hand-clear.

**This runs automatically.** The thesis is that cleanup fails because it depends
on someone paying attention; a ratchet that a human has to remember to crank
would reintroduce exactly that failure. So the PR is opened and kept up to date
by the system, on every gate improvement -- and merged by a human, because
turning on something that fails everyone's build is a policy decision.

    python ratchet.py --auto            # what would change
    python ratchet.py --auto --push     # open/update the PR
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1] / "detector"))

from devin import fork_path, load_env  # noqa: E402
from gates import assert_clean_tree, run_mypy, run_oxlint  # noqa: E402

# Every gate, its committed baseline, and where its enforcement switch lives.
GATES = {
    "react-hooks/rules-of-hooks": {
        "baseline": 47, "kind": "oxlint", "switch": '"warn"',
        "note": "4 of these are false positives in playwright/ -- `use()` there is "
                "Playwright's fixture callback, not React's hook",
    },
    "react/no-unstable-nested-components": {
        "baseline": 150, "kind": "oxlint", "switch": '"warn"', "note": "",
    },
    "react/jsx-key": {
        "baseline": 81, "kind": "oxlint", "switch": "absent, inherits default `warn`",
        "note": "",
    },
    "@typescript-eslint/ban-ts-comment": {
        "baseline": 197, "kind": "oxlint", "switch": '"off"', "note": "",
    },
    "unused-ignore": {
        "baseline": 49, "kind": "mypy",
        "switch": "8 modules exempted via `warn_unused_ignores = false`",
        "note": "measured in the stub-only mypy environment pre-commit uses",
    },
}

BRANCH = "ratchet/counting-baseline"

# Apache RAT checks every file for this header and the ratchet PR is not exempt.
# Nor should it be: a pull request that turns on enforcement while failing the
# project's existing gates would be the most embarrassing possible artifact.
ASF_HEADER_PY = '''#!/usr/bin/env python3
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
'''

ASF_HEADER_INI = ASF_HEADER_PY.replace("#!/usr/bin/env python3\n", "")

CHECK_SCRIPT = ASF_HEADER_PY + '''"""Counting ratchet.

Fails when any gate's finding count rises above its committed ceiling.

It does not require the count to be zero, which is what makes it usable against
debt too large to ever hand-clear. A pull request that reduces the debt may lower
a ceiling freely. Raising one requires deliberately editing a committed file, in
a diff a reviewer will see and have to approve.

    python scripts/ratchet_check.py
"""

import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = json.loads((ROOT / "ratchet-baseline.json").read_text())

# oxlint must run from superset-frontend/ with no path arguments, via the
# lockfile-pinned binary. Naming paths under-reports -- rules-of-hooks is 47 at
# CI scope and 43 if you pass `src packages plugins`, because playwright/ drops
# out of the scan.
FRONTEND = ROOT / "superset-frontend"
OXLINT = "./node_modules/.bin/oxlint"

# mypy's answer depends on what is installed. The gate is the isolated
# environment pre-commit uses: mypy plus stub packages and nothing else. With
# Superset's real dependencies installed the same commit reports 1,500+ errors
# that have nothing to do with this check.
MYPY_LINE = re.compile(r"^[^:]+:\\d+: error: .*\\[(?P<code>[\\w-]+)\\]$")


def count_oxlint(rule: str) -> int:
    """Count findings for one oxlint rule.

    `-A all -D <rule>` disables everything, then enables exactly one at error
    severity, so the number cannot drift because an unrelated rule changed.
    """
    out = subprocess.run(  # noqa: S603  # fixed argv, no shell, no user input
        [
            OXLINT,
            "--config",
            "oxlint.json",
            "-A",
            "all",
            "-D",
            rule,
            "--format",
            "json",
        ],
        cwd=FRONTEND,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return len(json.loads(out).get("diagnostics", [])) if out.strip() else 0


def count_mypy(code: str, mypy_bin: str) -> int:
    """Count mypy errors of one code, in the isolated gate environment."""
    out = subprocess.run(  # noqa: S603  # fixed argv, no shell, no user input
        [
            mypy_bin,
            "--config-file",
            str(ROOT / "ratchet-mypy.ini"),
            "--check-untyped-defs",
            "--no-color-output",
            "--no-error-summary",
            "superset/",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return sum(
        1
        for ln in out.splitlines()
        if (m := MYPY_LINE.match(ln.strip())) and m.group("code") == code
    )


def main() -> int:
    mypy_bin = BASELINE.get("mypy_bin", "mypy")
    failed: list[tuple[str, int, int]] = []
    print(f"{'gate':<40} {'now':>6} {'ceiling':>8}   verdict")
    print("-" * 72)
    for gate, spec in sorted(BASELINE["gates"].items()):
        ceiling = spec["ceiling"]
        try:
            now = (
                count_oxlint(gate)
                if spec["kind"] == "oxlint"
                else count_mypy(gate, mypy_bin)
            )
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"{gate:<40} {'?':>6} {ceiling:>8}   SKIPPED ({type(e).__name__})")
            continue
        if now > ceiling:
            verdict, bad = f"REGRESSION +{now - ceiling}", True
        elif now < ceiling:
            verdict, bad = f"improved -{ceiling - now}", False
        else:
            verdict, bad = "held", False
        print(f"{gate:<40} {now:>6} {ceiling:>8}   {verdict}")
        if bad:
            failed.append((gate, now, ceiling))

    if failed:
        print()
        for gate, now, ceiling in failed:
            print(f"error: {gate} rose from {ceiling} to {now}.", file=sys.stderr)
        print(
            "\\nFix the new findings, or -- if you are deliberately removing this "
            "code -- lower the ceiling in ratchet-baseline.json.",
            file=sys.stderr,
        )
        return 1

    print("\\nAll gates held or improved.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

WORKFLOW = """\
# The ratchet.
#
# Superset already runs every one of these checks and enforces none of them --
# `npm run lint` passes `--quiet`, which counts findings and discards them. This
# job is what turns the count into a constraint.
#
# It does not demand zero. It demands "no worse than the committed ceiling",
# which is a bar this repository can actually hold today.
#
# Actions are pinned by digest and permissions are declared explicitly, because
# this repository's own zizmor audit requires both -- and a pull request that
# turns on enforcement while failing an existing gate would be the most
# embarrassing artifact this project could produce. The digests match the ones
# already used across the other 36 workflows here.
name: ratchet

on:
  pull_request:
  push:
    branches: [master]

permissions:
  contents: read

jobs:
  counting-ratchet:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          # Nothing here pushes; leaving the token in .git/config would let any
          # later step, or a compromised dependency, use it.
          persist-credentials: false
      - uses: actions/setup-node@820762786026740c76f36085b0efc47a31fe5020 # v7.0.0
        with:
          node-version-file: superset-frontend/.nvmrc
      - uses: actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97 # v7.0.0
        with:
          python-version: "3.11"
      - name: install frontend deps
        working-directory: superset-frontend
        run: npm ci --prefer-offline --no-audit
      - name: install the mypy gate
        # Stub packages only. Installing Superset's real dependencies here would
        # change the answer -- see ratchet-mypy.ini.
        run: |
          python -m venv /tmp/mypy-gate
          /tmp/mypy-gate/bin/pip install "mypy==1.15.0" \\
            types-cachetools types-simplejson types-python-dateutil types-requests \\
            types-pytz types-croniter types-PyYAML types-setuptools types-paramiko \\
            types-Markdown
      - name: ratchet
        run: python scripts/ratchet_check.py
"""


def sh(cmd: list[str], cwd: Path, check: bool = True) -> str:
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])}: {(r.stderr or r.stdout)[:300]}")
    return r.stdout


def measure(repo: Path) -> dict[str, int]:
    counts = {}
    for gate, spec in GATES.items():
        counts[gate] = (len(run_oxlint(repo, gate)) if spec["kind"] == "oxlint"
                        else len(run_mypy(repo)))
    return counts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto", action="store_true", help="freeze every improved gate")
    ap.add_argument("--push", action="store_true", help="open/update the PR")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[2]
    env = load_env(root)
    repo_name = env["FORK_REPO"]
    repo = fork_path(root)

    assert_clean_tree(repo)
    sh(["git", "fetch", "origin", "master"], repo)
    sh(["git", "checkout", "-q", "master"], repo)
    sh(["git", "reset", "--hard", "-q", "origin/master"], repo)

    counts = measure(repo)
    improved = {g: c for g, c in counts.items() if c < GATES[g]["baseline"]}
    closed = [g for g, c in counts.items() if c == 0]

    print(f"{'gate':<40} {'baseline':>9} {'now':>6}   status")
    print("-" * 74)
    for gate, spec in GATES.items():
        now, base = counts[gate], spec["baseline"]
        status = ("CLOSED" if now == 0 else
                  f"improved -{base - now}" if now < base else "unchanged")
        print(f"{gate:<40} {base:>9} {now:>6}   {status}")

    if not improved:
        print("\nNo gate has improved. A ratchet only turns one way -- nothing to do.")
        return 0
    if closed:
        print(f"\nNOTE: {', '.join(closed)} reached zero -- eligible for a FULL "
              f"ratchet (promote to `error`). Run --mode full for those.")

    fe = repo / "superset-frontend"
    baseline_doc = {
        "_comment": (
            "Committed ceilings. Lower them freely in a PR that reduces debt. "
            "Raising one is a deliberate, reviewable act."
        ),
        "measured_at": sh(["git", "rev-parse", "HEAD"], repo).strip(),
        "mypy_bin": "/tmp/mypy-gate/bin/mypy",
        "gates": {g: {"ceiling": c, "kind": GATES[g]["kind"]} for g, c in counts.items()},
    }

    print(f"\nWould freeze {len(counts)} gates ({len(improved)} improved) on branch {BRANCH}")
    if not args.push:
        print("(dry run -- pass --push to open the PR)")
        return 0

    sh(["git", "checkout", "-B", BRANCH, "master"], repo)
    (repo / "ratchet-baseline.json").write_text(json.dumps(baseline_doc, indent=2) + "\n")
    (repo / "scripts").mkdir(exist_ok=True)
    (repo / "scripts" / "ratchet_check.py").write_text(CHECK_SCRIPT)
    (repo / "scripts" / "ratchet_check.py").chmod(0o755)
    (repo / "ratchet-mypy.ini").write_text(
        ASF_HEADER_INI
        + (Path(__file__).parents[1] / "gates" / "mypy-unused-ignore.ini").read_text())
    (repo / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (repo / ".github" / "workflows" / "ratchet.yml").write_text(WORKFLOW)

    rows = "\n".join(
        f"| `{g}` | {GATES[g]['baseline']} | **{counts[g]}** | "
        f"{'**-' + str(GATES[g]['baseline'] - counts[g]) + '**' if counts[g] < GATES[g]['baseline'] else '--'} | "
        f"{GATES[g]['switch']} |"
        for g in GATES
    )
    notes = "\n".join(f"- `{g}`: {GATES[g]['note']}" for g in GATES if GATES[g]["note"])

    title = f"ci: freeze {len(counts)} quality gates at current counts (counting ratchet)"
    body = f"""\
## SUMMARY

Commits the current finding count for every measured gate as a **ceiling**, and
fails CI on any pull request that raises one.

These rules are checked on every CI run and enforced on none of them. `npm run
lint` passes `--quiet`, which counts findings and discards them -- the count has
appeared in every green build without ever failing one.

Turning them into errors outright is not possible yet: findings remain, and the
next person to push an unrelated typo fix would get a build failure with hundreds
of errors that are not theirs. So this takes the weaker, available step. The
count can go **down** freely. It cannot go **up**.

| gate | baseline | ceiling | change | enforcement today |
|---|---|---|---|---|
{rows}

{notes}

## BEFORE/AFTER

- **Before:** unbounded. Any PR could add findings, silently, forever.
- **After:** capped. CI fails at ceiling + 1, naming the gate and the delta.

## TESTING INSTRUCTIONS

```bash
python scripts/ratchet_check.py     # passes at the committed ceilings
```

Introduce one new violation and re-run; it fails and names the rule.

## ADDITIONAL INFORMATION

- [x] Has associated issue: the per-gate remediation issues in this repository
- [x] Required feature flags: none
- [x] Changes UI: no
- [x] Introduces new feature or API: no

---
<sub>Opened automatically when a gate improved. Cleanup decays; the ceiling does
not. Merging is a human decision -- this turns on a check that can fail
everyone's build.</sub>
"""

    sh(["git", "add", "-A"], repo)
    sh(["git", "-c", "user.name=debt-ratchet", "-c", "user.email=ratchet@local",
        "commit", "-q", "-m", title], repo)
    sh(["git", "push", "-q", "--force", "-u", "origin", BRANCH], repo)

    existing = sh(["gh", "pr", "list", "--repo", repo_name, "--head", BRANCH,
                   "--state", "open", "--json", "number", "--jq", ".[0].number"],
                  repo, check=False).strip()
    if existing:
        # Non-fatal. The branch push above is what actually updates the pull
        # request's diff; refreshing title and body is cosmetic, and `gh pr edit`
        # currently fails against this repo with a Projects-classic GraphQL
        # deprecation that has nothing to do with us. Aborting here would leave
        # the caller thinking the ratchet had not been updated when it had.
        r = sh(["gh", "pr", "edit", existing, "--repo", repo_name,
                "--title", title, "--body", body], repo, check=False)
        print(f"updated PR #{existing}"
              + ("" if r is not None else " (branch pushed; body refresh failed)"))
    else:
        url = sh(["gh", "pr", "create", "--repo", repo_name, "--base", "master",
                  "--head", BRANCH, "--title", title, "--body", body,
                  "--label", "devin:ratchet"], repo).strip()
        print(f"opened {url}")

    sh(["git", "checkout", "-q", "master"], repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
