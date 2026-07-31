"""Turn findings into issues.

The unit of work is not a finding. 81 `jsx-key` findings filed as 81 issues
would mean 81 Devin sessions, 81 pull requests and 81 human reviews for what a
reviewer experiences as a handful of decisions. The unit is the *reviewable
unit*: one owning area, one branch, one PR, one reviewer.

Grouping is derived from the repo layout rather than hand-curated, so it stays
correct as the debt moves.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from gates import Finding

# Ordered: first match wins. These are Superset's own ownership boundaries --
# a plugin, a package, or a top-level src/ domain. Anything below MIN_UNIT
# findings is swept into a per-workstream misc bucket rather than becoming a
# one-line issue nobody wants to review.
AREA_PATTERNS = [
    re.compile(r"^superset-frontend/(plugins/[^/]+)/"),
    re.compile(r"^superset-frontend/(packages/[^/]+)/"),
    re.compile(r"^superset-frontend/(src/[^/]+)/"),
    re.compile(r"^superset-frontend/([^/]+)/"),
    # Python. Superset's backend packages are its ownership boundaries the same
    # way plugins are on the frontend, so the grouping rule is the same idea
    # applied to a different tree -- which is the point of normalizing findings
    # before they reach this layer.
    re.compile(r"^(superset/[^/]+/[^/]+)/"),
    re.compile(r"^(superset/[^/]+)/"),
]

MIN_UNIT = 4


def area_of(path: str) -> str:
    for pat in AREA_PATTERNS:
        m = pat.match(path)
        if m:
            return m.group(1)
    return "other"


@dataclass
class IssueUnit:
    workstream: str  # A | B | C | D | E | F
    ident: str  # C1, C2, ...
    rule: str
    gate: str
    area: str
    findings: list[Finding] = field(default_factory=list)

    @property
    def files(self) -> list[str]:
        return sorted({f.path for f in self.findings})

    @property
    def fingerprint(self) -> str:
        """Stable identity across detector runs.

        Deliberately keyed on (rule, area) and NOT on line numbers or counts:
        the detector runs on a schedule, and a partially-fixed area must map
        back to the same issue rather than opening a second one alongside it.
        """
        return hashlib.sha256(f"{self.rule}::{self.area}".encode()).hexdigest()[:12]


def _registry_path() -> Path:
    return Path(__file__).resolve().parents[2] / "ratchet" / "unit-ids.json"


def _assign_ident(workstream: str, fingerprint: str, area: str) -> str:
    """Give each area a permanent ID.

    Numbering by position looked fine until the first area was actually cleaned:
    `src/components` dropped off the list and every area below it shifted up, so
    the label `C5` silently came to mean a different area than it had an hour
    earlier. Issue titles, pull requests and the video would all disagree.

    IDs are therefore allocated once per area and persisted. Numbers are never
    reused -- a cleared area's ID retires with it.
    """
    path = _registry_path()
    reg = json.loads(path.read_text()) if path.exists() else {}
    if fingerprint in reg:
        return reg[fingerprint]["ident"]
    used = {v["ident"] for v in reg.values() if v["ident"].startswith(workstream)}
    n = 1
    while f"{workstream}{n}" in used:
        n += 1
    reg[fingerprint] = {"ident": f"{workstream}{n}", "area": area}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")
    return reg[fingerprint]["ident"]


def group(findings: list[Finding], workstream: str) -> list[IssueUnit]:
    by_area: dict[str, list[Finding]] = {}
    for f in findings:
        by_area.setdefault(area_of(f.path), []).append(f)

    big = {a: fs for a, fs in by_area.items() if len(fs) >= MIN_UNIT}
    small = [f for a, fs in by_area.items() if len(fs) < MIN_UNIT for f in fs]

    # Largest area first: the biggest bucket is the one whose fix establishes
    # the pattern every later bucket imitates.
    ordered = sorted(big.items(), key=lambda kv: (-len(kv[1]), kv[0]))

    units: list[IssueUnit] = []
    rule = findings[0].rule if findings else ""
    gate = findings[0].gate if findings else ""

    buckets = [(area, fs) for area, fs in ordered]
    if small:
        buckets.append(("misc", small))

    for area, fs in buckets:
        unit = IssueUnit(workstream, "", rule, gate, area,
                         sorted(fs, key=lambda f: (f.path, f.line)))
        unit.ident = _assign_ident(workstream, unit.fingerprint, area)
        units.append(unit)
    return units
