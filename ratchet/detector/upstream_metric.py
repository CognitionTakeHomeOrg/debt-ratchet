#!/usr/bin/env python3
"""Superset's own tech-debt dashboard.

`.github/workflows/tech-debt.yml` runs `npm run lint-stats` on a schedule and
uploads the results to a Google Sheet. That sheet is world-readable, so this is
not a reconstruction -- it is their data, exported.

It is the empirical case for this whole project. The argument that measurement
alone does not fix debt is usually made by assertion; here it can be made with
the target's own numbers, collected daily for nineteen months.

Two things the data shows, and the second matters more than the first:

  1. **The curve does not bend.** On a like-for-like basis -- the 13 rules present
     at both ends of the ESLint era -- the total went 533 -> 560 over 324 days.
     Up 5%, while being measured every single day.

  2. **None of the gates this system targets are on it.** `rules-of-hooks`,
     `jsx-key`, `no-unstable-nested-components` and `ban-ts-comment` are absent
     entirely. The dashboard tracks 6-13 rules; the debt in the enforcement
     switches is not among them.

⚠️ Do not quote the headline total across the 2025-10-30 boundary. Superset
migrated from ESLint to oxlint and the uploader's rule set changed with it, so
totals either side are not comparable. Any honest trend has to be computed on
rules present at both endpoints, which is what `like_for_like()` does.

    python upstream_metric.py            # from the cached CSV in baseline/
    python upstream_metric.py --refresh  # re-download first
"""

from __future__ import annotations

import argparse
import collections
import csv
import io
import urllib.request
from pathlib import Path

# From .github/workflows/tech-debt.yml in apache/superset.
SHEET_ID = "1oABNnzxJYzwUrHjr_c9wfYEq9dFL1ScVof9LlaAdxvo"
CSV_URL = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv"
CACHE = Path(__file__).resolve().parents[2] / "baseline" / "superset-dashboard" / "tech-debt-metrics.csv"

# The rules this system puts under a ratchet, to check against what Superset
# tracks. Kept as substrings because the uploader's naming changed with the
# oxlint migration.
OUR_GATES = [
    "rules-of-hooks",
    "jsx-key",
    "no-unstable-nested-components",
    "ban-ts-comment",
]


def load(refresh: bool = False) -> list[dict]:
    if refresh or not CACHE.exists():
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(CSV_URL, timeout=60) as r:
            CACHE.write_bytes(r.read())
    return list(csv.DictReader(io.StringIO(CACHE.read_text())))


def like_for_like(rows: list[dict], process: str = "ESLint") -> dict:
    """Trend across one measurement basis only.

    Restricted to a single `Process` and to rules present on both the first and
    last day of it. Comparing raw totals across the tooling migration would be
    comparing two different questions and calling the difference progress.
    """
    per_day: dict[str, dict[str, int]] = collections.defaultdict(dict)
    for r in rows:
        if r.get("Process") != process:
            continue
        try:
            per_day[r["Timestamp"][:10]][r["Rule"]] = int(r["Count"])
        except (ValueError, KeyError):
            continue
    days = sorted(per_day)
    if len(days) < 2:
        return {}
    first, last = per_day[days[0]], per_day[days[-1]]
    common = sorted(set(first) & set(last), key=lambda k: -last[k])
    return {
        "process": process,
        "start_date": days[0],
        "end_date": days[-1],
        "days": len(days),
        "rules": len(common),
        "start_total": sum(first[k] for k in common),
        "end_total": sum(last[k] for k in common),
        "per_rule": [(k, first[k], last[k]) for k in common],
    }


def coverage(rows: list[dict]) -> dict[str, bool]:
    tracked = {r.get("Rule", "") for r in rows}
    return {g: any(g in t for t in tracked) for g in OUR_GATES}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    rows = load(args.refresh)
    stats = like_for_like(rows)
    if not stats:
        print("not enough data")
        return 1

    delta = stats["end_total"] - stats["start_total"]
    pct = 100 * delta / stats["start_total"] if stats["start_total"] else 0

    print(f"Superset's own tech-debt dashboard  ({len(rows):,} rows)")
    print(f"  basis   {stats['process']}, {stats['rules']} rules present at both ends")
    print(f"  window  {stats['start_date']} -> {stats['end_date']}  ({stats['days']} days measured)\n")
    print(f"  {'rule':<52} {'start':>6} {'end':>6} {'change':>8}")
    for rule, a, b in stats["per_rule"]:
        print(f"  {rule[:52]:<52} {a:>6,} {b:>6,} {b - a:>+8,}")
    print("  " + "-" * 76)
    print(f"  {'TOTAL':<52} {stats['start_total']:>6,} {stats['end_total']:>6,} {delta:>+8,}   ({pct:+.0f}%)")
    print()
    print("  Measured every day for the whole window. The curve does not bend.\n")

    print("  Gates this system ratchets, as tracked by that dashboard:")
    for gate, present in coverage(rows).items():
        print(f"    {gate:<34} {'tracked' if present else 'NOT TRACKED'}")
    print()
    print("  The debt sitting behind the enforcement switches is not on the")
    print("  dashboard at all. It is counted in every CI run and reported nowhere.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
