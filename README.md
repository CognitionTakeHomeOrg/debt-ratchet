# Debt Ratchet

**Apache Superset's CI is green while 1,453 real findings sit in the codebase — because every gate that would catch them is switched off, and nobody can afford to switch them on.**

This system pays the debt down with Devin, then flips the gates permanently.

The number is not an estimate. It is Superset's own CI log for commit `9f5611aca5`, job `lint-frontend`, result **success**:

```
> npx oxlint --config oxlint.json --quiet
Found 1453 warnings and 0 errors.
```

`--quiet` hides the findings. It does not stop them being counted.

---

## The problem

A quality gate is two things: a **checker** and an **enforcement switch**. Superset has built the checkers and dialled every switch to non-blocking, each with the existing debt given as the reason:

| Gate | Enforcement today | Findings | Where |
|---|---|---|---|
| `react-hooks/rules-of-hooks` | `"warn"` | 47 | `superset-frontend/oxlint.json` |
| `react/no-unstable-nested-components` | `"warn"` | 150 | `superset-frontend/oxlint.json` |
| `react/jsx-key` | not configured → default `warn` | 81 | — |
| `@typescript-eslint/ban-ts-comment` | `"off"` | 197 | `superset-frontend/oxlint.json` |
| mypy `warn_unused_ignores` | disabled for 8 modules | 49 | `pyproject.toml` |
| zizmor (Actions security) | `--no-exit-codes` | 1 | `.pre-commit-config.yaml` |

The `zizmor` config states the deadlock outright:

```yaml
# Advisory until pre-existing findings are resolved; remove
# --no-exit-codes to make this hook blocking.
```

Too much debt to turn the gate on. No gate, so more debt accumulates.

**A ratchet is a toothed wheel that turns one way and cannot turn back.** Cleanup alone decays — clear a rule today and the count regrows. Promoting the enforcement switch is what makes the work permanent, and it is the deliverable this system exists to produce.

---

## Quickstart

```bash
git clone <this repo> && cd debt-ratchet
cp .env.example .env          # add DEVIN_API_KEY, DEVIN_ORG_ID, FORK_REPO
docker compose up
```

- Dashboard → <http://localhost:8100>
- Webhook receiver → <http://localhost:8099/webhook>, health at `/health`

### No API key? Run it anyway

```bash
docker compose --profile simulate up
```

Simulate mode replays recorded sessions from `ratchet/fixtures/`. No credentials, no spend, same pipeline.

---

## How it works

```
   gate (oxlint / mypy / zizmor)
        │  measured on a clean tree, at CI's exact scope
        ▼
   detector ──► GitHub issue          ◄── the event
        │        (one per reviewable unit, idempotent)
        ▼
   webhook ──► orchestrator ──► Devin session
        │         budget + concurrency caps, enforced before creation
        ▼
   independent verification ──► PR ──► merge
        │         our container, our oracle, not the session's self-report
        ▼
   ratchet PR: switch → "error"       ◄── the point
```

### 1. Detection

One command per rule, so a count never drifts because something unrelated changed:

```bash
oxlint --config oxlint.json -A all -D react/jsx-key --format json
```

*Disable every rule, enable exactly one, answer in JSON.*

Two environment traps are encoded, because both produce confidently wrong numbers:

- **oxlint must run from `superset-frontend/` with no path arguments**, using the lockfile-pinned binary. Naming paths under-reports — `rules-of-hooks` is 47 at CI scope and 43 if you pass `src packages plugins`.
- **mypy's answer depends on what is installed.** Pre-commit runs it in an isolated venv of mypy plus ten stub packages and nothing else; there, master is clean. With Superset's real dependencies installed the same commit reports 1,502 errors. Both are true. Only the first is the gate.

The detector also refuses to measure a dirty working tree. Every number here is a delta against a committed baseline, so uncommitted edits don't produce a wrong number — they produce a fabricated one.

### 2. Grouping

81 findings filed as 81 issues would mean 81 sessions, 81 pull requests and 81 reviews. Findings are grouped by the folder that owns them, so one issue is one branch, one PR, one reviewer.

Each unit gets a fingerprint hashed from **the rule and the area only** — deliberately not from line numbers or counts, which change as work gets done. The detector is built to run on a schedule; running it twice updates the existing issue instead of filing a duplicate.

### 3. The Devin session

The prompt is a contract, not a description. Four deliberate choices:

- **The oracle is in the prompt.** The session runs the verification command itself before opening a PR.
- **Anti-patterns are named.** `key={index}` silences `jsx-key` while preserving the exact reconciliation bug the rule exists to catch. It is forbidden explicitly.
- **Stopping is sanctioned.** *"If you cannot fix this correctly, stop and explain why. A blocked session is a valid outcome; a fix that passes the linter while preserving the defect is not."*
- **Structured output**, so the verdict is machine-readable rather than scraped from prose.

