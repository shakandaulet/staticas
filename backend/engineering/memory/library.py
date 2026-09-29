"""
The learning loop: a run that checked out becomes a retrievable example.

WHAT MAKES THIS WORTH HAVING
----------------------------
Documentation says what the API does. A verified run says what actually
worked on a real machine, with real numbers that survived an
independent cross-check. The second is worth more to the next plan, and
it is the only part of the corpus that grows without anyone writing it.

The gate is the point. A run is filed as VERIFIED only when its numbers
agree with the closed-form check for the same idealised problem. A run
that solved but disagreed is filed as UNVERIFIED and kept out of
retrieval -- because a confidently wrong example in the corpus teaches
the next plan to be wrong the same way, and does it invisibly.

Runs that FAILED are kept too, and they are not noise: a receipt that
records "nothing was selected for the fixture" is the most useful thing
in the file when the same message arrives again.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from .. import config
from ..core import physics

VERIFIED = "verified"
UNVERIFIED = "unverified"
FAILED = "failed"


@dataclass
class Example:
    id: str
    domain: str
    request: str
    summary: str
    plan: dict
    results: dict
    status: str
    verdict: str = ""
    messages: list[str] = field(default_factory=list)
    checks: dict = field(default_factory=dict)
    at: float = field(default_factory=time.time)


def _existing_ids() -> set[str]:
    if not config.EXAMPLES_PATH.exists():
        return set()
    ids = set()
    for line in config.EXAMPLES_PATH.read_text(encoding="utf-8").splitlines():
        try:
            ids.add(json.loads(line).get("id"))
        except json.JSONDecodeError:
            continue
    return ids


def ingest(receipt: dict, request: str, plan: dict,
           resolved_ops: list[Any]) -> Example:
    """File one run receipt, with its verdict decided by the cross-check."""
    domain = receipt.get("domain") or plan.get("domain") or "static"
    results = receipt.get("results", {}) or {}
    run_id = f"{domain}-{receipt.get('run_at', time.time())}".replace(":", "")

    if receipt.get("error"):
        status, verdict = FAILED, "the run did not complete"
        messages = [receipt["error"]]
        checks: dict = {}
    elif not results:
        status, verdict = UNVERIFIED, "no results could be read back"
        messages = list(receipt.get("warnings", []))
        checks = {}
    else:
        report = physics.crosscheck(domain, resolved_ops, results, receipt)
        checks = report.as_dict()
        messages = [f.message for f in report.findings]
        disagreements = [f for f in report.findings
                         if f.level == physics.WARN and f.kind == "check"]
        if disagreements:
            status = UNVERIFIED
            verdict = "results disagree with the closed-form check"
        elif report.compared:
            status = VERIFIED
            verdict = "results agree with the closed-form check"
        else:
            # No applicable closed form is not the same as agreement, and
            # promoting it to VERIFIED would put an unchecked run into
            # retrieval wearing a badge it did not earn.
            status = UNVERIFIED
            verdict = "no closed-form check applies to this case"

    example = Example(
        id=run_id, domain=domain, request=request,
        summary=receipt.get("summary", "") or plan.get("summary", ""),
        plan=plan, results=results, status=status, verdict=verdict,
        messages=messages, checks=checks)

    if example.id not in _existing_ids():
        with config.EXAMPLES_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(example), ensure_ascii=False) + "\n")
    return example


_LEGACY_SHAPES = {
    "rectangular_beam": ("width", "height", "length"),
    "round_bar": ("diameter", "length"),
    "tube": ("outer_diameter", "wall", "length"),
    "i_beam": ("flange_width", "height", "web_thickness", "flange_thickness",
               "length"),
}


def legacy_example(request: dict) -> Example | None:
    """One verified run of the previous generation, in this one's terms.

    Only runs that version itself marked verified AND whose sanity check
    compared them with beam theory, and only loads this plan schema can
    say: a distributed load on the top face, a fixed end. A point load
    part-way along the span has no face to go on here, and importing it
    as something else would file a wrong example as a right one. The
    earliest runs of that version created FREQUENCY studies while calling
    them static; they are marked failed there and stay out."""
    form = request.get("form_data") or {}
    geo, load = form.get("geometry") or {}, form.get("load") or {}
    support = form.get("support") or {}
    sanity = request.get("sanity") or {}
    dims = _LEGACY_SHAPES.get(geo.get("shape", ""))
    if (request.get("status") != "verified" or sanity.get("verdict") != "plausible"
            or dims is None or load.get("type") != "distributed_downward"
            or support.get("type") != "fixed"
            or support.get("location") not in ("start", "end")):
        return None

    from ..core.schema import Plan, QSlot, RawOp, Selector
    side = "min" if support["location"] == "start" else "max"
    plan = Plan(domain="static", study_name=f"Legacy_{request.get('id')}",
                summary=request.get("description", ""), ops=[
        RawOp(op="set_geometry",
              options={"source": "primitive", "primitive": geo["shape"]},
              quantities=[QSlot(name=d, value=geo[f"{d}_mm"], unit="mm")
                          for d in dims if f"{d}_mm" in geo]),
        RawOp(op="apply_material",
              options={"name": (form.get("material") or {}).get("name", "")}),
        RawOp(op="add_fixture", options={"type": "fixed"},
              target=Selector(kind="extreme", axis="z", side=side),
              note="Built-in end"),
        RawOp(op="add_force", options={"direction": "against_y"},
              target=Selector(kind="role", role="top"),
              quantities=[QSlot(name="magnitude", value=load["magnitude_N"],
                                unit="N")],
              note="Distributed load over the top face"),
        RawOp(op="set_mesh"), RawOp(op="solve"), RawOp(op="extract_results"),
    ]).model_dump()

    got = request.get("results") or {}
    results = {}
    if "max_von_mises_MPa" in got:
        results["von_mises_max"] = got["max_von_mises_MPa"] * 1e6
    if "max_displacement_mm" in got:
        results["displacement_max"] = got["max_displacement_mm"] / 1000.0
    ratios = sanity.get("ratios") or {}
    return Example(
        id=f"legacy-{request.get('id')}", domain="static",
        request=request.get("description", ""),
        summary=request.get("description", ""), plan=plan, results=results,
        status=VERIFIED,
        verdict=(f"imported from the previous version, where it agreed with "
                 f"beam theory (stress x{ratios.get('stress', 0):.3g}, "
                 f"deflection x{ratios.get('deflection', 0):.3g})"),
        messages=list(sanity.get("messages") or []), checks=sanity,
        at=time.time())


def import_previous_version(examples_dir) -> list[Example]:
    """File the previous generation's verified runs as retrievable examples.
    Safe to repeat: an id already in the library is not added again."""
    from pathlib import Path
    existing = _existing_ids()
    added = []
    for path in sorted(Path(examples_dir).glob("*/request.json")):
        try:
            ex = legacy_example(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        if ex is None or ex.id in existing:
            continue
        with config.EXAMPLES_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(ex), ensure_ascii=False) + "\n")
        added.append(ex)
    return added


def load_retrievable() -> list[dict]:
    """Only verified runs reach retrieval."""
    if not config.EXAMPLES_PATH.exists():
        return []
    out = []
    for line in config.EXAMPLES_PATH.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("status") == VERIFIED:
            out.append(rec)
    return out


def stats() -> dict:
    counts = {VERIFIED: 0, UNVERIFIED: 0, FAILED: 0}
    if not config.EXAMPLES_PATH.exists():
        return counts
    for line in config.EXAMPLES_PATH.read_text(encoding="utf-8").splitlines():
        try:
            status = json.loads(line).get("status", UNVERIFIED)
        except json.JSONDecodeError:
            continue
        counts[status] = counts.get(status, 0) + 1
    return counts


if __name__ == "__main__":
    # python -m engineering.memory.library <previous version's examples dir>
    import sys
    for ex in import_previous_version(sys.argv[1]):
        print(f"  filed {ex.id}: {ex.summary}")
    print("  library:", stats())
