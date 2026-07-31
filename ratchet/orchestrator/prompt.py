"""The prompt is the contract.

Four deliberate choices, each of which came out of measuring something rather
than guessing:

1. **The oracle goes in the prompt.** The session verifies itself before opening
   a pull request, instead of handing back a diff for someone else to check.

2. **Anti-patterns are named explicitly.** Both were observed. `key={index}`
   silences jsx-key while preserving the exact reconciliation bug the rule
   exists to catch. And oxlint's own `--fix` for `no-console` deleted a
   user-facing i18n warning and left an empty statement, with type-check and
   tests still green -- a wrong fix is very often the *easiest* fix to automate,
   which is the whole reason this work needs judgment rather than a codemod.

3. **Stopping is sanctioned.** "If you cannot fix this correctly, stop and
   explain why" is what turns an escalation into a designed outcome instead of a
   failure. A blocked session that names a real design question is worth more
   than a green one that hid it.

4. **Structured output.** A machine-readable verdict, which is then independently
   re-checked. The session's own claim is evidence, not proof.
"""

from __future__ import annotations

TEMPLATE = """\
## Repository

`{repo}`, branched from `master` at `{sha}`.

Superset's own conventions are in `AGENTS.md` and `CLAUDE.md` at the repo root.
Read them and follow them -- in particular: no `any` types, functional
components, and import UI components from `@superset-ui/core`.

## Task

There are {n} `{rule}` violations in `{area}` ({nfiles} files). This rule is
checked on every CI run and enforced on none of them, because `npm run lint`
passes `--quiet`, which hides findings but still counts them.

{findings}

## Definition of done

Run these yourself and confirm each one before opening a pull request. All paths
are relative to `superset-frontend/`. Use the lockfile-pinned binary at
`./node_modules/.bin/oxlint`, **not** `npx` -- npx may resolve a different
version than the lockfile and give you a different answer than CI.

```bash
cd superset-frontend

# 1. Every file in this task is clean.
./node_modules/.bin/oxlint --config oxlint.json -A all -D {rule} --format json \\
  | jq '[.diagnostics[] | select({scope_filter})] | length'
# must print 0

# 2. Nothing else regressed. The repo-wide count for this rule must be exactly
#    {after}, down from {before}. Higher means you broke something; lower means
#    you changed files outside the scope of this task.
./node_modules/.bin/oxlint --config oxlint.json -A all -D {rule} --format json \\
  | jq '.diagnostics | length'

# 3. Types still check.
npm run type

# 4. Tests still pass for what you touched.
npm run test -- {test_targets}
```

## Constraints

{antipatterns}
- Fix the cause, not the symptom.
- Do not modify `oxlint.json`, and do not add any lint-disable or `@ts-ignore`
  comment. Changing the rule's configuration is a failed session, not a fix.
- Do not touch files outside `{area}`.
- If a correct fix requires changing runtime behaviour, make the change and state
  it explicitly in the pull request body. Do not hide it.
- **If you cannot fix something correctly, stop and explain why.** A blocked
  session is a valid, expected outcome and a human will review it. A fix that
  passes the linter while preserving the defect is not a valid outcome.

## Pull request

Open a PR against `master` of `{repo}`. Follow `.github/PULL_REQUEST_TEMPLATE.md`
and Conventional Commits, e.g. `fix(components): add stable keys to error message lists`.
State in the body which of the four checks above you ran and what they printed.

## Output

Conform to the provided structured output schema. `gate_closed` must be true only
if check 1 actually printed 0 when you ran it.
"""

ANTIPATTERNS = {
    "react/jsx-key": [
        "**`key={index}` is not an acceptable fix.** Keying by array position is the "
        "precise behaviour this rule exists to prevent: when the list is reordered or "
        "filtered, React reuses the wrong DOM node and renders stale content against the "
        "wrong label. Find a stable unique identifier in the data being mapped and key on "
        "that. If no stable identifier exists, say so rather than falling back to the index.",
    ],
    "react-hooks/rules-of-hooks": [
        "Do not fix a conditional hook by moving the condition inside the hook if that "
        "changes when effects fire. Preserve the existing behaviour or state the change.",
    ],
    "@typescript-eslint/ban-ts-comment": [
        "Removing the suppression and leaving a type error is not a fix. Neither is "
        "replacing it with `any`, or with a cast that asserts something you have not "
        "verified. If the underlying type is genuinely wrong or missing, fixing the type "
        "definition is the fix.",
    ],
    "default": ["Do not suppress the rule. Suppression is a failed session, not a fix."],
}


