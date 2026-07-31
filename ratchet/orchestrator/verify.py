#!/usr/bin/env python3
"""Independent verification.

The single non-negotiable component. Devin reports whether it closed the gate;
this reproduces that claim in our own checkout, with our own pinned toolchain,
before anything is trusted or merged.

The reason is not distrust of the agent specifically -- it is that "the linter is
quiet" and "the defect is gone" are different propositions, and only the first
one is cheap to check. Every anti-gaming assertion below corresponds to a way of
making a gate go green while leaving the bug in place:

  * `key={index}` silences jsx-key and preserves the reconciliation bug exactly.
  * A lint-disable comment silences anything.
  * Editing oxlint.json silences everything, permanently, and looks like progress.
  * Deleting the offending code passes every check and loses the feature --
    oxlint's own `--fix` for no-console did precisely this to a user-facing i18n
    warning, with type-check and tests still green.

A verdict of PASS means all of these were checked and none of them fired.

    python verify.py --pr 3
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[1] / "detector"))

from devin import load_env  # noqa: E402
from gates import run_mypy, run_oxlint  # noqa: E402
from grouping import area_of, group  # noqa: E402

# Files that define what "clean" means. A change here is not a fix -- it is a
# redefinition of the test, and it must be reviewed as such.
GATE_CONFIG = {
    "superset-frontend/oxlint.json",
    "superset-frontend/package.json",
    "superset-frontend/tsconfig.json",
    "pyproject.toml",
    ".pre-commit-config.yaml",
}

SUPPRESSION = re.compile(
    r"(eslint-disable|oxlint-disable|@ts-ignore|@ts-nocheck|@ts-expect-error|istanbul ignore)"
)
INDEX_KEY = re.compile(r"key=\{\s*(i|idx|index|_i)\s*\}")


# Committed counts at the baseline SHA. The denominator for every "did anything
# else regress?" check.
RULE_BASELINE = {
    "react-hooks/rules-of-hooks": 47,
    "react/no-unstable-nested-components": 150,
    "react/jsx-key": 81,
    "@typescript-eslint/ban-ts-comment": 197,
    "unused-ignore": 49,
}

MYPY_RULES = {"unused-ignore"}

# Python equivalents of the frontend anti-patterns. Each is a way to make mypy
# quiet while leaving the code no safer than before.
PY_SUPPRESSION = re.compile(r"#\s*type:\s*ignore|#\s*noqa|#\s*mypy:\s*ignore")
PY_ANY = re.compile(r":\s*Any\b|->\s*Any\b|cast\(\s*Any")


def rule_for_pr(root: Path, pr_number: int, branch: str) -> tuple[str, int]:
    """Which gate does this PR claim to close?

    Read from the session record that opened it. Falling back to a default would
    mean verifying the wrong rule and reporting PASS -- a check that is confidently
    wrong is worse than one that is absent, so an unknown PR is an error.
    """
    import sqlite3

    con = sqlite3.connect(root / "ratchet" / "state.db")
    con.row_factory = sqlite3.Row
    row = con.execute(
        "SELECT rule FROM sessions WHERE pr_url LIKE ? ORDER BY updated_at DESC LIMIT 1",
        (f"%/pull/{pr_number}",),
    ).fetchone()
    if row:
        return row["rule"], RULE_BASELINE[row["rule"]]
    raise SystemExit(
        f"no session recorded for PR #{pr_number} (branch {branch}); refusing to "
        f"guess which rule it closes"
    )


class Verdict:
    def __init__(self):
        self.checks: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))

    @property
    def passed(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    def report(self) -> str:
        lines = []
        for name, ok, detail in self.checks:
            mark = "PASS" if ok else "FAIL"
            lines.append(f"  [{mark}] {name}" + (f" -- {detail}" if detail else ""))
        lines.append(f"\n  VERDICT: {'PASS' if self.passed else 'FAIL'}")
        return "\n".join(lines)


def sh(cmd: list[str], cwd: Path, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=check)


def mirror_node_modules(src: Path, dst: Path, repo: Path, wt: Path) -> None:
    """Reproduce node_modules in the worktree without breaking module identity.

    Symlinking the whole directory is the obvious move and it is wrong. npm
    workspaces install the repo's own packages as *relative* symlinks --
    `node_modules/@superset-ui/core -> ../../packages/superset-ui-core` -- and a
    relative link resolves against its real location. Through a symlinked
    node_modules that is the source clone, not the worktree.

    Jest then maps `@superset-ui/core` to `<rootDir>/node_modules/@superset-ui/
    core/src`, which lands in the source clone, while `@apache-superset/core`
    maps straight to `<rootDir>/packages/...`, which lands in the worktree. Two
    copies of the same module, two module registries, two singletons -- and
    `SupersetClient.configure()` in the test bootstrap configures the one the
    component under test is not using:

        You must call SupersetClient.configure(...) before calling other methods

    Every test in the suite fails, identically on a correct PR and on unmodified
    master, which makes it look like the change broke them.

    So: real directories, per-entry symlinks, and any link that points back into
    the repository is re-pointed at the worktree's copy. Lint and type-check were
    never affected -- they have no singletons -- which is exactly why this was
    invisible until a test actually ran.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        target = dst / entry.name
        if target.exists() or target.is_symlink():
            continue
        if entry.is_symlink():
            real = entry.resolve()
            try:
                target.symlink_to(wt / real.relative_to(repo))  # workspace self-link
            except ValueError:
                target.symlink_to(real)  # genuinely external
        elif entry.is_dir() and entry.name.startswith("@"):
            mirror_node_modules(entry, target, repo, wt)  # scope dir may hold self-links
        else:
            target.symlink_to(entry)


