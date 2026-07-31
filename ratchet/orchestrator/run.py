#!/usr/bin/env python3
"""Launch and track one Devin session for one filed issue.

    python run.py --issue 1            # launch
    python run.py --poll               # poll everything in flight

The budget check happens before the session is created, never after.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1] / "detector"))

import prompt as prompt_mod  # noqa: E402
import state  # noqa: E402
from devin import DevinClient, load_env  # noqa: E402
from gates import GATES_DIR, assert_clean_tree, production_only, run_mypy, run_oxlint  # noqa: E402
from grouping import group  # noqa: E402

WORKSTREAM_RULES = {
    "A": "react-hooks/rules-of-hooks",
    "B": "react/no-unstable-nested-components",
    "C": "react/jsx-key",
    "D": "@typescript-eslint/ban-ts-comment",
    "G": "react/prefer-function-component",
    "E": "unused-ignore",
}
MYPY_WORKSTREAMS = {"E"}
PRODUCTION_ONLY = {"D"}

# Verified against a live v3 session, not against the docs: the response field is
# `status` (not `status_enum`), spend is `acus_consumed`, and pull requests come
# back as a list under `pull_requests`.
TERMINAL = {"finished", "completed", "blocked", "expired", "stopped"}

# `waiting_for_user` is reported with `status: "running"`, but the session has
# stopped and is waiting to be spoken to. Left unhandled it sits in flight
# forever, holding a concurrency slot against a session that will never move on
# its own. Whether it is done or stuck is decided by what it produced, not by the
# status string.
IDLE_DETAIL = "waiting_for_user"


def read_status(s: dict) -> tuple[str | None, float, str | None]:
    status = s.get("status") or s.get("status_enum")
    acu = s.get("acus_consumed") or 0
    prs = s.get("pull_requests") or []
    pr = None
    if prs:
        first = prs[0]
        # The key is `pr_url`, not `url`. Reading the wrong one returns None,
        # which is indistinguishable from "no pull request was opened" -- and
        # that difference decides whether an idle session is recorded as finished
        # or escalated. A missing field must not be able to masquerade as a
        # negative result.
        pr = first.get("pr_url") or first.get("url") if isinstance(first, dict) else str(first)
    return status, float(acu), pr


class LaunchError(Exception):
    """A launch that should not happen, reported without a stack trace.

    The webhook path invokes this module as a subprocess, so an uncaught
    exception here surfaces as a traceback in the server log for what is often
    an ordinary condition -- a stale delivery, an issue closed since it was
    filed. Those should read as one line, not as a crash.
    """


def gh_issue(repo: str, number: int) -> dict:
    r = subprocess.run(
        ["gh", "issue", "view", str(number), "--repo", repo, "--json",
         "number,title,body,labels,state"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise LaunchError(f"issue #{number} not readable in {repo}: {r.stderr.strip()[:200]}")
    return json.loads(r.stdout)


def fingerprint_of(issue: dict) -> str:
    marker = "<!-- ratchet-fingerprint: "
    return issue["body"].split(marker, 1)[1].split(" -->", 1)[0].strip()


def relabel(repo: str, number: int, add: str, remove: list[str]) -> None:
    cmd = ["gh", "issue", "edit", str(number), "--repo", repo, "--add-label", add]
    for r in remove:
        cmd += ["--remove-label", r]
    subprocess.run(cmd, capture_output=True)


def launch(args, root: Path, env: dict, con) -> int:
    repo, sha = env["FORK_REPO"], env["BASELINE_SHA"]
    ceiling = float(env["GLOBAL_ACU_CEILING"])
    per_session = int(env["MAX_ACU_PER_SESSION"])
    max_conc = int(env["MAX_CONCURRENT_SESSIONS"])

    spent = state.total_acu(con)
    if spent + per_session > ceiling:
        print(f"REFUSING: {spent:.1f} ACU spent, ceiling {ceiling}. "
              f"Starting a session could exceed it.", file=sys.stderr)
        return 2
    if state.active_count(con) >= max_conc:
        print(f"REFUSING: {state.active_count(con)} sessions already in flight "
              f"(cap {max_conc}).", file=sys.stderr)
        return 2

    issue = gh_issue(repo, args.issue)
    fp = fingerprint_of(issue)
    ident = issue["title"].split("]")[0].lstrip("[")
    workstream = ident[0]
    rule = WORKSTREAM_RULES[workstream]

    # Re-derive the findings from the gate rather than parsing them back out of
    # the issue body. The issue is a human-facing artifact; the gate is the
    # source of truth, and it may have moved since the issue was filed.
    repo_path = (root / "superset-adham-clone").resolve()
    assert_clean_tree(repo_path)
    is_mypy = workstream in MYPY_WORKSTREAMS
    all_findings = run_mypy(repo_path) if is_mypy else run_oxlint(repo_path, rule)
    if workstream in PRODUCTION_ONLY:
        all_findings = production_only(all_findings)
    units = {u.fingerprint: u for u in group(all_findings, workstream)}
    if fp not in units:
        print(f"issue #{args.issue} has no matching findings any more -- already fixed?",
              file=sys.stderr)
        return 1
    unit = units[fp]

    if is_mypy:
        text = prompt_mod.build_mypy(
            repo=repo, sha=sha, area=unit.area, findings=unit.findings,
            before=len(all_findings),
            config_text=(GATES_DIR / "mypy-unused-ignore.ini").read_text().strip(),
        )
    else:
        text = prompt_mod.build(
            repo=repo, sha=sha, rule=rule, area=unit.area,
            findings=unit.findings, before=len(all_findings),
        )
    if args.dry_run:
        print(text)
        return 0

    client = DevinClient(env["DEVIN_API_KEY"], env["DEVIN_ORG_ID"], env["DEVIN_API_BASE"])
    sess = client.create_session(
        prompt=text,
        title=f"[{unit.ident}] {rule} in {unit.area}",
        tags=["debt-ratchet", f"workstream:{workstream}", f"issue:{args.issue}"],
        max_acu=per_session,
        idem_key=f"ratchet-{fp}",
    )
    state.record(
        con, session_id=sess.session_id, issue_number=args.issue, fingerprint=fp,
        workstream=workstream, rule=rule, area=unit.area, findings=len(unit.findings),
        status="running", session_url=sess.url,
    )
    relabel(repo, args.issue, "devin:in-progress", ["devin:queued"])

    # Post the session link on the issue at launch, not at completion.
    #
    # Devin puts the link in the pull request body itself, but that only exists
    # once there *is* a pull request -- so a session that is still running, or
    # that escalates without opening one, is unreachable from the issue that
    # started it. The issue is the hub the whole system pivots on; anyone
    # triaging one should be able to reach the run without going through this
    # database.
    subprocess.run(
        ["gh", "issue", "comment", str(args.issue), "--repo", repo, "--body",
         f"**Devin session started** — [`{sess.session_id[:12]}`]({sess.url})\n\n"
         f"| | |\n|---|---|\n"
         f"| Unit | `{unit.ident}` — `{unit.area}` |\n"
         f"| Gate | `{rule}` |\n"
         f"| Findings | {len(unit.findings)} across {len(unit.files)} files |\n"
         f"| Budget | capped at {per_session} ACU |\n\n"
         f"Result will be verified independently before merge; this comment is "
         f"the audit trail from issue to run."],
        capture_output=True,
    )

    print(f"session {sess.session_id}  new={sess.is_new}\n{sess.url}")
    print(f"issue #{args.issue}  {unit.ident}  {unit.area}  "
          f"{len(unit.findings)} findings  cap {per_session} ACU")
    return 0


def update_progress(client, con, row, repo: str, status: str, pr: str | None) -> None:
    """Mirror the session's narration onto its issue, in one comment.

    Edited in place rather than appended. A session emits ten to thirty messages;
    posting each one turns the issue into a log file and buries the findings the
    issue exists to describe. One comment that changes is readable -- and it is
    also what makes the issue a live status page rather than an archive.
    """
    try:
        msgs = client.get_messages(row["session_id"])
    except Exception as e:
        print(f"  (progress unavailable: {e})")
        return

    devin_msgs = [m for m in msgs if m.get("source") == "devin"]
    if not devin_msgs:
        return
    newest = devin_msgs[-1].get("event_id")
    if newest == row["last_event_id"] and row["progress_comment_id"]:
        return  # nothing new to say

    icon = {"merged": "✅", "escalated": "⚠️", "failed": "❌"}.get(status, "🔄")
    lines = [
        f"### {icon} Devin session `{row['session_id'][:12]}` — `{status}`",
        "",
        f"**{row['area']}** · `{row['rule']}` · {row['findings']} findings · "
        f"[open session]({row['session_url']})",
        "",
    ]
    if pr:
        lines += [f"**Pull request:** {pr}", ""]
    lines += ["<details open><summary>Progress</summary>", ""]
    for m in devin_msgs[-12:]:
        text = " ".join((m.get("message") or "").split())
        lines.append(f"- {text[:300]}")
    lines += ["", "</details>", "",
              "<sub>Updated automatically while the session runs. Merge still "
              "requires a human after independent verification.</sub>"]
    body = "\n".join(lines)

    if row["progress_comment_id"]:
        r = subprocess.run(
            ["gh", "api", "-X", "PATCH",
             f"repos/{repo}/issues/comments/{row['progress_comment_id']}",
             "-f", f"body={body}"],
            capture_output=True, text=True)
        if r.returncode == 0:
            state.update(con, row["session_id"], last_event_id=newest)
            return
        # Comment deleted or otherwise unreachable -- fall through and re-create.

    r = subprocess.run(
        ["gh", "api", f"repos/{repo}/issues/{row['issue_number']}/comments",
         "-f", f"body={body}", "--jq", ".id"],
        capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip().isdigit():
        state.update(con, row["session_id"],
                     progress_comment_id=int(r.stdout.strip()), last_event_id=newest)


def poll(args, root: Path, env: dict, con) -> int:
    client = DevinClient(env["DEVIN_API_KEY"], env["DEVIN_ORG_ID"], env["DEVIN_API_BASE"])
    rows = con.execute(
        "SELECT * FROM sessions WHERE status IN ('queued','running','verifying')"
    ).fetchall()
    if not rows:
        print("nothing in flight")
    for row in rows:
        s = client.get_session(row["session_id"])
        st, acu, pr = read_status(s)
        so = s.get("structured_output")
        state.update(
            con, row["session_id"], devin_status=st, acu_spent=acu,
            pr_url=pr, structured=json.dumps(so) if so else None,
        )
        detail = (s.get("status_detail") or "")[:60]
        print(f"#{row['issue_number']} {row['area']:24} {str(st):10} "
              f"acu={acu:5.2f} pr={pr or '-'}  {detail}")

        update_progress(client, con, row, env["FORK_REPO"], st or "running", pr)

        if detail == IDLE_DETAIL and st not in TERMINAL:
            # It produced a PR and a verdict, so it finished and simply never got
            # told so. It produced neither, so it is stuck waiting on a human --
            # which is an escalation regardless of what the status string says.
            st = "finished" if (pr and so) else "blocked"
            print(f"  -> idle; treating as {st}")

        if st in TERMINAL:
            _settle(con, env, row, st, pr, so)
    print(f"\ntotal ACU spent: {state.total_acu(con):.2f} / {env['GLOBAL_ACU_CEILING']}")
    return 0


def _verify_and_report(con, env, row, pr_url: str, partial: bool = False) -> None:
    """Run the independent verifier and post its verdict on the pull request.

    Left manual, this was the weakest link in the whole system: the orchestrator
    marked a session `verifying` and then waited for a person to remember to run
    the checks. A verification step that depends on someone remembering is not a
    control, which is the same argument that made the ratchet PR automatic.

    The verdict goes on the PR rather than into a log, because the PR is where
    the human decision actually happens. Merging stays theirs -- this makes the
    evidence unavoidable, not the decision.
    """
    number = pr_url.rstrip("/").rsplit("/", 1)[-1]

    # Verification takes minutes and every run uses the same worktree path. The
    # poll loop runs far more often than that, and a row stays selectable while
    # it is `verifying` -- so without a lock a second poll starts a second
    # verification, which begins by force-removing the worktree the first one is
    # still reading. The result would be a spurious FAIL comment on a PR that
    # was fine.
    #
    # A stale lock left by a crashed run is cleared rather than honoured: a row
    # stuck in `verifying` should be retried on the next poll, not abandoned.
    lock = Path(tempfile.gettempdir()) / "ratchet-verify.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        try:
            holder = int(lock.read_text().strip())
            os.kill(holder, 0)  # signal 0 only tests for existence
            print(f"  -> verification already running (pid {holder}); skipping")
            return
        except (ValueError, ProcessLookupError, PermissionError):
            print("  -> clearing stale verification lock")
            lock.unlink(missing_ok=True)
            lock.write_text(str(os.getpid()))

    print(f"  -> verifying PR #{number} ...")
    try:
        r = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "verify.py"), "--pr", number],
            capture_output=True, text=True,
        )
    finally:
        lock.unlink(missing_ok=True)

    report = (r.stdout or r.stderr).strip()
    passed = r.returncode == 0

    tail = "\n".join(report.splitlines()[-25:])
    if partial:
        verdict = "⚠️ **Partial fix — verified, gate still open**"
        closing = (
            "The session named something it could not do (`blocked_reason`) and "
            "explained why. The work it *did* do is checked above; the remaining "
            "findings are untouched and the issue stays open.\n\n"
            "**This is a wanted outcome, not a failure.** A fix that closed the "
            "gate by doing the risky part badly would have passed the linter and "
            "broken behaviour. Review the rationale on the issue before merging."
        )
    elif passed:
        verdict = "✅ **Independent verification passed**"
        closing = "Ready for human review. **This does not merge anything.**"
    else:
        verdict = "❌ **Independent verification FAILED**"
        closing = ("Do not merge. The gate may be green while the defect survives "
                   "— that is exactly what these checks exist to catch.")

    body = (
        f"{verdict}\n\n"
        f"Re-run in a clean worktree with the lockfile-pinned toolchain — not the "
        f"session's own environment, and not its self-report.\n\n"
        f"```\n{tail}\n```\n\n{closing}"
    )
    subprocess.run(
        ["gh", "pr", "comment", number, "--repo", env["FORK_REPO"], "--body", body],
        capture_output=True,
    )
    status = "escalated" if partial else ("pr_open" if passed else "failed")
    state.update(con, row["session_id"], status=status)
    if partial:
        relabel(env["FORK_REPO"], row["issue_number"], "status:escalated",
                ["devin:in-progress"])
    print(f"  -> {'PARTIAL' if partial else ('PASS' if passed else 'FAIL')}, "
          f"posted to PR #{number}")


def _settle(con, env, row, st, pr, so) -> None:
    repo = env["FORK_REPO"]
    if st == "blocked":
        state.update(con, row["session_id"], status="escalated")
        relabel(repo, row["issue_number"], "status:escalated", ["devin:in-progress"])
        reason = (so or {}).get("blocked_reason") or (so or {}).get("rationale") or "(none given)"
        subprocess.run(
            ["gh", "issue", "comment", str(row["issue_number"]), "--repo", repo, "--body",
             f"**Devin stopped and escalated this.**\n\n> {reason}\n\n"
             f"Session: {row['session_url']}\n\nThis needs a human decision. "
             f"Retrying it would only spend ACUs on the same wall."],
            capture_output=True,
        )
        print(f"  -> ESCALATED. Not retrying: a blocked session retried is just money.")
    elif st == "expired":
        state.update(con, row["session_id"], status="failed")
        print(f"  -> expired (hit the {env['MAX_ACU_PER_SESSION']} ACU cap or timed out)")
    elif st in ("finished", "completed"):
        # A session can finish having done correct work *and* not closed the gate.
        # That is a third outcome, and it is the most interesting one: G1 fixed
        # Theme.tsx and refused DragDroppable.tsx, because react-dnd's legacy
        # DragSource/DropTarget hand the class instance to the hover and drop
        # specs, and four other files read `component.mounted|ref|props|setState`
        # off it. Converting it would have passed the linter and silently broken
        # drag-and-drop -- exactly the failure the prompt forbids.
        #
        # Without this branch the verifier reports FAILED, because the gate is
        # genuinely still open. Stamping "failed" on a correct partial fix would
        # punish the behaviour the whole system is built to encourage, and would
        # teach a reviewer to distrust the label.
        # Keyed on `blocked_reason`, not on `gate_closed`.
        #
        # G1 fixed 1 of 2 findings and still returned `gate_closed: true`, which
        # was defensible: the verification command it was given was scoped to the
        # unit's area, and for a `misc` bucket that prefix matches nothing, so
        # check 1 printed 0 vacuously. The honest signal was in `blocked_reason`,
        # which it filled in unprompted with the react-dnd explanation.
        #
        # A session that names something it could not do has not fully succeeded,
        # whatever it says about the gate.
        so = so or {}
        partial = bool(so.get("blocked_reason")) and bool(pr)
        state.update(con, row["session_id"], status="verifying")
        print("  -> finished" + (" (PARTIAL -- gate not closed, by its own report)"
                                 if partial else ", running independent verification"))
        if pr:
            _verify_and_report(con, env, row, pr, partial=partial)

        # A declared behaviour change is not a failure -- the prompt asks for it
        # to be declared rather than hidden, and here it caught a latent crash:
        # TimeoutErrorMessage threw a TypeError on an empty issue_codes array
        # because reduce() was called with no initial value. But it does mean the
        # oracle is no longer sufficient on its own. A linter cannot tell you
        # whether changed behaviour is *wanted*, so this routes to a human.
        if (so or {}).get("behavior_change"):
            relabel(repo, row["issue_number"], "needs:human-review", [])
            subprocess.run(
                ["gh", "issue", "comment", str(row["issue_number"]), "--repo", repo, "--body",
                 f"**Devin reports a deliberate behaviour change.** Gate checks alone "
                 f"cannot approve this -- a human needs to confirm the new behaviour is "
                 f"wanted.\n\n> {(so or {}).get('rationale', '')[:900]}"],
                capture_output=True,
            )
            print("  -> flagged: declared behaviour change, needs human review")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issue", type=int)
    ap.add_argument("--poll", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print the prompt, spend nothing")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[2]
    env = load_env(root)
    con = state.connect(root)

    try:
        if args.poll:
            return poll(args, root, env, con)
        if args.issue:
            return launch(args, root, env, con)
    except LaunchError as e:
        print(f"skipped: {e}", file=sys.stderr)
        return 1
    ap.error("need --issue or --poll")


if __name__ == "__main__":
    raise SystemExit(main())