MYPY_TEMPLATE = """\
## Repository

`{repo}`, branched from `master` at `{sha}`.

Superset's Python conventions are in `AGENTS.md` and `CLAUDE.md` at the repo root.

## Task

`{area}` contains {n} **dead `# type: ignore` comments** -- suppressions for type
errors that no longer occur. Each one is a silent liability: it suppresses not
only the error it was written for, but any future error on that line.

They are invisible today because `pyproject.toml` switches the check off for the
modules that contain them:

```toml
# Disable warn_unused_ignores for modules with dynamic type assignments
[[tool.mypy.overrides]]
module = [ ... {area_module} ... ]
warn_unused_ignores = false
```

{findings}

## Definition of done

### ⚠️ Read this first -- mypy's answer depends on what is installed

Superset's pre-commit runs mypy in an **isolated environment containing mypy and
ten stub packages and nothing else**. In that environment master is clean across
1,423 files. If you install Superset's real dependencies, the same command on the
same commit reports over 1,500 errors that have nothing to do with this task.

**Both numbers are real. Only the isolated one is the gate**, because that is what
CI runs. Build it exactly like this and use it for every check below:

```bash
python3.11 -m venv /tmp/mypy-gate
/tmp/mypy-gate/bin/pip install "mypy==1.15.0" \\
  types-cachetools types-simplejson types-python-dateutil types-requests \\
  types-pytz types-croniter types-PyYAML types-setuptools types-paramiko \\
  types-Markdown
```

### The checks

The gate config is `pyproject.toml`'s mypy settings with exactly one change: the
`warn_unused_ignores = false` override is removed. Write it verbatim to
`/tmp/gate.ini` -- do not regenerate it, the point is that it differs from the
repo's real settings in one variable and nothing else:

```bash
cat > /tmp/gate.ini <<'GATE_INI'
{config}
GATE_INI
```

```bash

# 1. No dead ignores remain in {area}.
/tmp/mypy-gate/bin/mypy --config-file /tmp/gate.ini --check-untyped-defs superset/ \\
  2>&1 | grep '\\[unused-ignore\\]' | grep -E '{mypy_scope}' | wc -l
# must print 0

# 2. Nothing else regressed. Repo-wide unused-ignore count must be exactly
#    {after}, down from {before}.
/tmp/mypy-gate/bin/mypy --config-file /tmp/gate.ini --check-untyped-defs superset/ \\
  2>&1 | grep -c '\\[unused-ignore\\]'

# 3. No NEW errors of any other kind. This is the one that catches a bad fix:
#    deleting an ignore that was actually load-bearing turns one dead comment
#    into one real type error.
/tmp/mypy-gate/bin/mypy --config-file /tmp/gate.ini --check-untyped-defs superset/ \\
  2>&1 | grep -v '\\[unused-ignore\\]' | grep -c 'error:'
# must print 0

# 4. Tests still pass for what you touched.
pytest tests/unit_tests/ -q
```

## Constraints

- **Deleting the comment is usually right, but verify it -- do not assume.** An
  ignore reported as unused under this config may still be doing work under a
  different one. Check 3 exists precisely to catch that.
- Do not add `# type: ignore` anywhere, and do not widen a type to `Any` to make
  an error go away. Both are the same failure as the comment you are removing.
- Do not edit `pyproject.toml`. Changing the gate's configuration is a failed
  session, not a fix. Removing the override is a separate, deliberate change that
  happens only after this work lands.
- Do not touch files outside `{area}`.
- **If you cannot fix something correctly, stop and explain why.** A blocked
  session is a valid, expected outcome and a human will review it. If a
  suppression is load-bearing and removing it requires a design change, say so
  rather than forcing it.

## Pull request

Open a PR against `master` of `{repo}`, Conventional Commits, e.g.
`chore(types): remove dead type-ignore comments in {area}`. State which checks you
ran and what they printed.

## Output

Conform to the provided structured output schema. `gate_closed` must be true only
if check 1 actually printed 0 when you ran it.
"""


def build_mypy(*, repo: str, sha: str, area: str, findings: list, before: int,
               config_text: str) -> str:
    by_file: dict[str, list] = {}
    for f in findings:
        by_file.setdefault(f.path, []).append(f)
    blocks = []
    for path in sorted(by_file):
        rows = "\n".join(f"  line {f.line}: {f.message}"
                         for f in sorted(by_file[path], key=lambda f: f.line))
        blocks.append(f"`{path}`\n{rows}")

    # Same trap as the oxlint side: `^misc` matches no path, so the scoped check
    # would pass without testing anything. Anchor on the real files instead.
    mypy_scope = ("^(" + "|".join(sorted(by_file)) + ")") if area == "misc" else f"^{area}"

    return MYPY_TEMPLATE.format(
        repo=repo, sha=sha, area=area, n=len(findings), mypy_scope=mypy_scope,
        area_module=area.replace("/", "."),
        findings="\n\n".join(blocks),
        before=before, after=before - len(findings),
        config=config_text,
    )


def build(*, repo: str, sha: str, rule: str, area: str, findings: list, before: int) -> str:
    by_file: dict[str, list] = {}
    for f in findings:
        by_file.setdefault(f.path, []).append(f)

    blocks = []
    for path in sorted(by_file):
        rows = "\n".join(
            f"  line {f.line}, col {f.column}: {f.message}"
            for f in sorted(by_file[path], key=lambda f: f.line)
        )
        blocks.append(f"`{path}`\n{rows}")
    findings_md = "\n\n".join(blocks)

    anti = ANTIPATTERNS.get(rule, ANTIPATTERNS["default"])
    anti_md = "\n".join(f"- {a}" for a in anti)

    # Jest is run from superset-frontend/, so strip that prefix off the paths.
    targets = " ".join(sorted({p.split("/", 1)[1] for p in by_file}))

    # A path prefix is the right scope for a real area, and *meaningless* for the
    # `misc` bucket -- there is no `misc/` directory, so `startswith("misc/")`
    # matches nothing and check 1 passes without testing anything. G1 reported
    # `gate_closed: true` having fixed 1 of 2 findings for exactly this reason,
    # and it was not wrong: the check it was handed was vacuous. Enumerate the
    # files instead.
    rel_files = sorted(p.split("/", 1)[1] for p in by_file)
    if area == "misc":
        listed = ",".join(f'"{f}"' for f in rel_files)
        scope_filter = f".filename | IN({listed})"
    else:
        scope_filter = f'.filename | startswith("{area}/")'

    return TEMPLATE.format(
        repo=repo,
        sha=sha,
        n=len(findings),
        nfiles=len(by_file),
        rule=rule,
        area=area,
        findings=findings_md,
        before=before,
        after=before - len(findings),
        test_targets=targets,
        antipatterns=anti_md,
        scope_filter=scope_filter,
    )
