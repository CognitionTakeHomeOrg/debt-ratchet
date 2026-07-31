#!/usr/bin/env python3
"""Observability.

The question this has to answer is the one the brief asks literally: *if I were
an engineering leader, how would I know this is working?*

The honest answer is **not** "how many sessions succeeded". Sessions are activity,
and activity is what a dashboard shows when nobody has decided what the outcome
is. Four numbers, in descending order of how much they actually mean:

  1. **Gates closed.** The outcome. A closed gate is a defect class that cannot
     come back. Nothing else on this page survives contact with time -- cleanup
     decays, enforcement doesn't.

  2. **Burndown per gate.** Progress toward (1). Superset already tracks this in
     a Google Sheet; the curve has never bent. This is the same measurement with
     something attached to it that moves the curve.

  3. **Cost per merged PR.** The number that decides whether this gets adopted.
     Deliberately computed against *merged* PRs -- not opened, not attempted --
     because spend on work that was thrown away is still spend.

  4. **Escalation rate.** Reported next to success rather than buried. A system
     that never escalates is not more trustworthy than one that does; it is
     either working on trivial problems or hiding something.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import state  # noqa: E402
from devin import load_env  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ENV = load_env(ROOT)

# Committed at the baseline SHA. Every burndown is a delta against these.
BASELINE = {
    "react-hooks/rules-of-hooks":
        {"count": 47, "false_positives": 4, "switch": '"warn"', "lang": "ts"},
    "react/no-unstable-nested-components":
        {"count": 150, "false_positives": 0, "switch": '"warn"', "lang": "ts"},
    "react/jsx-key":
        {"count": 81, "false_positives": 0, "switch": "absent (default warn)", "lang": "ts"},
    "@typescript-eslint/ban-ts-comment":
        {"count": 197, "false_positives": 0, "switch": '"off"', "lang": "ts"},
    # Python. The switch here is not a severity but a scope exclusion: eight
    # modules are exempted from warn_unused_ignores in pyproject.toml, which is
    # the same deadlock expressed differently -- the check exists and is turned
    # off exactly where it would fire.
    "unused-ignore":
        {"count": 49, "false_positives": 0,
         "switch": "8 modules exempted in pyproject.toml", "lang": "py"},
}

# Devin ACU list price. Kept as a named constant because every cost figure on the
# page depends on it, and it is an assumption rather than a measurement.
USD_PER_ACU = 2.25


def snapshot() -> dict:
    con = state.connect(ROOT)
    rows = [dict(r) for r in con.execute("SELECT * FROM sessions ORDER BY created_at DESC")]

    merged = [r for r in rows if r["status"] == "merged"]
    escalated = [r for r in rows if r["status"] == "escalated"]
    failed = [r for r in rows if r["status"] == "failed"]
    active = [r for r in rows if r["status"] in ("queued", "running", "verifying")]
    done = [r for r in rows if r["status"] in ("merged", "pr_open", "verifying")]

    acu = sum(r["acu_spent"] or 0 for r in rows)
    findings_fixed = sum(r["findings"] for r in merged)

    gates = []
    for rule, meta in BASELINE.items():
        touched = [r for r in rows if r["rule"] == rule]
        fixed = sum(r["findings"] for r in touched if r["status"] == "merged")
        real = meta["count"] - meta["false_positives"]
        gates.append({
            "rule": rule,
            "baseline": meta["count"],
            "false_positives": meta["false_positives"],
            "real": real,
            "fixed": fixed,
            "remaining": real - fixed,
            "switch": meta["switch"],
            "closed": (real - fixed) == 0,
            "pct": round(100 * fixed / real) if real else 0,
        })

    terminal = len(merged) + len(escalated) + len(failed)

    # The API has reported acus_consumed = 0.0 on every session so far, including
    # ones that ran to completion and opened a pull request. Rather than divide by
    # it and publish "$0.00 per merged PR" -- which reads as either broken or
    # dishonest -- the cost panel declares the number unavailable and says why.
    # A cost model nobody can reproduce is worse than an admitted gap.
    acu_reported = acu > 0
    cost_per_pr = round(acu * USD_PER_ACU / len(merged), 2) if (merged and acu_reported) else None

    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "gates_closed": sum(1 for g in gates if g["closed"]),
        "gates_total": len(gates),
        "gates": gates,
        "findings_fixed": findings_fixed,
        "acu_spent": round(acu, 2),
        "acu_ceiling": float(ENV["GLOBAL_ACU_CEILING"]),
        "usd_spent": round(acu * USD_PER_ACU, 2),
        "acu_reported_by_api": acu_reported,
        "cost_per_merged_pr": cost_per_pr,
        "sessions": len(rows),
        "active": len(active),
        "merged": len(merged),
        "escalated": len(escalated),
        "failed": len(failed),
        "success_rate": round(100 * len(merged) / terminal) if terminal else None,
        "escalation_rate": round(100 * len(escalated) / terminal) if terminal else None,
        "rows": rows[:25],
    }


STYLE = """
:root{--bg:#0e1116;--card:#161b22;--line:#2a3038;--fg:#e6edf3;--dim:#8b949e;
--ok:#3fb950;--warn:#d29922;--bad:#f85149;--accent:#58a6ff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;padding:28px}
h1{font-size:17px;margin:0 0 2px}.sub{color:var(--dim);font-size:12px;margin-bottom:22px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin-bottom:26px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px}
.card .k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
.card .v{font-size:27px;font-weight:600;margin-top:6px}
.card .n{color:var(--dim);font-size:11px;margin-top:4px}
.hero{border-color:var(--accent)}.hero .v{color:var(--accent)}
table{width:100%;border-collapse:collapse;background:var(--card);
border:1px solid var(--line);border-radius:8px;overflow:hidden;margin-bottom:24px}
th{text-align:left;font-size:11px;color:var(--dim);text-transform:uppercase;
letter-spacing:.06em;padding:10px 14px;border-bottom:1px solid var(--line)}
td{padding:10px 14px;border-bottom:1px solid var(--line);font-size:13px}
tr:last-child td{border-bottom:none}
.bar{height:6px;background:#21262d;border-radius:3px;overflow:hidden;min-width:110px}
.bar i{display:block;height:100%;background:var(--ok)}
.pill{padding:2px 8px;border-radius:20px;font-size:11px;border:1px solid}
.s-merged{color:var(--ok);border-color:var(--ok)}
.s-escalated{color:var(--warn);border-color:var(--warn)}
.s-failed{color:var(--bad);border-color:var(--bad)}
.s-running,.s-verifying,.s-queued,.s-pr_open{color:var(--accent);border-color:var(--accent)}
.note{color:var(--dim);font-size:12px;border-left:2px solid var(--line);padding-left:12px;margin:18px 0}
a{color:var(--accent)}
"""


def render(d: dict) -> str:
    def card(k, v, n="", hero=False):
        return (f'<div class="card{" hero" if hero else ""}"><div class="k">{k}</div>'
                f'<div class="v">{v}</div><div class="n">{n}</div></div>')

    cards = "".join([
        card("Gates closed", f'{d["gates_closed"]}/{d["gates_total"]}',
             "a closed gate cannot reopen", hero=True),
        card("Findings fixed", d["findings_fixed"], "verified independently, then merged"),
        card("Cost / merged PR",
             f'${d["cost_per_merged_pr"]}' if d["cost_per_merged_pr"] is not None else "n/a",
             f'{d["acu_spent"]} of {d["acu_ceiling"]} ACU used' if d["acu_reported_by_api"]
             else "API reports 0 ACU on all sessions"),
        card("Merged", d["merged"], f'{d["active"]} in flight'),
        card("Escalated", d["escalated"],
             f'{d["escalation_rate"]}% of finished' if d["escalation_rate"] is not None else "none yet"),
    ])

    grows = ""
    for g in d["gates"]:
        fp = (f' <span style="color:var(--warn)">({g["false_positives"]} false positive'
              f'{"s" if g["false_positives"] != 1 else ""})</span>') if g["false_positives"] else ""
        mark = '<span style="color:var(--ok)">CLOSED</span>' if g["closed"] else g["switch"]
        grows += (
            f'<tr><td><code>{g["rule"]}</code>{fp}</td>'
            f'<td>{g["fixed"]} / {g["real"]}</td>'
            f'<td><div class="bar"><i style="width:{g["pct"]}%"></i></div></td>'
            f'<td>{g["remaining"]}</td><td>{mark}</td></tr>'
        )

    srows = ""
    for r in d["rows"]:
        pr = (f'<a href="{r["pr_url"]}">#{r["pr_url"].rsplit("/", 1)[-1]}</a>'
              if r["pr_url"] else "--")
        srows += (
            f'<tr><td>#{r["issue_number"]}</td><td>{r["area"]}</td>'
            f'<td><code>{r["rule"].split("/")[-1]}</code></td><td>{r["findings"]}</td>'
            f'<td><span class="pill s-{r["status"]}">{r["status"]}</span></td>'
            f'<td>{r["acu_spent"] or 0}</td><td>{pr}</td></tr>'
        )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Debt ratchet</title><meta http-equiv="refresh" content="20">
<style>{STYLE}</style></head><body>
<h1>Debt ratchet &mdash; {ENV["FORK_REPO"]}</h1>
<div class="sub">baseline {ENV["BASELINE_SHA"][:10]} &nbsp;·&nbsp; {d["generated"]} &nbsp;·&nbsp; refreshes every 20s</div>
<div class="grid">{cards}</div>

<table><tr><th>Gate</th><th>Cleared</th><th>Burndown</th><th>Remaining</th><th>Enforcement</th></tr>
{grows}</table>

<div class="note">
<b>Gates closed is the only metric here that does not decay.</b> Findings fixed goes
back up the moment attention moves elsewhere &mdash; that is what happened to the 1,453
findings already being counted on every CI run. A closed gate is a promoted
enforcement switch: the defect class becomes impossible to reintroduce, so the
work cannot be undone by inattention.<br><br>
Cost is computed against <i>merged</i> pull requests, not attempted ones, at
${USD_PER_ACU}/ACU. {'' if d['acu_reported_by_api'] else
'<b>The API has returned <code>acus_consumed: 0.0</code> for every session so far, '
'including completed ones that opened pull requests</b> &mdash; so cost per PR is '
'shown as unavailable rather than as $0.00. The budget controls are still live: '
'a per-session cap the API enforces, and a global ceiling checked before any '
'session is created. '}False positives are subtracted from each gate's denominator
&mdash; the 4 <code>rules-of-hooks</code> findings in <code>playwright/</code> are the
linter matching on the name of Playwright's <code>use()</code> fixture callback,
not real violations, and a gate cannot close while its target is wrong.
</div>

<table><tr><th>Issue</th><th>Area</th><th>Rule</th><th>Findings</th><th>Status</th><th>ACU</th><th>PR</th></tr>
{srows or '<tr><td colspan="7">no sessions yet</td></tr>'}</table>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        d = snapshot()
        if self.path == "/metrics.json":
            body, ctype = json.dumps(d, indent=2, default=str).encode(), "application/json"
        else:
            body, ctype = render(d).encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    port = int(ENV.get("DASHBOARD_PORT", "8100"))
    print(f"dashboard on http://localhost:{port}  (JSON at /metrics.json)", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
