#!/usr/bin/env python3
"""The event receiver.

Nobody decides what Devin works on. A gate reports debt, the detector files an
issue, and *that filing is the event*. This process turns it into a session.

Deliberately written against the standard library. `docker compose up` resolving
no dependencies is worth more here than the ergonomics of a framework, and the
HTTP surface is three routes.

Three properties that matter more than they look:

  * **Signature verification before parsing.** The endpoint is public by
    necessity -- GitHub has to reach it -- and it spends money. An unauthenticated
    caller who can reach it can drain an ACU budget.

  * **Respond 202 before doing the work.** GitHub gives a webhook 10 seconds and
    then retries. Launching a Devin session takes longer than that, so doing it
    inline guarantees a retry, and a retry guarantees a duplicate session and a
    duplicate pull request. Acknowledge first, work after.

  * **A reconciler alongside the webhook.** Webhooks get missed -- tunnel drops,
    process restarts, a delivery that 500s after its retries are exhausted. The
    reconciler periodically asks GitHub what is actually labelled `devin:queued`
    and starts anything the webhook dropped, which makes the system converge
    rather than depend on every delivery landing.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import state  # noqa: E402
from devin import load_env  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ENV = load_env(ROOT)
TRIGGER_LABEL = "devin:queued"


def launch_async(issue_number: int) -> None:
    """Run the launcher out of band so the HTTP response is never blocked."""

    def worker():
        print(f"  -> launching session for issue #{issue_number}", flush=True)
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "run.py"), "--issue", str(issue_number)],
            capture_output=True, text=True,
        )
        print((r.stdout or r.stderr).strip(), flush=True)

    threading.Thread(target=worker, daemon=True).start()


def should_handle(event: str, payload: dict) -> tuple[bool, str]:
    if event == "ping":
        return False, "ping"
    if event != "issues":
        return False, f"ignoring event {event!r}"

    action = payload.get("action")
    issue = payload.get("issue", {})
    labels = {l["name"] for l in issue.get("labels", [])}

    # Only the detector's own issues, and only while they are queued. Without the
    # label check, any human opening any issue starts a paid session.
    if TRIGGER_LABEL not in labels:
        return False, f"issue #{issue.get('number')} not labelled {TRIGGER_LABEL}"
    if action not in {"opened", "labeled", "reopened"}:
        return False, f"ignoring action {action!r}"
    if "<!-- ratchet-fingerprint:" not in (issue.get("body") or ""):
        return False, "not a detector-filed issue"
    return True, ""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        print(f"  {self.address_string()} {fmt % a}", flush=True)

    def _send(self, code: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path != "/health":
            return self._send(404, {"error": "not found"})
        con = state.connect(ROOT)
        self._send(200, {
            "ok": True,
            "acu_spent": state.total_acu(con),
            "acu_ceiling": float(ENV["GLOBAL_ACU_CEILING"]),
            "in_flight": state.active_count(con),
        })

    def do_POST(self):
        if self.path != "/webhook":
            return self._send(404, {"error": "not found"})

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)

        secret = ENV.get("GITHUB_WEBHOOK_SECRET", "")
        sig = self.headers.get("X-Hub-Signature-256", "")
        if secret:
            expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
            # compare_digest, not ==, so a timing side channel cannot be used to
            # forge a signature byte by byte.
            if not hmac.compare_digest(expected, sig):
                print("  REJECTED: bad signature", flush=True)
                return self._send(401, {"error": "bad signature"})
        elif sig:
            print("  WARNING: signed delivery but no GITHUB_WEBHOOK_SECRET set", flush=True)

        event = self.headers.get("X-GitHub-Event", "")
        payload = json.loads(raw or b"{}")
        ok, why = should_handle(event, payload)
        number = payload.get("issue", {}).get("number")

        if not ok:
            print(f"  skip: {why}", flush=True)
            return self._send(200, {"skipped": why})

        # Acknowledge inside GitHub's 10s budget, then work.
        self._send(202, {"accepted": True, "issue": number})
        print(f"  accepted issue #{number}", flush=True)
        launch_async(number)


def reconcile_loop(interval: int = 300) -> None:
    """Start anything the webhook missed. Convergence, not delivery guarantees."""
    repo = ENV["FORK_REPO"]
    while True:
        time.sleep(interval)
        try:
            out = subprocess.run(
                ["gh", "issue", "list", "--repo", repo, "--label", TRIGGER_LABEL,
                 "--state", "open", "--json", "number"],
                capture_output=True, text=True, check=True).stdout
            con = state.connect(ROOT)
            started = {r[0] for r in con.execute("SELECT issue_number FROM sessions")}
            for issue in json.loads(out):
                if issue["number"] not in started:
                    print(f"  reconciler: #{issue['number']} was never started", flush=True)
                    launch_async(issue["number"])
        except Exception as e:  # a failed sweep must not kill the server
            print(f"  reconciler error: {e}", flush=True)


def settle_loop(interval: int = 60) -> None:
    """Carry launched sessions through to a verdict.

    Launching is event-driven; settling cannot be. Devin reports progress by
    being asked, and a pull request appears at a moment nothing notifies us
    about -- so without this the system starts work correctly and then abandons
    it: the session runs, the PR opens, and nothing verifies it or says so on
    the issue. That is a worse outcome than not starting, because it looks like
    success.

    Runs as a subprocess for the same reason `launch_async` does: sqlite
    connections are not shared across threads, and a poll that crashes must not
    take the receiver down with it.
    """
    while True:
        time.sleep(interval)
        try:
            subprocess.run(
                [sys.executable, str(Path(__file__).parent / "run.py"), "--poll"],
                capture_output=True, text=True, timeout=1800,
            )
        except Exception as e:  # a failed sweep must not kill the server
            print(f"  poller error: {e}", flush=True)


def main() -> int:
    port = int(ENV.get("WEBHOOK_PORT", "8099"))
    threading.Thread(target=reconcile_loop, daemon=True).start()
    threading.Thread(target=settle_loop, daemon=True).start()
    print(f"listening on :{port}  (POST /webhook, GET /health)", flush=True)
    print(f"trigger: issues labelled {TRIGGER_LABEL!r} on {ENV['FORK_REPO']}", flush=True)
    if not ENV.get("GITHUB_WEBHOOK_SECRET"):
        print("WARNING: GITHUB_WEBHOOK_SECRET unset -- signatures are not verified", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
