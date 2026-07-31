# Adding a gate

A **gate** is a checker plus an enforcement switch. Adding one means teaching the
system to measure a new kind of debt; everything after measurement — grouping,
issue filing, the Devin prompt, independent verification, the ratchet — already
works on normalized findings and does not care which tool produced them.

That abstraction holds. What does not hold is the configuration, which is spread
across six tables that should be one registry. This document is honest about
both: the interface is one function, the wiring is nine edits.

The worked example is **zizmor**, because it is the gate I would add next — see
[Why zizmor is the right next one](#why-zizmor-is-the-right-next-one).

---

## The contract

Everything in the system speaks one shape, defined in
[`ratchet/detector/gates.py`](../ratchet/detector/gates.py):

```python
@dataclass(frozen=True)
class Finding:
    gate: str      # oxlint | mypy | zizmor
    rule: str      # react/jsx-key
    path: str      # repo-relative, ALWAYS
    line: int
    column: int
    message: str
```

A gate runner is any function that returns `list[Finding]`. If your tool can
produce that, everything downstream is already written.

Two rules about `path`, both of which have cost real time:

- **Repo-relative, always.** oxlint reports paths relative to
  `superset-frontend/`, so `run_oxlint` re-roots them
  ([gates.py:72-74](../ratchet/detector/gates.py#L72)). If you skip this, grouping
  puts findings in the wrong area and the verifier's scoped check passes
  vacuously.
- **Real paths only.** The grouping layer derives ownership from the path, so a
  synthetic or tool-internal path silently lands in the `misc` bucket.

---

## Step 1 — write the runner

One function in `gates.py`. Run the tool, parse its output, return findings.

```python
# --- zizmor -----------------------------------------------------------------
#
# Superset runs zizmor as a pre-commit hook with --no-exit-codes, and the config
# says why: "Advisory until pre-existing findings are resolved; remove
# --no-exit-codes to make this hook blocking." That is the same deadlock this
# whole system attacks, written down by a maintainer.

ZIZMOR_BIN = "zizmor"


def run_zizmor(repo_root: Path, rule: str) -> list[Finding]:
    """Measure one zizmor audit across the repository's workflows.

    Pinned to the version the hook pins. An auditor that gains a check between
    versions reports more findings on unchanged code, which would look like a
    regression and fail the ratchet for no reason.
    """
    proc = subprocess.run(
        [ZIZMOR_BIN, "--format", "json", ".github/workflows"],
        cwd=repo_root, capture_output=True, text=True,
    )
    if not proc.stdout.strip():
        raise RuntimeError(f"zizmor produced no output: {proc.stderr[:400]}")

    findings = []
    for audit in json.loads(proc.stdout):
        if audit["ident"] != rule:          # one audit per workstream, isolated
            continue
        for loc in audit.get("locations", []):
            given = loc["symbolic"]["key"].get("Local", {}).get("given_path")
            if not given:                   # remote/synthetic location, not ours
                continue
            point = loc["concrete"]["location"]["start_point"]
            findings.append(
                Finding(
                    gate="zizmor",
                    rule=audit["ident"],
                    path=given,             # already repo-relative
                    line=point["row"] + 1,  # zizmor rows are 0-indexed
                    column=point["column"],
                    message=loc["symbolic"]["annotation"] or audit["desc"],
                )
            )
    return findings
```

That parser is written against the real output committed at
[`baseline/zizmor/zizmor-1.25.2-default-persona.json`](../baseline/zizmor/zizmor-1.25.2-default-persona.json) —
top-level list, `ident` / `desc` / `locations[]`, with the path under
`locations[].symbolic.key.Local.given_path` and the position under
`locations[].concrete.location.start_point`. It was run against both committed
personas before being written down here: `dangerous-triggers` yields exactly one
finding, `.github/workflows/labeler.yml:2`.

> **One audit is not one finding.** zizmor attaches several locations to a single
> audit — `template-injection` emits *this step*, *this run block* and *the
> expansion* separately, so 24 audits became 48 findings in the pedantic run.
> Decide deliberately whether that is three findings or one, because whatever you
> choose becomes the ceiling the ratchet enforces. The oxlint and mypy runners
> never had to make this call: they emit one diagnostic per site.

### Isolate the rule, and pin the tool

Both existing runners measure **one rule at a time**, and you should too:
a count that moves because an unrelated rule changed is not a baseline.

`run_oxlint` does this with `-A all -D <rule>`. zizmor has no equivalent flag, so
the runner filters after the fact — same outcome, and no risk of
[Trap 3](#traps-that-have-already-cost-time).

---

## Step 2 — wire it up

Nine edits. There is no way around most of them today, and pretending otherwise
would waste your afternoon.

| # | File | Change | Required |
|---|---|---|---|
| 1 | `detector/gates.py` | the runner above | yes |
| 2 | `detector/detect.py` [`WORKSTREAM_RULES`](../ratchet/detector/detect.py#L33) | `"H": ("zizmor", "dangerous-triggers")` | yes |
| 3 | `detector/detect.py` [line 102](../ratchet/detector/detect.py#L102) | the dispatch is a hardcoded ternary — extend it | yes |
| 4 | `orchestrator/run.py` [`WORKSTREAM_RULES`](../ratchet/orchestrator/run.py#L29) | **the same table again** | yes |
| 5 | `orchestrator/verify.py` [`RULE_BASELINE`](../ratchet/orchestrator/verify.py#L61) | `"dangerous-triggers": 1` | yes |
| 6 | `orchestrator/ratchet.py` [`GATES`](../ratchet/orchestrator/ratchet.py#L50) | baseline, kind, switch, note | yes |
| 7 | `orchestrator/dashboard.py` [`BASELINE`](../ratchet/orchestrator/dashboard.py#L46) | count, false positives, switch | or no panel |
| 8 | `.github/workflows/detect.yml` | matrix entry + a step installing the tool | yes |
| 9 | `detector/render.py` `GATE_SWITCH` / `ANTIPATTERNS` / `MANDATE` | see below | optional |

### Step 3 is the one that bites

```python
findings = run_mypy(repo_path) if gate == "mypy" else run_oxlint(repo_path, rule)
```

A hardcoded ternary. Adding a third tool means editing it — this is the clearest
place the design is honest-but-unfinished. A registry
(`RUNNERS = {"oxlint": run_oxlint, "mypy": run_mypy, "zizmor": run_zizmor}`)
would remove edits 2, 3 and 4 at once, and is the refactor I would do before a
fourth tool rather than a third.

### Step 5 is not optional

`verify.py` fails **by name** when a rule is missing from `RULE_BASELINE` — a
deliberate choice, because the earlier behaviour was a bare `KeyError` inside a
subprocess that surfaced as "verification FAILED" with no checks listed. A gate
that cannot be verified must not be filable.

### Step 9, and the one thing not to do

`GATE_SWITCH` and `ANTIPATTERNS` have defaults, so skipping them degrades
gracefully. `MANDATE` does not work that way:

> Only rules that actually carry a written statement appear here. Three of the
> gates this system remediates do not, and inventing a mandate for them would be
> the fastest way to lose a reviewer who opens the config.
> — [`render.py:25-27`](../ratchet/detector/render.py#L25)

**Add a `MANDATE` entry only if you can quote a real comment with a line
reference.** "The project asked for this" is a much stronger claim than "a tool
found this" precisely because it is checkable — which means a fabricated one is
worse than none.

---

## Step 3 — does it need a new prompt template?

Probably not. There are two today —
[`build`](../ratchet/orchestrator/prompt.py#L253) for oxlint and
[`build_mypy`](../ratchet/orchestrator/prompt.py#L229) — and they differ because
the *oracle* differs, not because the tool does.

Reuse `build` when the verification command is "run the tool, count findings
under this path, expect 0". That covers zizmor.

Write a new template only when the definition of done is shaped differently. Any
template must keep four things, which are the contract rather than decoration:

1. **The oracle verbatim** — the exact command, interpolated for this unit. Not
   a description. The session runs it before opening a PR.
2. **Named anti-patterns** — the specific wrong fix for *this* rule. For zizmor
   that is pinning an action to a mutable tag instead of a digest: it satisfies
   `unpinned-uses` and leaves the supply-chain hole open.
3. **Stopping is sanctioned** — verbatim. This is what turns an escalation into a
   designed outcome instead of a failure.
4. **Structured output** — the schema at
   [`devin.py:33`](../ratchet/orchestrator/devin.py#L33), so the verdict is
   machine-readable and can be independently re-checked.

---

## Step 4 — prove the baseline before filing anything

Do this before `--apply`. A wrong baseline is worse than no gate: every delta
downstream is measured against it, and the ratchet will commit it as a ceiling.

```bash
# 1. clean tree, or the number is fabricated rather than merely wrong
git -C superset-adham-clone status --porcelain     # must be empty

# 2. dry run
python ratchet/detector/detect.py --workstream H

# 3. run it twice -- identical fingerprints is the idempotency claim
python ratchet/detector/detect.py --workstream H
```

Then commit the raw tool output to `baseline/<gate>/`, as
`baseline/oxlint/`, `baseline/mypy/` and `baseline/zizmor/` already do. That file
is what makes the number auditable by someone who does not trust the code — and
if you can, commit a **control** alongside it, the way
[`gates/mypy-control.ini`](../ratchet/gates/mypy-control.ini) replicates the repo's real settings
with exactly one variable changed. A control running clean is what turns "49" from
a number a tool printed into a measurement.

### Traps that have already cost time

- **Scope changes the answer.** oxlint must run from `superset-frontend/` with no
  path arguments: `rules-of-hooks` is 47 at CI's scope and 43 if you name
  `src packages plugins`. Measure at exactly the scope CI uses, or you are
  measuring a different gate.
- **Environment changes the answer.** mypy's gate is 1.15.0 plus ten stub
  packages *and nothing else*; master is clean there, and reports 1,502 errors
  with Superset's real dependencies installed. Both numbers are real. Only one is
  the gate.
- **Trap 3: `-A all -D <rule>` drops that rule's options.** This made
  `jest/expect-expect` read 120 when it is really 0. If your rule takes options,
  isolate it a different way.
- **False positives belong in the denominator, not in your head.** 4 of the 47
  `rules-of-hooks` findings are `playwright/`'s `await use(fixture)` — a fixture
  callback, not React's hook. Record them in `dashboard.py` and `ratchet.py`, or
  the ratchet will fail a build over code that was never broken.

---

## Step 5 — the ratchet

Adding the entry at [`ratchet.py:50`](../ratchet/orchestrator/ratchet.py#L50) is
enough for **counting mode**: today's count becomes a ceiling and CI fails any PR
that raises it. That works on debt too large to ever hand-clear, and it is the
mode that matters — it does not require finishing.

**Full mode** — promoting the switch to `"error"` — fires only when the gate
reaches zero, and the tool refuses otherwise. Flipping a switch early breaks the
build for the next person to push anything at all, which is the fastest way to
get the whole idea rejected.

---

## Why zizmor is the right next one

- **It has a written mandate.** `.pre-commit-config.yaml`: *"Advisory until
  pre-existing findings are resolved; remove `--no-exit-codes` to make this hook
  blocking."* Three switches in the repository carry such a statement and this is
  the only one without a runner.
- **One finding.** It is one finding away from blocking — the most persuasive
  possible ratchet.
- **It is a security gate, not a style gate.** Unpinned actions and overbroad
  permissions are supply-chain exposure, which is a different and better
  conversation than code style.
- **The baseline already exists** at `baseline/zizmor/`, measured at two personas.

---

## Checklist

- [ ] Runner returns `list[Finding]` with **repo-relative** paths
- [ ] Rule measured in isolation, tool version pinned
- [ ] Baseline measured on a clean tree and committed to `baseline/<gate>/`
- [ ] `RULE_BASELINE` entry — verification fails by name without it
- [ ] `ratchet.py` `GATES` entry, with false positives noted
- [ ] `detect.yml` matrix entry **and** a step that installs the tool
- [ ] `MANDATE` entry only if a real comment can be quoted with a line reference
- [ ] Detector run twice: identical fingerprints, no duplicate issues
- [ ] First `--apply` on a single unit (`--only-area`) before the whole workstream
