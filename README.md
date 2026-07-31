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

**Two of these six have runners here** — oxlint and mypy, covering five of the rules above. The rest are measured and documented in `baseline/` but not yet automated. Adding one means writing a function that returns a list of `Finding`; everything downstream — grouping, issue filing, the prompt contract, verification, the ratchet — already works on normalized findings and never learns which tool produced them. The wiring is six config tables that should be one registry, and [**docs/adding-a-gate.md**](docs/adding-a-gate.md) walks through it end to end with zizmor as the worked example, including the parts that are unfinished. See *Why an agent* for what was deliberately left out and why.

The `zizmor` config states the deadlock outright:

```yaml
# Advisory until pre-existing findings are resolved; remove
# --no-exit-codes to make this hook blocking.
```

Too much debt to turn the gate on. No gate, so more debt accumulates.

### This is not an outside opinion — it is written in the repository

Three of these switches carry a maintainer's note saying, explicitly, that enforcement is intended once the existing findings are cleared:

| Where | What it says |
|---|---|
| `superset-frontend/oxlint.json:176` | *"TODO: Graduate to `"error"` after cleanup pass — ~150 violations across the codebase require hoisting nested component definitions out of their parent render functions."* |
| `superset-frontend/oxlint.json:168` | *"Disabled because the codebase still contains legacy class components; flip to `"error"` once the class-to-function migration completes."* |
| `.pre-commit-config.yaml:176` | *"Advisory until pre-existing findings are resolved; remove `--no-exit-codes` to make this hook blocking."* |

The system quotes these back in the issues it files, with line references. "The project asked for this" is a categorically stronger claim than "a tool found this" — and it is checkable.

**Being precise about it:** the other gates carry no such statement. `jsx-key` is not mentioned in the config at all; it inherits a default. `ban-ts-comment` is simply `"off"`. Claiming a mandate where none is written would not survive a reviewer opening the file, so the tool only quotes rules that actually carry one.

### Two instruments, pointed at different things

