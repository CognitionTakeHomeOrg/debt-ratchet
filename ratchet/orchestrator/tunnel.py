#!/usr/bin/env python3
"""Expose the local receiver and point the repository's webhook at it.

A quick tunnel gets a new hostname every time it starts, so a hook registered by
hand is correct exactly once. After the next restart it points at a host that no
longer exists, GitHub's deliveries fail into the void, and the system looks
broken when it is merely unaddressed -- the worst failure mode available, because
nothing reports it.

So the address is not configuration to be typed. This starts the tunnel, reads
the hostname it was given, and *upserts* the hook: one hook is reused and patched
rather than a new one added, because a pile of stale hooks is the same silent
failure multiplied. It then makes GitHub prove reachability with a ping before
claiming the loop is closed.

    python ratchet/orchestrator/tunnel.py             # start, point the hook, stay up
    python ratchet/orchestrator/tunnel.py --status    # what is registered right now
    python ratchet/orchestrator/tunnel.py --prune     # remove every receiver hook

Ctrl-C stops the tunnel. The hook is left in place and will be re-pointed on the
next run; `--prune` is the way to remove it deliberately.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from devin import load_env  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
ENV = load_env(ROOT)
REPO = ENV.get("FORK_REPO", "")
SECRET = ENV.get("GITHUB_WEBHOOK_SECRET", "")
PORT = ENV.get("WEBHOOK_PORT", "8099")

# Any hook whose path is /webhook is one of ours. Matching on the path rather
# than the host is deliberate: the host is exactly the part that changes.
OURS = re.compile(r"/webhook/?$")
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def gh(*args: str, check: bool = True) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}\n{r.stderr.strip()}")
    return r.stdout.strip()


def receiver_hooks() -> list[dict]:
    return [h for h in json.loads(gh("api", f"repos/{REPO}/hooks") or "[]")
            if OURS.search(h.get("config", {}).get("url", ""))]


def upsert(url: str) -> int:
    """Point the receiver hook at `url`, creating it only if none exists.

    Returns the hook id. Extra receiver hooks are reported rather than silently
    tolerated: two hooks means every issue launches two sessions, which is a way
    to spend twice as much and open duplicate pull requests.
    """
    body = json.dumps({
        "active": True,
        "events": ["issues"],
        "config": {"url": f"{url}/webhook", "content_type": "json",
                   "secret": SECRET, "insecure_ssl": "0"},
    })
    existing = receiver_hooks()
    if existing:
        hook = existing[0]
        _patch(hook["id"], body)
        print(f"  hook {hook['id']} re-pointed -> {url}/webhook")
        for extra in existing[1:]:
            print(f"  WARNING extra receiver hook {extra['id']} "
                  f"({extra['config']['url']}) -- run --prune", file=sys.stderr)
        return hook["id"]

    out = _post(json.dumps(json.loads(body) | {"name": "web"}))
    hook_id = json.loads(out)["id"]
    print(f"  hook {hook_id} created -> {url}/webhook")
    return hook_id


def _patch(hook_id: int, body: str) -> str:
    r = subprocess.run(["gh", "api", f"repos/{REPO}/hooks/{hook_id}", "-X", "PATCH",
                        "--input", "-"], input=body, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout


def _post(body: str) -> str:
    r = subprocess.run(["gh", "api", f"repos/{REPO}/hooks", "-X", "POST", "--input", "-"],
                       input=body, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip())
    return r.stdout


def latest_delivery(hook_id: int) -> dict | None:
    out = gh("api", f"repos/{REPO}/hooks/{hook_id}/deliveries", check=False)
    for d in json.loads(out or "[]"):
        if d.get("status_code"):
            return d
    return None


def confirm(hook_id: int, tries: int = 10) -> bool:
    """Make GitHub prove it can reach us *now*.

    Reading the newest delivery is not enough: after a re-point the newest
    delivery is whatever the previous URL did, so a hook pointing at a dead host
    reports the last success of a tunnel that no longer exists. This fires a
    fresh ping and waits for a delivery newer than the one already on record --
    the only version of this check that tests the address just registered.
    """
    before = latest_delivery(hook_id)
    before_at = before["delivered_at"] if before else ""
    gh("api", f"repos/{REPO}/hooks/{hook_id}/pings", "-X", "POST", check=False)

    for _ in range(tries):
        d = latest_delivery(hook_id)
        if d and d["delivered_at"] > before_at:
            ok = 200 <= d["status_code"] < 300
            print(f"  delivery: {d['event']} -> {d['status']} ({d['status_code']})")
            if not ok:
                print("  (530 means Cloudflare could not reach the receiver -- "
                      "is it still listening?)", file=sys.stderr)
            return ok
        time.sleep(3)
    print("  no fresh delivery seen", file=sys.stderr)
    return False


def start_tunnel() -> tuple[subprocess.Popen, str]:
    log = ROOT / "ratchet" / ".tunnel.log"
    log.write_text("")
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}"],
        stdout=log.open("a"), stderr=subprocess.STDOUT,
    )
    for _ in range(40):
        m = TUNNEL_URL.search(log.read_text())
        if m:
            return proc, m.group(0)
        if proc.poll() is not None:
            raise RuntimeError(f"cloudflared exited:\n{log.read_text()[-600:]}")
        time.sleep(1)
    proc.terminate()
    raise RuntimeError(f"no tunnel URL after 40s:\n{log.read_text()[-600:]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true", help="show the registered hook")
    ap.add_argument("--prune", action="store_true", help="delete every receiver hook")
    ap.add_argument("--url", help="point at this URL instead of starting a tunnel")
    args = ap.parse_args()

    if not REPO:
        print("FORK_REPO is not set (see .env)", file=sys.stderr)
        return 1

    if args.status:
        hooks = receiver_hooks()
        if not hooks:
            print("no receiver hook registered")
            return 1
        ok = True
        for h in hooks:
            print(f"hook {h['id']}  active={h['active']}  {h['config']['url']}", flush=True)
            ok &= confirm(h["id"])
        return 0 if ok else 1

    if args.prune:
        for h in receiver_hooks():
            gh("api", f"repos/{REPO}/hooks/{h['id']}", "-X", "DELETE")
            print(f"deleted hook {h['id']}")
        return 0

    if not SECRET:
        print("GITHUB_WEBHOOK_SECRET is empty -- the receiver would reject every "
              "delivery. Generate one with `openssl rand -hex 24`.", file=sys.stderr)
        return 1

    # The receiver has to be up first: the hook's ping arrives seconds after
    # registration, and a hook whose first delivery fails is indistinguishable
    # from a misconfigured one.
    health = subprocess.run(["curl", "-fsS", "-m", "3",
                             f"http://localhost:{PORT}/health"], capture_output=True)
    if health.returncode != 0:
        print(f"nothing answering on :{PORT}. Start it first:\n"
              f"  docker compose up -d orchestrator\n"
              f"  # or: python ratchet/orchestrator/webhook.py", file=sys.stderr)
        return 1

    proc = None
    try:
        if args.url:
            url = args.url.rstrip("/")
            print(f"using {url}")
        else:
            print("starting tunnel ...")
            proc, url = start_tunnel()
            print(f"  {url}")

        hook_id = upsert(url)
        ok = confirm(hook_id)
        print()
        if ok:
            print("loop closed. Filing an issue now starts a session:")
            print("  python ratchet/detector/detect.py --workstream C "
                  "--only-area src/explore --apply")
        else:
            print("hook registered but no successful delivery yet -- check "
                  "`--status` in a moment.", file=sys.stderr)

        if proc:
            print("\nCtrl-C to stop the tunnel. The hook is re-pointed on every run.")
            proc.wait()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        if proc and proc.poll() is None:
            proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
