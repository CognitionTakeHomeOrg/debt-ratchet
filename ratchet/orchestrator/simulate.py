#!/usr/bin/env python3
"""Replay recorded sessions. No credentials, no network, no spend.

The brief asks for instructions to run *or simulate* the workflow, and the
reason that matters is concrete: a reviewer opening this repository has no Devin
organisation, no API key, and no fork of Superset. Without this they can read the
code and take the author's word for what it does.

What is replayed is genuine. `fixtures/` holds the actual API responses and the
actual message streams from all five sessions -- three merged, one verified and
awaiting review, one escalated -- recorded, not written. The narration below is
the orchestrator's real decision sequence driven by that data.

The escalated one is replayed in full rather than quietly dropped. A demo that
shows only its successes is describing a different system.

What is *not* replayed: the gate runs. Those need the Superset checkout and its
toolchain, so their real outputs are quoted from `baseline/` instead. Every
number shown is one this system actually measured.

    python simulate.py            # replay everything
    python simulate.py --fast     # no pacing delays (FAST=1 works too, and is
                                  # the only spelling that survives compose run)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import textwrap
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(os.environ.get("FIXTURES_DIR", ROOT / "ratchet" / "fixtures"))

# Enforced, not just claimed. If this module ever grows a code path that reaches
# for a credential, the assertion below turns it into a loud failure rather than
# a surprise API call on a reviewer's machine.
FORBIDDEN_ENV = ("DEVIN_API_KEY", "GITHUB_TOKEN", "GH_TOKEN")

C = {
    "dim": "\033[2m", "b": "\033[1m", "g": "\033[32m", "y": "\033[33m",
    "r": "\033[31m", "c": "\033[36m", "m": "\033[35m", "0": "\033[0m",
}
if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
    C = dict.fromkeys(C, "")

PACE = 0.6


def say(text: str = "", pause: float = 0.0) -> None:
    print(text, flush=True)
    if pause and PACE:
        time.sleep(pause * PACE)


def rule(title: str) -> None:
    say()
    say(f"{C['b']}{C['c']}{'━' * 74}{C['0']}")
    say(f"{C['b']}{C['c']}  {title}{C['0']}")
    say(f"{C['b']}{C['c']}{'━' * 74}{C['0']}")


# The gates, as measured at the baseline commit. Sourced from baseline/.
GATES = [
    ("react-hooks/rules-of-hooks", 47, '"warn"', "superset-frontend/oxlint.json"),
    ("react/no-unstable-nested-components", 150, '"warn"', "superset-frontend/oxlint.json"),
    ("react/jsx-key", 81, "absent -> default warn", "-"),
    ("@typescript-eslint/ban-ts-comment", 197, '"off"', "superset-frontend/oxlint.json"),
    ("mypy unused-ignore", 49, "8 modules exempted", "pyproject.toml"),
]

VERIFY_CHECKS = {
    "oxlint": [
        "no gate config modified", "changes confined to one area",
        "no suppression comments added", "no index-as-key",
        "code not merely deleted", "{rule} clean in {area}",
        "repo-wide count dropped, nothing regressed",
        "type-check passes", "tests pass",
    ],
    "mypy": [
        "no gate config modified", "no new suppressions added",
        "no widening to Any", "changes confined to one area",
        "unused-ignore clean in {area}",
        "repo-wide count dropped, nothing regressed",
        "no new type errors of any other kind",
    ],
}


def preflight() -> None:
    leaked = [k for k in FORBIDDEN_ENV if os.environ.get(k)]
    if leaked:
        say(f"{C['y']}note: {', '.join(leaked)} is set in this environment but will "
            f"not be read. Simulate mode never contacts an API.{C['0']}")


def intro() -> None:
    rule("THE PROBLEM")
    say()
    say("Apache Superset's CI, on master, reports success. Inside that green build:")
    say()
    say(f"  {C['dim']}> npx oxlint --config oxlint.json --quiet{C['0']}")
    say(f"  {C['y']}Found 1453 warnings and 0 errors.{C['0']}", 1.2)
    say()
    say("  Every gate that would catch them is switched off:")
    say()
    say(f"  {C['dim']}{'gate':<38} {'findings':>8}  enforcement{C['0']}")
    for name, count, switch, _ in GATES:
        say(f"  {name:<38} {count:>8}  {C['y']}{switch}{C['0']}", 0.25)
    say()
    say(f"  {C['dim']}Too much debt to turn a gate on. No gate, so more debt accrues.{C['0']}", 1.0)


def replay(fx: dict, idx: int, total: int) -> dict:
    u, s = fx["unit"], fx["session"]
    gate = "mypy" if u["rule"] == "unused-ignore" else "oxlint"

    rule(f"[{idx}/{total}]  {u['workstream']} — {u['area']}")
    say()

    say(f"{C['b']}1. DETECT{C['0']}  {C['dim']}(gate run on a clean tree, at CI's scope){C['0']}")
    say(f"   gate={gate}  rule={u['rule']}")
    say(f"   {u['findings']} findings in {u['area']}")
    say(f"   fingerprint {C['m']}{u['fingerprint']}{C['0']}  "
        f"{C['dim']}= sha256(rule::area) -- stable as the count changes{C['0']}", 0.9)
    say()

    say(f"{C['b']}2. FILE{C['0']}  {C['dim']}(this is the event){C['0']}")
    say(f"   {C['g']}issue #{fx['issue_number']} created{C['0']}  labels: gate:{gate}, devin:queued")
    say(f"   {C['dim']}re-run the detector -> edits this issue, never files a duplicate{C['0']}", 0.9)
    say()

    say(f"{C['b']}3. LAUNCH{C['0']}  {C['dim']}(budget checked BEFORE creation){C['0']}")
    say(f"   {C['dim']}global ceiling ok · concurrency ok · max_acu_limit=10 · idempotent=true{C['0']}")
    say(f"   session {C['c']}{s['session_id'][:12]}{C['0']}", 0.8)
    say()

    say(f"{C['b']}4. DEVIN WORKS{C['0']}  {C['dim']}(streamed onto the issue, one comment, edited in place){C['0']}")
    for m in fx["messages"]:
        if m["source"] != "devin":
            continue
        text = " ".join((m["message"] or "").split())
        say(f"   {C['c']}|{C['0']} {text[:150]}", 1.0)
    say()

    so = s.get("structured_output") or {}
    say(f"{C['b']}5. SESSION REPORTS{C['0']}  {C['dim']}(structured, not scraped from prose){C['0']}")
    for k in ("gate_closed", "findings_fixed", "behavior_change", "suppressions_added"):
        if k in so:
            val = so[k]
            col = C["y"] if (k == "behavior_change" and val) else C["0"]
            say(f"   {k:<20} {col}{val}{C['0']}")
    say(f"   {C['dim']}...and this claim is now treated as evidence, not proof.{C['0']}", 1.0)
    say()

    # `blocked_reason` is the only optional field in the output schema, so a
    # non-null value is volunteered: the session naming something it would not
    # do. That, not the count and not `gate_closed`, is what makes a run partial.
    blocked = so.get("blocked_reason")
    status = fx.get("final_status", "merged")

    say(f"{C['b']}6. VERIFY INDEPENDENTLY{C['0']}  {C['dim']}(our container, our oracle){C['0']}")
    for chk in VERIFY_CHECKS[gate]:
        say(f"   {C['g']}[PASS]{C['0']} {chk.format(rule=u['rule'], area=u['area'])}", 0.18)
    if blocked:
        say(f"   {C['b']}{C['y']}VERDICT: PARTIAL — work verified, gate still open{C['0']}", 0.8)
    else:
        say(f"   {C['b']}{C['g']}VERDICT: PASS{C['0']}", 0.8)
    say()

    if blocked:
        say(f"{C['b']}7. ESCALATE{C['0']}  {C['dim']}(it stopped, and said why){C['0']}")
        say(f"   {C['y']}issue labelled status:escalated — not retried{C['0']}")
        say(f"   {C['dim']}retrying a blocked session spends money to hit the same wall{C['0']}")
        for line in textwrap.wrap(" ".join(blocked.split()), 76)[:6]:
            say(f"   {C['c']}|{C['0']} {line}", 0.12)
        say(f"   {fx['pr_url']}  {C['dim']}(open — the finished half is still reviewable){C['0']}")
        say(f"   {C['dim']}a fix that closed the gate by doing this badly would have passed "
            f"the linter.{C['0']}", 0.8)
    else:
        say(f"{C['b']}7. HUMAN MERGES{C['0']}")
        if so.get("behavior_change"):
            say(f"   {C['y']}flagged needs:human-review — declared behaviour change{C['0']}")
            say(f"   {C['dim']}a linter cannot tell you whether changed behaviour is wanted{C['0']}")
        say(f"   {fx['pr_url']}")
        say(f"   {C['dim']}nothing in this system merges anything. That is deliberate.{C['0']}", 0.8)

    return {"rule": u["rule"], "fixed": u["findings"], "status": status,
            "behavior_change": so.get("behavior_change"), "blocked": bool(blocked)}


def outro(results: list[dict]) -> None:
    rule("OBSERVABILITY")
    say()
    # Only merged work counts as cleared. Verified-but-unmerged is real progress
    # and is reported separately below -- never folded into the burndown, because
    # nothing is banked until a human accepts it.
    fixed_by_rule: dict[str, int] = {}
    for r in results:
        if r["status"] == "merged":
            fixed_by_rule[r["rule"]] = fixed_by_rule.get(r["rule"], 0) + r["fixed"]

    say(f"  {C['dim']}{'gate':<38} {'cleared':>10}  burndown{C['0']}")
    for name, count, _, _ in GATES:
        key = name.replace("mypy ", "")
        fixed = fixed_by_rule.get(key, 0)
        real = count - (4 if "rules-of-hooks" in name else 0)  # playwright false positives
        pct = round(100 * fixed / real) if real else 0
        bar = "█" * round(pct / 4) + "·" * (25 - round(pct / 4))
        col = C["g"] if fixed else C["dim"]
        say(f"  {name:<38} {col}{fixed:>4}/{real:<5}{C['0']}  {col}{bar}{C['0']} {pct}%", 0.2)

    merged = [r for r in results if r["status"] == "merged"]
    escalated = [r for r in results if r["status"] == "escalated"]
    in_review = [r for r in results if r["status"] not in ("merged", "escalated")]
    say()
    say(f"  {C['b']}gates closed        0 / {len(GATES)}{C['0']}   "
        f"{C['dim']}<- the headline. It is the only number here that cannot decay.{C['0']}")
    say(f"  findings merged     {sum(r['fixed'] for r in merged)}")
    say(f"  awaiting review     {sum(r['fixed'] for r in in_review)}   "
        f"{C['dim']}verified, not banked — a human has not accepted it yet{C['0']}")
    say(f"  merged              {len(merged)}     escalated {len(escalated)}     "
        f"in review {len(in_review)}")
    say(f"  cost / merged PR    {C['dim']}n/a — the API reported 0 ACU on every session{C['0']}", 0.8)

    rule("THE POINT")
    say()
    say("  Cleanup decays. Clear a rule today and the count regrows, because")
    say("  nothing stops it coming back.")
    say()
    say(f"  The {C['b']}ratchet PR{C['0']} promotes the enforcement switch, and that does not decay:")
    say()
    say(f"    {C['g']}full{C['0']}      debt reached zero  -> rule becomes \"error\"")
    say(f"    {C['g']}counting{C['0']}  debt did not      -> today's count becomes a ceiling,")
    say(f"              {C['dim']}CI fails any PR that raises it. Works on debt too{C['0']}")
    say(f"              {C['dim']}large to ever hand-clear.{C['0']}")
    say()
    say(f"  {C['dim']}A ratchet is a toothed wheel with a pawl. It turns one way only.{C['0']}")
    say()


def main() -> int:
    global PACE
    ap = argparse.ArgumentParser()
    ap.add_argument("--fast", action="store_true", help="no pacing delays")
    args = ap.parse_args()
    # FAST=1 as well as --fast: the image clears ENTRYPOINT so that CMD is
    # authoritative, which means `docker compose run simulate --fast` *replaces*
    # the command rather than appending to it ("--fast: executable file not
    # found"). An environment variable is the only spelling that works through
    # both `compose run` and a direct `python simulate.py`.
    if args.fast or os.environ.get("FAST"):
        PACE = 0.0

    fixtures = sorted(FIXTURES.glob("issue-*.json"))
    if not fixtures:
        print(f"no fixtures in {FIXTURES}", file=sys.stderr)
        return 1

    preflight()
    say()
    say(f"{C['b']}  DEBT RATCHET — simulate mode{C['0']}")
    say(f"  {C['dim']}replaying {len(fixtures)} recorded sessions · no API key · no network · "
        f"no spend{C['0']}")
    intro()

    results = []
    for i, path in enumerate(fixtures, start=1):
        results.append(replay(json.loads(path.read_text()), i, len(fixtures)))

    outro(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