Superset tracks its own tech debt: [`.github/workflows/tech-debt.yml`](https://github.com/apache/superset/blob/master/.github/workflows/tech-debt.yml) uploads lint counts to a **world-readable** Google Sheet every day. Nineteen months of data.

It disagrees with their CI, because the two run different commands:

```
package.json "lint"            npx oxlint --config oxlint.json --quiet   → 1,434
oxlint-metrics-uploader.js:82  npx oxlint --format json                  →    93
```

Without `--config`, oxlint uses its built-in defaults and never loads `oxlint.json`. **94% of what CI already counts is invisible to the dashboard — and that 94% is where every enforcement switch lives.** The dashboard's largest single number, `no-unused-vars` at 87, is a rule Superset explicitly set to `"off"`; the TypeScript replacement they actually enabled sits at zero.

The asymmetry is what marks it as an oversight rather than a decision: the ESLint half of that same function *does* pass `--config`, twenty lines below, with a comment explaining the choice.

And the part they can see has not improved. Like-for-like — the 13 rules present at both ends of the ESLint era — **533 → 560 across 324 measured days.** Up 5%, while being measured daily.

```bash
python ratchet/detector/upstream_metric.py --refresh
```

> Every rule Superset configured as `"error"` currently has **zero** findings. All 1,434 findings sit in rules set to `"warn"` or never configured. Debt exists exactly where enforcement doesn't — which is the entire argument for a ratchet over a dashboard.

**A ratchet is a toothed wheel that turns one way and cannot turn back.** Cleanup alone decays — clear a rule today and the count regrows. Promoting the enforcement switch is what makes the work permanent, and it is the deliverable this system exists to produce.

---

## Run it yourself

**Start here.** No API key, no credentials, no setup beyond the clone:

```bash
git clone https://github.com/CognitionTakeHomeOrg/debt-ratchet
cd debt-ratchet
docker compose run --rm simulate
```

`run` rather than `up`, so only this service starts. It mounts nothing and reads
no environment: the fixtures are baked into the image, so it behaves identically
on any machine. Runtime is about a minute; `-e FAST=1` skips the pacing.

Simulate replays all five real sessions, narrating the orchestrator's real
decision sequence: detect → file → launch → Devin works → structured report →
independent verification → human merges. The fixtures in `ratchet/fixtures/` are
**recorded API responses and message streams from those actual runs**, not
synthetic data. `simulate.py` refuses to touch a credential even if one is
present in the environment.

**The escalated session is replayed in full**, ending on the reason it refused
rather than on a success. A demo that shows only what worked is describing a
different system.

### The dashboard

```bash
docker compose up dashboard          # → http://localhost:8100
```

Needs no key and no fork checkout. A fresh clone has an empty ledger, so the
page falls back to `ratchet/fixtures/ledger.json` — the committed record of the
real runs — and labels itself **recorded results** while it does. Once the
orchestrator has run for real, the same page reads live from SQLite and the
label disappears. JSON at `/metrics.json`.

### File issues yourself — and see exactly what would happen first

The detector is the event source. Running it needs a checkout of the repository
under measurement and a `gh` login, but **no Devin key and no spend** — it only
reads the tree and writes issues.

```bash
git clone https://github.com/CognitionTakeHomeOrg/superset-adham-clone
cd superset-adham-clone/superset-frontend && npm ci && cd ../..

python ratchet/detector/detect.py --workstream C        # dry run, nothing written
```

```
gate=oxlint rule=react/jsx-key  findings=77  units=5
  C1   plugins/plugin-chart-echarts          36 findings  16 files  fp=9eebe8ba1fe2  -> create new
  C2   src/explore                           18 findings   5 files  fp=7ff60ee15b59  -> create new
  C3   src/dashboard                          7 findings   5 files  fp=eb7e0e1e7083  -> create new
  C4   packages/superset-ui-chart-controls    6 findings   3 files  fp=de1707890c50  -> create new
  C6   misc                                  10 findings   5 files  fp=0c0b7f28ca7d  -> create new

(dry run -- would create 5 and update 0. Pass --apply to do it.)
```

**The dry run resolves the create-or-update decision for real.** It queries the
open issues and matches each unit's fingerprint against them using the *same*
lookup that `--apply` uses — so the preview is the decision, not a description of
it. Run it against a workstream that has already been filed and you can watch
idempotency work rather than take it on trust:

```bash
python ratchet/detector/detect.py --workstream B
#   B2   src/components   18 findings   6 files  fp=772b48573da8  -> update #8
#   (dry run -- would create 6 and update 1. Pass --apply to do it.)
```

`B2` resolves to the issue that already exists. Nothing else in that workstream
does, so a second run edits one issue and files the rest — never a duplicate.

### File one issue

`--only-area` narrows a run to a single unit. Preview it, then repeat the command
with `--apply`:

```bash
python ratchet/detector/detect.py --workstream C --only-area packages/superset-ui-chart-controls
#   ... -> create new
#   (dry run -- would create 1 and update 0. Pass --apply to do it.)

python ratchet/detector/detect.py --workstream C --only-area packages/superset-ui-chart-controls --apply
#   created https://github.com/.../issues/N
```

**Whatever the arrow says is what `--apply` will do** — the areas listed above
already carry issues in this repository, so run the preview first and pick one
that still says `create new`. Or file the whole workstream:

```bash
python ratchet/detector/detect.py --workstream C --apply
```

Two workstreams are worth knowing about:

- **`G` is one unit by nature.** Only two `prefer-function-component` findings
  exist in the repository, so it can never produce more than one issue.
- **`E`** is the mypy gate — see the environment note below.

### Watch idempotency instead of trusting it

Every issue carries `<!-- ratchet-fingerprint: … -->`, and re-running resolves
against it. `src/dashboard` was filed as issue #13 by exactly the command above,
so the same command now previews an update rather than a duplicate:

```bash
python ratchet/detector/detect.py --workstream C --only-area src/dashboard
#   C3   src/dashboard   7 findings   5 files  fp=eb7e0e1e7083  -> update #13
#   (dry run -- would create 0 and update 1. Pass --apply to do it.)
```

That is the property the nightly schedule depends on. Workstreams are
`A` `B` `C` `D` `E` `G`; see the table under *The problem*.

`E` is the mypy gate and needs no npm install, but it does need the isolated
environment the gate is defined by — mypy plus ten stub packages and **nothing
else**. It refuses to run otherwise rather than answer a different question
(Trap 1, below):

```bash
python -m venv .mypy-gate
.mypy-gate/bin/pip install "mypy==1.15.0" \
  types-cachetools types-simplejson types-python-dateutil types-requests \
  types-pytz types-croniter types-PyYAML types-setuptools types-paramiko \
  types-Markdown
```

> The detector refuses to measure a dirty working tree. Every number here is a
> delta against a committed baseline, so uncommitted edits would not produce a
> wrong number — they would produce a fabricated one.

### The full system

Two prerequisites, and both are load-bearing:

```bash
# 1. the repository under measurement -- a separate clone, because this repo
#    measures Superset rather than living inside it
git clone https://github.com/CognitionTakeHomeOrg/superset-adham-clone
git -C superset-adham-clone checkout 9f5611aca5

# 2. credentials
cp .env.example .env      # DEVIN_API_KEY, DEVIN_ORG_ID, FORK_REPO, GITHUB_WEBHOOK_SECRET

docker compose up
```

- Dashboard → <http://localhost:8100>
- Webhook receiver → <http://localhost:8099/webhook>, health at `/health`

The checkout must exist before `up`: Docker cannot bind-mount a path that is not
there. Set `FORK_PATH` if you keep it somewhere else. This is the only step that
spends money — the budget controls are enforced before any session is created.

### Closing the loop: getting the event to the orchestrator

`docker compose up` gives you a receiver on `localhost:8099`. **GitHub cannot
reach that**, so filing an issue will not start anything until you connect the
two. There are two ways, and the second needs no inbound network at all.

**Either — the webhook.** Expose the receiver and register it on the repository
under measurement:

```bash
cloudflared tunnel --url http://localhost:8099        # or: ngrok http 8099

gh api repos/$FORK_REPO/hooks -X POST \
  -f name=web -F active=true -f 'events[]=issues' \
  -f config[url]="https://<tunnel-host>/webhook" \
  -f config[content_type]=json \
  -f config[secret]="$GITHUB_WEBHOOK_SECRET"
```

Signatures are verified with HMAC against that secret, and the handler ignores
anything that is not a detector-filed issue carrying `devin:queued` — without
that check, any human opening any issue would start a paid session.

**Or — the reconciler.** Ask GitHub what is queued instead of waiting to be told:

```bash
python ratchet/orchestrator/run.py --poll
```

This is not a lesser fallback. Webhooks get missed — tunnels drop, processes
restart, a delivery exhausts its retries — so the reconciler runs *alongside* the
receiver in normal operation and is what makes the system converge rather than
depend on every delivery landing. On a laptop it is also the whole loop by
itself, with no tunnel and nothing exposed.

To launch one specific issue, which is the same code path the webhook calls:

```bash
python ratchet/orchestrator/run.py --issue 13 --dry-run   # print the prompt, spend nothing
python ratchet/orchestrator/run.py --issue 13             # launch, poll, verify, report
```

> **How the sessions in this repository were run.** The receiver and reconciler
> are both implemented and are the same launch path, but no public tunnel was
> stood up for this project — every session here was started with
> `run.py --issue N`. The event-driven leg is built and unit-triggerable; what
> was never deployed is the inbound delivery.

## How it works

```
   cron: 0 6 * * *                     ◄── nothing here is hand-started
        │  six gates, one job each, in parallel
        ▼
   gate (oxlint · mypy)
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

### 0. The trigger

[`.github/workflows/detect.yml`](.github/workflows/detect.yml). A schedule, because a
system that only runs when someone remembers to run it is the failure this project
exists to attack:

```yaml
on:
  schedule:
    - cron: "0 6 * * *"    # 06:00 UTC daily — findings that appear
                           # overnight are filed before standup
  workflow_dispatch:       # manual, for a single workstream
```

Six jobs, one per gate, `fail-fast: false` so a mypy failure cannot silence the
oxlint ones. Each checks out the repository under measurement at the pinned
baseline, installs that gate's exact toolchain — `npm ci` for oxlint, the
stub-only venv for mypy — and runs the detector with `--apply`.

Two asymmetries are deliberate:

- **`--apply` is implicit on the schedule and opt-in on a manual run.** A hand-triggered
  run is a dry run unless you ask for issues, so nobody files fifty issues by
  reflex while testing.
- **The ratchet step only fires on the schedule** — not on `workflow_dispatch` —
  so a dry run can never touch the capstone PR.

Running on a schedule is only safe because the detector is idempotent: each unit
carries a fingerprint hashed from `(rule, area)`, deliberately not from line
numbers or counts, so a second run edits the existing issue instead of opening a
duplicate. See *Grouping* below.

Configuration lives in repository variables and one secret:

| | |
|---|---|
| `vars.FORK_REPO` | the repository under measurement |
| `vars.BASELINE_SHA` | the commit every delta is measured against |
| `secrets.RATCHET_GITHUB_TOKEN` | checkout + issue/PR write |

Run one gate by hand:

```bash
gh workflow run detect -f workstream=C -f apply=false     # dry run
python ratchet/detector/detect.py --workstream C          # or locally
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

```bash
python ratchet/orchestrator/ratchet.py --auto          # what would change
python ratchet/orchestrator/ratchet.py --auto --push   # open/update the PR
```

Two modes:

- **full** — a gate reached zero, so promote the rule to `"error"`. The defect class becomes impossible to reintroduce.
- **counting** — it did not, so freeze it. Today's counts are committed as ceilings and CI fails any pull request that raises one.

The tool **refuses** to fire a full ratchet while findings remain, because flipping the switch early breaks the build for the next person to push anything at all.

Counting mode is what makes this apply to debt too large to ever hand-clear. It does not require finishing.

**This runs automatically**, on the same nightly schedule as the detector — [`detect.yml:94-104`](.github/workflows/detect.yml#L94-L104) calls `ratchet.py --auto --push` after the gates have been measured, gated on `github.event_name == 'schedule'` so a manual dry run can never touch it. The thesis of the whole system is that cleanup fails because it depends on someone paying attention — so a ratchet a human has to remember to crank would reintroduce exactly that failure. The PR is updated in place rather than duplicated. **Merging stays human**: turning on a check that can fail everyone's build is a policy decision.

The PR ships `scripts/ratchet_check.py`, the committed ceilings, the pinned mypy config, and a **GitHub Actions workflow** — without that last piece a ceiling is a document, not a constraint. Verified in both directions: it passes at the committed counts, and introducing one unkeyed `.map()` produces

```
react/jsx-key    78    77    REGRESSION +1
error: react/jsx-key rose from 77 to 78.
```

---

## Observability

<http://localhost:8100>, JSON at `/metrics.json`.

**Gates closed** is the headline, because it is the only metric on the page that does not decay. Findings-fixed regrows the moment attention moves elsewhere — that is precisely what happened to the 1,453 findings already printed in every CI run.

Also reported: per-gate burndown across all five gates, escalation rate next to success rate, and cost per **merged** PR (not per attempt — spend on discarded work is still spend).

**Cost currently reads `n/a`, deliberately.** Devin's API has returned `acus_consumed: 0.0` for every session so far, including completed ones that opened pull requests. Dividing by that would publish "$0.00 per merged PR", which reads as either broken or dishonest, so the panel declares the number unavailable and says why. The budget controls below are live regardless — they cap before spending, not after measuring it.

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

## Where the human is

Nothing in this system merges anything. The state machine stops at `verifying` and waits.

```
Devin reports "done"        →  treated as evidence, never as proof
verify.py re-runs the oracle →  automated, 7–9 checks depending on the gate
behaviour change declared    →  labelled needs:human-review, with the rationale
merge                        →  HUMAN, always
ratchet PR opened            →  automatic
ratchet PR merged            →  HUMAN, always
```

The split is deliberate and consistent: **automate the noticing, never the deciding.** Finding debt, filing it, remediating it, and proving the fix are all mechanical. Accepting a behaviour change, and turning on a check that can fail everyone's build, are not.

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
│   ├── prompt.py      the contract sent to each session (oxlint + mypy variants)
│   ├── run.py         launch, poll, settle (verify / escalate / expire)
│   ├── verify.py      independent re-check — the non-negotiable one
│   ├── ratchet.py     the capstone PR, opened automatically
│   ├── webhook.py     HMAC-verified receiver + reconciler
│   ├── dashboard.py   gates closed, burndown, escalation rate
│   ├── simulate.py    replay recorded sessions; no key, no network, no spend
│   └── state.py       SQLite; the budget ledger survives restarts
├── gates/             mypy gate config + its control
├── fixtures/          recorded sessions for simulate mode
└── Dockerfile
baseline/              committed scans at 9f5611aca5 — the deltas' denominator
```

`gates/mypy-control.ini` is not dead weight. It replicates `pyproject.toml`'s mypy settings exactly, *including* the `warn_unused_ignores = false` override that `mypy-unused-ignore.ini` removes. The control running clean across 1,423 files is what proves the two configs differ in one variable and nothing else — without it, 49 is just a number a tool printed.

### Notes on the Devin API

Verified against live sessions rather than documentation: the session response field is **`status`** (`status_enum` exists but is always `null`), spend is **`acus_consumed`**, and pull requests arrive as a list under **`pull_requests[].pr_url`**. A finished session may report `status: "running"` with `status_detail: "waiting_for_user"` — it is resolved by what it produced, not by the status string.

---

## Why an agent, and not a script

Detection is entirely scriptable — that is the whole first half of this system.

Remediation is not, and the reason is sharper than "it's hard": **the wrong fix is usually the scriptable one.** `key={index}` closes `jsx-key` in a single regex and preserves the defect perfectly. Deleting an unused-ignore comment satisfies mypy and leaves 18 real type errors behind. Three of the four oxlint rules here have no autofixer at all.

Choosing a correct key requires reading the data to find a stable identifier. Fixing a conditional hook requires restructuring a component without changing what it renders. Those are judgement calls with a machine-checkable answer — which is the exact shape of work worth giving an autonomous agent, and the exact shape a codemod cannot do.

One session in this repository fixed 4 `jsx-key` findings and reported back `behavior_change: true`: the component had been crashing whenever its error list was empty, because `reduce` was called on an empty array with no initial value. Nobody asked it to look for that. It fixed it, and it said so, instead of shipping it silently.

`oxlint --fix` was tested on this repository as a control. On `no-console` it deleted a user-facing i18n warning and left an empty statement behind — with type-check and tests both green. That diff is the argument in one artifact: **a wrong fix is very often the easiest fix to automate.**

---

## Scope, and what was left out

Deliberately excluded: `prefer-destructuring` (557 findings) and `exhaustive-deps` (374). Together they are roughly 64% of Superset's own tech-debt metric, and clearing them would move that number more than everything here combined. They were declined because neither has a correctness argument a reviewer would thank you for — one is stylistic, and the other frequently requires changing when effects fire. Volume is not the goal; closing gates is.

`no-unstable-nested-components` (150) and `ban-ts-comment` (197) are measured and **frozen by the ratchet** — capped so they cannot grow — but not yet remediated. zizmor's single finding is measured in `baseline/` only; it has no runner and is not in the ratchet.

**Four of the 47 `rules-of-hooks` findings are false positives**, all in `playwright/`: the code is `await use(fixture)` — Playwright's fixture callback, not React's `use` hook. The linter matched on the name. The real number is 43, the dashboard subtracts them from the denominator, and a full ratchet on that rule will need a `playwright/**` override. Finding this cost fifteen minutes of reading; missing it would have failed a build on code that was never broken.
