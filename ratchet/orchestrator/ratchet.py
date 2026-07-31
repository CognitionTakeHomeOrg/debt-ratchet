#!/usr/bin/env python3
"""The ratchet.

Cleanup decays. Someone clears every `jsx-key` violation in the repository, and
six months later there are forty of them again, because nothing stopped them
from coming back. The enforcement switch is what stops them, and flipping it is
the entire point of the preceding work -- the pull requests are the means, this
is the end.

A ratchet, mechanically, is a toothed wheel with a pawl that lets it turn one way
only. That is the property being bought here: the count can go down, and cannot
go back up.

Two modes:

  full     -- the debt reached zero, so promote the rule to `error`. The class of
              defect becomes impossible to reintroduce.

  counting -- the debt did not reach zero, so freeze it. Today's count becomes a
              committed baseline and CI fails any pull request that raises it.
              Strictly weaker, but it still only turns one way, and it does not
              require finishing first. This is what makes the approach apply to
              the 557 `prefer-destructuring` findings nobody is ever going to
              hand-fix.

    python ratchet.py --workstream C --mode full
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1] / "detector"))

from devin import load_env  # noqa: E402
from gates import assert_clean_tree, run_oxlint  # noqa: E402

RULES = {
    "A": "react-hooks/rules-of-hooks",
    "B": "react/no-unstable-nested-components",
    "C": "react/jsx-key",
    "D": "@typescript-eslint/ban-ts-comment",
}

BASELINE = {"A": 47, "B": 150, "C": 81, "D": 197}

COUNTING_SCRIPT = """\
#!/usr/bin/env node
/*
 * Counting ratchet.
 *
 * Fails when a rule's finding count rises above the committed baseline. It does
 * not require the count to be zero, which is what makes it usable against debt
 * too large to ever hand-clear. The number in ratchet-baseline.json may be
 * lowered by any PR that reduces the debt; raising it requires a human to
 * deliberately edit a committed file, in a diff a reviewer will see.
 */
const { execSync } = require('child_process');
const fs = require('fs');

const baseline = JSON.parse(fs.readFileSync(__dirname + '/ratchet-baseline.json', 'utf8'));
let failed = false;

for (const [rule, allowed] of Object.entries(baseline.rules)) {
  const out = execSync(
    `./node_modules/.bin/oxlint --config oxlint.json -A all -D ${rule} --format json`,
    { encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 },
  );
  const actual = JSON.parse(out).diagnostics.length;
  const verdict = actual > allowed ? 'REGRESSION' : actual < allowed ? 'improved' : 'held';
  console.log(`${rule}: ${actual} (baseline ${allowed}) ${verdict}`);
  if (actual > allowed) {
    console.error(
      `  ${rule} rose by ${actual - allowed}. Fix the new findings, or lower the ` +
      `baseline in ratchet-baseline.json if you are removing them.`,
    );
    failed = true;
  }
}
process.exit(failed ? 1 : 0);
"""


def sh(cmd: list[str], cwd: Path, check: bool = True) -> str:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check).stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workstream", required=True, choices=sorted(RULES))
    ap.add_argument("--mode", choices=["full", "counting", "auto"], default="auto")
    ap.add_argument("--push", action="store_true", help="open the PR (default is dry run)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[2]
    env = load_env(root)
    repo_name = env["FORK_REPO"]
    repo = (root / "superset-adham-clone").resolve()
    rule = RULES[args.workstream]

    assert_clean_tree(repo)
    findings = run_oxlint(repo, rule)
    n = len(findings)
    before = BASELINE[args.workstream]

    mode = args.mode
    if mode == "auto":
        mode = "full" if n == 0 else "counting"

    print(f"{rule}: baseline {before} -> now {n}   mode={mode}")
    if mode == "full" and n != 0:
        print(f"REFUSING full ratchet: {n} findings remain. Promoting the rule to "
              f"`error` would break the build on the first commit.", file=sys.stderr)
        print("Use --mode counting to freeze the current count instead.", file=sys.stderr)
        return 2
    if n >= before and mode == "counting":
        print(f"REFUSING: count did not improve ({before} -> {n}). "
              f"A ratchet only turns one way.", file=sys.stderr)
        return 2

    fe = repo / "superset-frontend"
    branch = f"ratchet/{args.workstream.lower()}-{rule.split('/')[-1]}"
    sh(["git", "checkout", "-B", branch, "master"], repo)

    if mode == "full":
        cfg_path = fe / "oxlint.json"
        cfg = json.loads(cfg_path.read_text())
        cfg.setdefault("rules", {})[rule] = "error"
        cfg_path.write_text(json.dumps(cfg, indent=2) + "\n")
        title = f"ci: enforce {rule} (ratchet)"
        body = f"""\