def make_worktree(repo: Path, ref: str, dest: Path, share_build: bool,
                  need_node: bool = True) -> Path:
    """Verify on a throwaway worktree so the measurement clone is never touched.

    Two categories of directory are linked in rather than regenerated, because
    both are *environment* rather than code -- neither appears in any diff:

      * `node_modules`. A fresh `npm ci` per verification would cost minutes and,
        worse, could resolve a different tree than the lockfile-pinned binary the
        baseline was measured with.

      * `lib/` and `esm/` under `packages/` and `plugins/`. Superset's tsconfig
        uses project references, so `npm run type` fails with TS6305 unless the
        referenced packages have been built. CI handles this by running
        `npm run plugins:build` first; sharing the built output is the same thing
        without paying for it on every PR.

    `share_build` is false when the pull request touches `packages/` or
    `plugins/` source. Sharing built output would then be actively wrong twice
    over: the linked `lib/` would be stale relative to the changed source, and a
    rebuild inside the worktree would write back through the symlink into the
    measurement clone.
    """
    if dest.exists():
        sh(["git", "worktree", "remove", "--force", str(dest)], repo)
        shutil.rmtree(dest, ignore_errors=True)
    sh(["git", "worktree", "add", "--detach", str(dest), ref], repo, check=True)

    def link(src: Path) -> None:
        dst = dest / src.relative_to(repo)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            dst.symlink_to(src)

    if not need_node:
        return dest

    for nm in sorted(repo.glob("**/node_modules")):
        if any(p.name == "node_modules" for p in nm.relative_to(repo).parents):
            continue
        mirror_node_modules(nm, dest / nm.relative_to(repo), repo, dest)

    if share_build:
        fe = repo / "superset-frontend"
        for parent in ("packages", "plugins"):
            for out in sorted((fe / parent).glob("*/lib")) + sorted((fe / parent).glob("*/esm")):
                link(out)
    return dest


def test_files_for(changed: list[str], wt: Path) -> tuple[list[str], list[str]]:
    """Map changed sources to the tests that cover them.

    Passing a source path to Jest matches nothing -- Jest's argument is a regex
    over *test* paths -- so the naive version of this check reported "0 matches"
    and read as a pass to anyone not looking closely. Silence is not success.
    Returns (tests found, sources with no test).
    """
    fe = wt / "superset-frontend"
    tests: list[str] = []
    uncovered: list[str] = []
    for path in changed:
        if not path.startswith("superset-frontend/"):
            continue
        rel = Path(path.split("/", 1)[1])
        if any(s in rel.name for s in (".test.", ".stories.", "_spec.")):
            tests.append(str(rel))
            continue
        if rel.suffix not in {".ts", ".tsx", ".js", ".jsx"}:
            continue
        found = [
            str(rel.with_name(rel.stem + suffix))
            for suffix in (".test.tsx", ".test.ts", ".test.jsx", ".test.js")
            if (fe / rel.with_name(rel.stem + suffix)).exists()
        ]

        # Superset names a directory's entry point `index.tsx` and its test after
        # the *component*, e.g. `TaskList/index.tsx` is covered by
        # `TaskList/TaskList.test.tsx`. Matching only on the stem finds nothing
        # there and skips the suite silently -- which is how this check came to
        # run zero tests against a pull request that had five passing ones.
        # Falling back to every test in the same directory also catches
        # collateral damage to siblings, which is worth the extra runtime.
        if not found:
            found = [
                str(rel.parent / p.name)
                for p in sorted((fe / rel.parent).glob("*.test.*"))
            ]

        if found:
            tests.extend(found)
        else:
            uncovered.append(str(rel))
    return sorted(set(tests)), uncovered