### 4. Independent verification

The session's report is evidence, not proof. `verify.py` reproduces the claim in a throwaway worktree with our own pinned toolchain, and asserts the specific ways a gate can go green while the bug survives:

| Check | Why |
|---|---|
| no `key={index}` | silences the rule, keeps the bug |
| no suppression comments added | silences anything |
| no gate config modified | redefines the test and looks like progress |
| code not merely deleted | passes every check, loses the feature |
| rule clean in the target area | the actual goal |
| repo-wide count dropped | nothing regressed elsewhere |
| type-check + tests pass | nothing else broke |

This matters concretely: `oxlint --fix` on this repository deleted a user-facing i18n warning and left an empty statement behind, with type-check and tests both green. **A wrong fix is very often the easiest fix to automate.**

### 5. The ratchet

Two modes:

- **full** — the debt reached zero, so promote the rule to `"error"`. The defect class becomes impossible to reintroduce.
- **counting** — the debt did not reach zero, so freeze it. Today's count is committed as a ceiling and CI fails any PR that raises it.

The tool refuses to fire a full ratchet while findings remain, because flipping the switch early breaks the build for the next person to push anything at all.

Counting mode is what makes this apply to debt too large to ever hand-clear — the 557 `prefer-destructuring` findings nobody is going to fix by hand can still be stopped from growing.

---

## Observability

<http://localhost:8100>, JSON at `/metrics.json`.

**Gates closed** is the headline, because it is the only metric on the page that does not decay. Findings-fixed regrows the moment attention moves elsewhere — that is precisely what happened to the 1,453 findings already printed in every CI run.

Also reported: per-gate burndown, escalation rate next to success rate, and cost per **merged** PR (not per attempt — spend on discarded work is still spend).

**False positives are subtracted from each gate's denominator.** Four of the 47 `rules-of-hooks` findings are in `playwright/`, where the code is `await use(fixture)` — Playwright's fixture callback, not React's `use` hook. The linter matched on the name. The real number is 43, and the ratchet PR needs a `playwright/**` override or it would fail the build on code that was never broken.

---

## Budget controls

| Control | Where |
|---|---|
| Per-session ACU cap | `max_acu_limit` — enforced by the Devin API, so a runaway cannot outlive our polling loop |
| Global ceiling | Checked **before** session creation, persisted in SQLite so it survives a crash |
| Concurrency cap | Refuses to start beyond N in flight |
| Retry policy | Once on `expired`; **never** on `blocked` — retrying a blocked session is spending money to fail again |

---

## Layout

```
ratchet/
├── detector/
│   ├── gates.py       gate runners → normalized findings; clean-tree assertion
│   ├── grouping.py    findings → reviewable units; stable fingerprints and IDs
│   ├── render.py      issue body: findings + oracle + anti-patterns
│   └── detect.py      the event source; idempotent
├── orchestrator/
│   ├── devin.py       Devin API v3 client + structured output schema
│   ├── prompt.py      the contract sent to each session
│   ├── run.py         launch, poll, settle (verify / escalate / expire)
│   ├── verify.py      independent re-check — the non-negotiable one
│   ├── ratchet.py     the capstone PR, full and counting modes
│   ├── webhook.py     HMAC-verified receiver + reconciler
│   └── state.py       SQLite; the budget ledger survives restarts
├── Dockerfile
└── fixtures/          recorded sessions for simulate mode
baseline/              committed scans at 9f5611aca5 — the deltas' denominator
```

### Notes on the Devin API

Verified against live sessions rather than documentation: the session response field is **`status`** (`status_enum` exists but is always `null`), spend is **`acus_consumed`**, and pull requests arrive as a list under **`pull_requests[].pr_url`**. A finished session may report `status: "running"` with `status_detail: "waiting_for_user"` — it is resolved by what it produced, not by the status string.

---

## Why an agent, and not a script

Detection is entirely scriptable — that is the whole first half of this system.

Remediation is not, and the reason is sharper than "it's hard": **the wrong fix is usually the scriptable one.** `key={index}` closes `jsx-key` in a single regex and preserves the defect perfectly. Deleting an unused-ignore comment satisfies mypy and leaves 18 real type errors behind. Three of the four oxlint rules here have no autofixer at all.

Choosing a correct key requires reading the data to find a stable identifier. Fixing a conditional hook requires restructuring a component without changing what it renders. Those are judgement calls with a machine-checkable answer — which is the exact shape of work worth giving an autonomous agent, and the exact shape a codemod cannot do.

One session in this repository fixed 4 `jsx-key` findings and reported back `behavior_change: true`: the component had been crashing whenever its error list was empty, because `reduce` was called on an empty array with no initial value. Nobody asked it to look for that. It fixed it, and it said so, instead of shipping it silently.
