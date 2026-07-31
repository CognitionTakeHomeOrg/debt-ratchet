#!/usr/bin/env python3
"""The event source.

Runs a gate, groups its findings into reviewable units, and files one GitHub
issue per unit. This is what makes the system event-driven: nobody decides what
Devin works on. A gate reports debt, the detector files issues, and issue
creation is the event the orchestrator reacts to.

Idempotent by fingerprint -- it is designed to run on a schedule, and a second
run must not double-file. Findings that are already fixed cause the issue to be
updated with the smaller set, not closed silently; closing is the orchestrator's
job after it independently verifies a merge.

Usage:
    python detect.py --workstream C --rule react/jsx-key [--apply]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from gates import assert_clean_tree, production_only, run_mypy, run_oxlint  # noqa: E402
from grouping import group  # noqa: E402
from render import render  # noqa: E402

WORKSTREAM_RULES = {
    "A": ("oxlint", "react-hooks/rules-of-hooks"),
    "B": ("oxlint", "react/no-unstable-nested-components"),
    "C": ("oxlint", "react/jsx-key"),
    "D": ("oxlint", "@typescript-eslint/ban-ts-comment"),
    "E": ("mypy", "unused-ignore"),
}

# Workstreams whose findings in test fixtures are usually legitimate rather than
# debt, and so are not filed. See gates.production_only.
PRODUCTION_ONLY = {"D"}

GATE_LABEL = {"oxlint": "gate:oxlint", "mypy": "gate:mypy", "zizmor": "gate:zizmor"}


def load_env(root: Path) -> dict:
    env = {}
    envfile = root / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return {**env, **os.environ}


def existing_issues(repo: str) -> dict[str, dict]:
    """Map fingerprint -> issue, for every open detector-filed issue."""
    out = subprocess.run(
        ["gh", "issue", "list", "--repo", repo, "--state", "open", "--limit", "200",
         "--json", "number,title,body,labels"],
        capture_output=True, text=True, check=True,
    ).stdout
    found = {}
    for issue in json.loads(out):
        body = issue.get("body") or ""
        marker = "<!-- ratchet-fingerprint: "
        if marker in body:
            fp = body.split(marker, 1)[1].split(" -->", 1)[0].strip()
            found[fp] = issue
    return found


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workstream", required=True, choices=sorted(WORKSTREAM_RULES))
    ap.add_argument("--repo-path", default="superset-adham-clone")
    ap.add_argument("--apply", action="store_true", help="actually create issues")
    ap.add_argument("--only-area", help="file only this area (used for the pilot run)")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[2]
    env = load_env(root)
    repo = env["FORK_REPO"]
    sha = env["BASELINE_SHA"]
    repo_path = (root / args.repo_path).resolve()

    gate, rule = WORKSTREAM_RULES[args.workstream]
    assert_clean_tree(repo_path)
    findings = run_mypy(repo_path) if gate == "mypy" else run_oxlint(repo_path, rule)
    if args.workstream in PRODUCTION_ONLY:
        before = len(findings)
        findings = production_only(findings)
        print(f"scope: {len(findings)} production findings ({before - len(findings)} "
              f"in tests/stories, not filed)")
    units = group(findings, args.workstream)

    print(f"gate={gate} rule={rule}  findings={len(findings)}  units={len(units)}")
    for u in units:
        print(f"  {u.ident:4} {u.area:45} {len(u.findings):3} findings  "
              f"{len(u.files):2} files  fp={u.fingerprint}")

    if args.only_area:
        units = [u for u in units if u.area == args.only_area]
        if not units:
            print(f"no unit matches area {args.only_area!r}", file=sys.stderr)
            return 1

    if not args.apply:
        print("\n(dry run -- pass --apply to file issues)")
        return 0

    known = existing_issues(repo)
    for u in units:
        title, body = render(u, repo, sha)
        labels = [GATE_LABEL[u.gate], "devin:queued"]
        if u.fingerprint in known:
            num = known[u.fingerprint]["number"]
            subprocess.run(
                ["gh", "issue", "edit", str(num), "--repo", repo,
                 "--title", title, "--body", body],
                check=True, capture_output=True,
            )
            print(f"updated #{num}  {u.ident} {u.area}")
        else:
            res = subprocess.run(
                ["gh", "issue", "create", "--repo", repo, "--title", title,
                 "--body", body, *sum((["--label", l] for l in labels), [])],
                check=True, capture_output=True, text=True,
            )
            print(f"created {res.stdout.strip()}  {u.ident} {u.area}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
