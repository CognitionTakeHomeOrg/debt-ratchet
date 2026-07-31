"""Render an issue unit into GitHub issue text.

Two audiences read this body and they need different things from it:

  * A human reviewer needs to know why the issue exists, what the enforcement
    switch is, and what flipping it unblocks.
  * Devin needs the exact findings, the exact command that decides whether the
    work is done, and explicit statements of what does NOT count as a fix.

The second audience is why the verification command is embedded verbatim rather
than described. The agent is expected to run its own oracle before opening a PR;
handing it prose to interpret would make that step lossy.
"""

from __future__ import annotations

from grouping import IssueUnit

# Where each rule's enforcement switch lives, and what turning it on requires.
GATE_SWITCH = {
    "react/jsx-key": (
        "superset-frontend/oxlint.json",
        "not configured -- inherits oxlint's default `warn`",
        'add `"react/jsx-key": "error"`',
    ),
    "react-hooks/rules-of-hooks": (
        "superset-frontend/oxlint.json#L183",
        '`"warn"`',
        'change to `"error"`',
    ),
    "react/no-unstable-nested-components": (
        "superset-frontend/oxlint.json#L179",
        '`"warn"`',
        'change to `"error"`',
    ),
    "@typescript-eslint/ban-ts-comment": (
        "superset-frontend/oxlint.json#L225",
        '`"off"`',
        'change to `"error"`',
    ),
}

# Fixes that satisfy the linter while preserving the defect. Each was observed,
# not imagined: `key={index}` is the canonical jsx-key cop-out, and oxlint's own
# `--fix` for no-console deleted a user-facing i18n warning and left an empty
# statement behind while type-check and tests both stayed green.
ANTIPATTERNS = {
    "react/jsx-key": [
        "`key={index}` is **not** an acceptable fix. Keying by array position is exactly "
        "the behaviour the rule exists to prevent -- it silences the warning and keeps the "
        "reconciliation bug. Use a stable identifier from the data.",
        "Do not add `// eslint-disable` or `// oxlint-disable` comments.",
    ],
    "default": ["Do not suppress the rule. Suppression is a failed session, not a fix."],
}


def render(unit: IssueUnit, repo: str, sha: str) -> tuple[str, str]:
    switch_file, switch_now, switch_todo = GATE_SWITCH.get(
        unit.rule, ("superset-frontend/oxlint.json", "non-blocking", "promote to `error`")
    )

    by_file: dict[str, list] = {}
    for f in unit.findings:
        by_file.setdefault(f.path, []).append(f)

    lines = [f"| file | line | finding |", "|---|---|---|"]
    for path in sorted(by_file):
        for f in sorted(by_file[path], key=lambda f: f.line):
            lines.append(f"| `{path}` | {f.line} | {f.message} |")
    findings_table = "\n".join(lines)

    anti = ANTIPATTERNS.get(unit.rule, ANTIPATTERNS["default"])
    anti_md = "\n".join(f"- {a}" for a in anti)

    rel = unit.findings[0].path.split("/", 1)[1].rsplit("/", 1)[0] if unit.findings else ""

    title = (
        f"[{unit.ident}] {unit.rule}: {len(unit.findings)} findings in "
        f"`{unit.area}` ({len(unit.files)} files)"
    )

    body = f"""<!-- ratchet-fingerprint: {unit.fingerprint} -->
**Gate:** `{unit.rule}` &nbsp;·&nbsp; **Enforcement today:** {switch_now} &nbsp;·&nbsp; **Findings:** {len(unit.findings)} across {len(unit.files)} files

This rule is checked on every CI run and enforced on none of them. `npm run lint`
passes `--quiet`, so the findings are counted and then discarded. Clearing this
area is one of the prerequisites for flipping the switch.

## Findings

{findings_table}

## Definition of done

Verify these yourself before opening a pull request. All commands run from
`superset-frontend/`, using the lockfile-pinned binary -- not `npx`.

```bash
# 1. this area is clean
./node_modules/.bin/oxlint --config oxlint.json -A all -D {unit.rule} --format json \\
  | jq '[.diagnostics[] | select(.filename | startswith("{unit.area}/"))] | length'
# must print 0

# 2. nothing else regressed -- repo-wide count must DROP by {len(unit.findings)}, not just change
./node_modules/.bin/oxlint --config oxlint.json -A all -D {unit.rule} --format json \\
  | jq '.diagnostics | length'
# baseline for this rule is recorded in baseline/ at {sha[:10]}

# 3. types still check
npm run type

# 4. tests still pass for the touched files
npm run test -- <touched files>
```

## Constraints

{anti_md}
- Fix the cause, not the symptom.
- If a correct fix requires a behaviour change, say so explicitly in the PR body.
- **If you cannot fix something correctly, stop and explain why.** A blocked session
  is a valid outcome and will be reviewed by a human. A wrong fix that passes the
  linter is not a valid outcome.

## What this unblocks

Once every `{unit.rule}` finding in the repository is cleared, `{switch_file}`
can {switch_todo} and this class of defect becomes impossible to reintroduce.
That ratchet PR is tracked separately and is the point of this work -- cleanup
alone decays, the enforcement switch does not.

---
<sub>Filed automatically by the debt-ratchet detector against `{repo}` at `{sha[:10]}`.</sub>
"""
    return title, body
