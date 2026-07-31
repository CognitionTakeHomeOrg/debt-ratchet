"""Gate runners.

A *gate* is a checker plus an enforcement switch. Superset has built every
checker here and set every switch to non-blocking, because the pre-existing
debt is too large to turn on. Each runner below measures one gate's debt.

Every runner returns the same normalized shape so the grouping and issue-filing
layers never need to know which tool produced a finding.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path


@dataclass(frozen=True)
class Finding:
    gate: str  # oxlint | mypy | zizmor
    rule: str  # react/jsx-key
    path: str  # repo-relative, always
    line: int
    column: int
    message: str

    def as_dict(self) -> dict:
        return asdict(self)


# --- oxlint -----------------------------------------------------------------
#
# Trap 2: oxlint MUST run from superset-frontend/ with no path arguments and via
# the lockfile-pinned binary. Passing `src packages plugins` under-reports --
# rules-of-hooks is 47 at CI scope but 43 if you name the paths, because
# playwright/ drops out. `npx` may resolve a different version than the lockfile.

FRONTEND_SUBDIR = "superset-frontend"
OXLINT_BIN = "./node_modules/.bin/oxlint"


def run_oxlint(repo_root: Path, rule: str) -> list[Finding]:
    """Measure a single oxlint rule in isolation.

    `-A all -D <rule>` switches every rule off, then turns exactly one on at
    error severity. Isolating the rule keeps the count stable when unrelated
    rules change, and promoting to error is what makes the count meaningful --
    the repo's own `npm run lint` passes `--quiet`, which hides the findings but
    not the count.
    """
    frontend = repo_root / FRONTEND_SUBDIR
    proc = subprocess.run(
        [OXLINT_BIN, "--config", "oxlint.json", "-A", "all", "-D", rule, "--format", "json"],
        cwd=frontend,
        capture_output=True,
        text=True,
    )
    if not proc.stdout.strip():
        raise RuntimeError(f"oxlint produced no output for {rule}: {proc.stderr[:400]}")

    payload = json.loads(proc.stdout)
    findings = []
    for d in payload.get("diagnostics", []):
        span = (d.get("labels") or [{}])[0].get("span", {})
        findings.append(
            Finding(
                gate="oxlint",
                rule=rule,
                # oxlint reports relative to superset-frontend/; re-root so every
                # finding in the system is addressable from the repo root.
                path=f"{FRONTEND_SUBDIR}/{d['filename']}",
                line=span.get("line", 0),
                column=span.get("column", 0),
                message=d.get("message", "").strip(),
            )
        )
    return findings


# --- mypy -------------------------------------------------------------------
#
# Trap 1, and it is the trap most likely to waste a session.
#
# mypy's answer depends on what is installed. Pre-commit runs it in an isolated
# environment of mypy 1.15.0 plus ten stub packages and nothing else; there,
# master is clean across 1,423 files. Install Superset's real dependencies and
# the same command on the same commit reports 1,502 errors. Both numbers are
# real. Only the first one is the gate, because the first one is what CI runs.
#
# The gate config replicates `pyproject.toml`'s mypy settings with exactly one
# variable changed: the `warn_unused_ignores = false` override covering eight
# modules is removed. The control config keeps it. The control running clean is
# what makes the 49 trustworthy -- it proves the config differs from the repo's
# real settings in that one variable and nothing else.

MYPY_LINE = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+): error: (?P<msg>.*?)\s+\[(?P<code>[\w-]+)\]$")
GATES_DIR = Path(__file__).resolve().parents[1] / "gates"


def mypy_bin() -> str:
    import os

    env = os.environ.get("MYPY_GATE")
    if env and Path(env).exists():
        return env
    local = Path(__file__).resolve().parents[2] / ".mypy-gate" / "bin" / "mypy"
    if local.exists():
        return str(local)
    raise RuntimeError(
        "no isolated mypy found. Create it with the stub-only recipe -- running "
        "mypy from a environment with Superset's dependencies installed answers a "
        "different question than the gate asks (see Trap 1)."
    )


def run_mypy(repo_root: Path, config: str = "mypy-unused-ignore.ini",
             code: str | None = "unused-ignore") -> list[Finding]:
    """Run the isolated mypy gate.

    `code` filters to one error code. Passing None returns every error, which is
    how the verifier asks the question that actually matters after a suppression
    is deleted: *did removing it surface a real type error?*
    """
    proc = subprocess.run(
        [mypy_bin(), "--config-file", str(GATES_DIR / config),
         "--check-untyped-defs", "--no-color-output", "--no-error-summary", "superset/"],
        cwd=repo_root, capture_output=True, text=True,
    )
    findings = []
    for line in proc.stdout.splitlines():
        m = MYPY_LINE.match(line.strip())
        if m and (code is None or m.group("code") == code):
            findings.append(
                Finding(
                    gate="mypy",
                    # The code parsed from the line, never the filter argument --
                    # with code=None the latter tags every finding None, and the
                    # verifier's "any error that is not unused-ignore" check then
                    # matches all 49 of them on a clean tree.
                    rule=m.group("code"),
                    path=m.group("path"),
                    line=int(m.group("line")),
                    column=0,
                    message=m.group("msg"),
                )
            )
    if not findings and proc.returncode not in (0, 1):
        raise RuntimeError(f"mypy failed: {(proc.stderr or proc.stdout)[:400]}")
    return findings


# Test fixtures, stories and spec helpers are excluded from the suppression
# workstream. A `@ts-expect-error` over a deliberately malformed fixture is
# usually correct -- the fixture exists precisely to be wrong. Sweeping those in
# would inflate the number by 133 and hand Devin a queue of tasks whose right
# answer is "leave it alone", which is the fastest way to lose a reviewer's
# trust in the whole set.
TEST_PATH = re.compile(r"(\.test\.|\.stories\.|_spec\.|(^|/)spec/|/__tests__/|(^|/)cypress|(^|/)playwright/)")


def production_only(findings: list[Finding]) -> list[Finding]:
    return [f for f in findings if not TEST_PATH.search(f.path.split("/", 1)[1])]


def assert_clean_tree(repo_root: Path) -> None:
    """Refuse to measure a dirty tree.

    This exists because of a real incident: a set of uncommitted autofix
    experiments were left in the working tree and produced 19 phantom errors
    that were nearly written up as an upstream version-drift bug. Every number
    this system reports is a delta against a baseline, so a dirty tree does not
    produce a wrong number -- it produces a fabricated one.
    """
    proc = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo_root, capture_output=True, text=True
    )
    dirty = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    if dirty:
        raise RuntimeError(
            f"working tree is dirty ({len(dirty)} paths) -- refusing to measure.\n"
            + "\n".join(dirty[:10])
        )