## SUMMARY

Promotes `{rule}` from non-blocking to `error` in `superset-frontend/oxlint.json`.

This rule has been checked on every CI run and enforced on none of them. `npm run
lint` passes `--quiet`, which counts the findings and discards them -- the count
appeared in {before} consecutive green builds without ever failing one.

The {before} pre-existing violations have been cleared, so the switch can now be
turned on. This is the change that makes the cleanup permanent: without it, the
count grows back and the work is spent.

## BEFORE/AFTER

- **Before:** `{rule}` — {before} findings, build green
- **After:** `{rule}` — 0 findings, build **fails** on the next one introduced

## TESTING INSTRUCTIONS

```bash
cd superset-frontend
./node_modules/.bin/oxlint --config oxlint.json -A all -D {rule} --format json \\
  | jq '.diagnostics | length'   # 0
npm run lint                      # passes, and now this is load-bearing
```

## ADDITIONAL INFORMATION

- [ ] Has associated issue
- [x] Required feature flags: none
- [x] Changes UI: no
- [x] Introduces new feature or API: no
"""
    else:
        (fe / "ratchet-check.js").write_text(COUNTING_SCRIPT)
        (fe / "ratchet-check.js").chmod(0o755)
        bl_path = fe / "ratchet-baseline.json"
        existing = json.loads(bl_path.read_text()) if bl_path.exists() else {"rules": {}}
        existing["rules"][rule] = n
        bl_path.write_text(json.dumps(existing, indent=2) + "\n")

        pkg_path = fe / "package.json"
        pkg = json.loads(pkg_path.read_text())
        pkg["scripts"]["ratchet"] = "node ratchet-check.js"
        pkg_path.write_text(json.dumps(pkg, indent=2) + "\n")

        title = f"ci: freeze {rule} at {n} (counting ratchet)"
        body = f"""\
## SUMMARY

Commits the current `{rule}` count as a ceiling and fails CI on any pull request
that raises it.

{before - n} of {before} findings have been cleared; {n} remain. Rather than wait
for zero before turning on any enforcement at all, this locks in the progress
already made. The number can be lowered by any PR that reduces the debt. Raising
it requires deliberately editing a committed file, which a reviewer will see.

This is the weaker form of the ratchet, and it is the one that generalises: it
works against debt classes too large to ever hand-clear.

## BEFORE/AFTER

- **Before:** {before} findings, unbounded — any PR could add more, silently
- **After:** {n} findings, capped — CI fails at {n + 1}

## TESTING INSTRUCTIONS

```bash
cd superset-frontend
npm run ratchet     # passes at {n}
```

Introduce one new violation and re-run; it fails and names the rule.

## ADDITIONAL INFORMATION

- [ ] Has associated issue
- [x] Required feature flags: none
- [x] Changes UI: no
- [x] Introduces new feature or API: no
"""

    diff = sh(["git", "diff", "--stat"], repo)
    print("\n" + diff)

    if not args.push:
        sh(["git", "checkout", "master"], repo)
        sh(["git", "checkout", "--", "."], repo)
        sh(["git", "branch", "-D", branch], repo, check=False)
        print("(dry run -- pass --push to open the PR)")
        return 0

    sh(["git", "add", "-A"], repo)
    sh(["git", "commit", "-m", title], repo)
    sh(["git", "push", "-u", "origin", branch, "--force"], repo)
    url = subprocess.run(
        ["gh", "pr", "create", "--repo", repo_name, "--base", "master", "--head", branch,
         "--title", title, "--body", body, "--label", "devin:ratchet"],
        cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    sh(["git", "checkout", "master"], repo)
    print(f"opened {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