def verify_mypy(v, root: Path, repo: Path, wt: Path, pr: dict, changed: list[str],
                rule: str, baseline: int, args) -> int:
    """Verify a Python type-suppression PR.

    The frontend oracle does not transfer. A dead `# type: ignore` is removed by
    deleting a comment, so "the finding is gone" is trivially satisfiable -- by
    `sed`, by deleting the line the comment sat on, or by removing the code
    entirely. What separates a fix from vandalism is what mypy says about
    *everything else* afterwards.

    Hence check 3, which has no frontend equivalent: the count of errors that are
    NOT unused-ignore must stay at zero. A suppression that was actually
    load-bearing turns into a real type error the moment it is deleted, and that
    error is the whole reason this task needs judgement rather than a regex.
    """
    diff = sh(["git", "diff", f"{pr['baseRefName']}...{pr['headRefOid']}", "--unified=0"],
              repo).stdout
    added = [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]

    v.add("no gate config modified", "pyproject.toml" not in changed,
          "pyproject.toml changed" if "pyproject.toml" in changed else "")

    supp = [ln.strip() for ln in added if PY_SUPPRESSION.search(ln)]
    v.add("no new suppressions added", not supp, f"{len(supp)}: {supp[:2]}" if supp else "")

    anyish = [ln.strip() for ln in added if PY_ANY.search(ln)]
    v.add("no widening to Any", not anyish, f"{len(anyish)}: {anyish[:2]}" if anyish else "")

    areas = {area_of(p) for p in changed if p.startswith("superset/")}
    v.add("changes confined to one area", len(areas) <= 1, ", ".join(sorted(areas)))

    findings = run_mypy(wt)
    target = sorted(areas)[0] if areas else None
    in_area = [f for f in findings if area_of(f.path) == target]
    v.add(f"unused-ignore clean in {target}", not in_area, f"{len(in_area)} remain")
    v.add("repo-wide count dropped, nothing regressed",
          len(findings) < baseline, f"{rule}: {baseline} -> {len(findings)}")

    # The check that matters. Anything that is not an unused-ignore is an error
    # this PR introduced by removing a suppression that was doing real work.
    others = [f for f in run_mypy(wt, code=None) if f.rule != "unused-ignore"]
    v.add("no new type errors of any other kind", not others,
          f"{len(others)}: {[f'{f.path}:{f.line} [{f.rule}]' for f in others[:3]]}" if others else "")

    print(v.report())
    if not args.keep:
        sh(["git", "worktree", "remove", "--force", str(wt)], repo)
    return 0 if v.passed else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--keep", action="store_true", help="leave the worktree in place")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[2]
    env = load_env(root)
    repo_name = env["FORK_REPO"]
    repo = (root / "superset-adham-clone").resolve()

    pr = json.loads(sh(
        ["gh", "pr", "view", str(args.pr), "--repo", repo_name, "--json",
         "number,title,headRefName,headRefOid,baseRefName,files,body,state"],
        root, check=True).stdout)

    print(f"PR #{pr['number']}: {pr['title']}")
    print(f"  branch {pr['headRefName']} @ {pr['headRefOid'][:10]} -> {pr['baseRefName']}")

    sh(["git", "fetch", "origin", f"pull/{args.pr}/head"], repo, check=True)

    # Outside the project directory on purpose. A git worktree placed next to the
    # repository is a second full checkout of ~10k files, which editors index as
    # a separate repository -- and when it is torn down they report every one of
    # those files as a deletion. The verification scratch space should not be
    # visible to anything but the verifier.
    wt_path = Path(tempfile.gettempdir()) / f"ratchet-verify-{repo.name}"

    rule, baseline = rule_for_pr(root, args.pr, pr["headRefName"])
    is_mypy = rule in MYPY_RULES

    changed = [f["path"] for f in pr["files"]]
    touches_pkg = any(
        p.startswith(("superset-frontend/packages/", "superset-frontend/plugins/"))
        for p in changed
    )
    # A Python change needs no JavaScript toolchain. Mirroring 1,621 node_modules
    # entries for it would be several seconds of pure waste per verification.
    wt = make_worktree(repo, pr["headRefOid"], wt_path,
                       share_build=not touches_pkg, need_node=not is_mypy)

    v = Verdict()
    print(f"  {len(changed)} files changed  gate={rule}"
          + ("  (touches packages/plugins -- rebuilding, not sharing build output)"
             if touches_pkg else "") + "\n")

    if is_mypy:
        return verify_mypy(v, root, repo, wt, pr, changed, rule, baseline, args)

    # --- 1. scope -----------------------------------------------------------
    touched_config = sorted(set(changed) & GATE_CONFIG)
    v.add("no gate config modified", not touched_config,
          ", ".join(touched_config) if touched_config else "")

    areas = {area_of(p) for p in changed if p.startswith("superset-frontend/")}
    v.add("changes confined to one area", len(areas) <= 1, ", ".join(sorted(areas)))

    # --- 2. no suppressions in the added lines ------------------------------
    diff = sh(["git", "diff", f"{pr['baseRefName']}...{pr['headRefOid']}", "--unified=0"],
              repo).stdout
    added = [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    supp = [ln.strip() for ln in added if SUPPRESSION.search(ln)]
    v.add("no suppression comments added", not supp, f"{len(supp)} found: {supp[:2]}" if supp else "")

    idx = [ln.strip() for ln in added if INDEX_KEY.search(ln)]
    v.add("no index-as-key", not idx, f"{len(idx)} found: {idx[:2]}" if idx else "")

    net = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---")) - len(added)
    v.add("code not merely deleted", net < 40, f"net {net} lines removed")

    # --- 3. the gate itself, measured by us ---------------------------------
    #
    # The rule comes from the session that produced this PR, not from a default.
    # A hardcoded fallback here would quietly verify the wrong rule for every
    # workstream but one -- and it would report PASS while doing it, which is
    # worse than reporting nothing.
    rule, baseline = rule_for_pr(root, args.pr, pr["headRefName"])
    findings = run_oxlint(wt, rule)
    target_area = sorted(areas)[0] if areas else None
    in_area = [f for f in findings if area_of(f.path) == target_area]
    v.add(f"{rule} clean in {target_area}", not in_area, f"{len(in_area)} remain")

    v.add("repo-wide count dropped, nothing regressed",
          len(findings) < baseline, f"{rule}: {baseline} -> {len(findings)}")

    # --- 4. nothing else broke ----------------------------------------------
    fe = wt / "superset-frontend"

    # CI runs `npm i && npm run plugins:build && npm run type`. Without the build
    # step, tsconfig's project references make tsc fail with TS6305 on a clean
    # master -- so a naive type check here would report failure for every PR
    # including correct ones. When the PR touches packages/ or plugins/ we cannot
    # share the prebuilt output, so we pay for the rebuild.
    if touches_pkg:
        build = sh(["npm", "run", "plugins:build"], fe)
        v.add("packages build", build.returncode == 0,
              (build.stdout + build.stderr)[-200:] if build.returncode else "")

    tsc = sh(["npm", "run", "type"], fe)
    v.add("type-check passes", tsc.returncode == 0,
          (tsc.stdout + tsc.stderr).strip()[-300:] if tsc.returncode else "")

    tests, uncovered = test_files_for(changed, wt)
    if tests:
        jest = sh(["npm", "run", "test", "--", *tests], fe)
        v.add(f"tests pass ({len(tests)} suites)", jest.returncode == 0,
              (jest.stderr or jest.stdout).strip()[-300:] if jest.returncode else "")
    if uncovered:
        # Reported, never fatal. A source file with no sibling test is a fact
        # about Superset's coverage, not a defect in this pull request -- but it
        # must be visible, because "tests passed" over zero suites is the exact
        # shape of a check that looks green while verifying nothing.
        print(f"  [note] no sibling test for: {', '.join(uncovered[:4])}")

    print(v.report())

    if not args.keep:
        sh(["git", "worktree", "remove", "--force", str(wt)], repo)
    return 0 if v.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
