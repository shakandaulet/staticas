"""
The planner against real Gemini: reference requests and what their plans
must contain.

    .\\run.ps1 --eval              every case
    .\\run.ps1 --eval bracket      the cases whose name contains "bracket"

Needs the key (run.ps1 sets it) and a network; costs one planning call
per case. Not collected by pytest: its answers come from a model, so a
failure here is a finding to look at, not a broken build.

WHY THIS EXISTS
---------------
Every other check in this project starts from a hand-built plan. That
tests everything downstream of the model and nothing about the model:
whether "hang 10 kN off the other end" becomes a force against Y on the
free end, whether "through its bolt holes" becomes the holes selector or
a guess at a face. Those are the choices a wrong answer comes from, and
they are only visible by asking the model.

Each case names the decisions that matter and nothing else. The study
name, the wording of a note and the order of the steps are the model's
business; the domain, the target and the direction are not.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime

from .. import config
from ..codegen import emitter
from ..codegen.validator import validate
from ..core import domains as D
from ..core import planner

AXIAL = {"along_z", "against_z"}


@dataclass
class EvalCase:
    name: str
    message: str
    domain: str
    ops: tuple[str, ...] = ()
    primitive: str | None = None        # "" = the open part
    targets: dict[str, set[str]] = field(default_factory=dict)
    roles: dict[str, set[str]] = field(default_factory=dict)
    directions: dict[str, set[str]] = field(default_factory=dict)
    values: dict[str, float] = field(default_factory=dict)   # "op.slot": SI
    at_least: dict[str, int] = field(default_factory=dict)   # op: count
    distinct_targets: tuple[str, ...] = ()
    may_ask: bool = False


CASES = [
    EvalCase(
        "i_beam_tip",
        "A 2 m steel I-beam, 200 mm deep, with 100 mm wide flanges 12 mm "
        "thick and an 8 mm web, is built into a wall at one end. Hang 10 kN "
        "off the other end. How far does the tip drop?",
        "static", ops=("add_fixture", "add_force"), primitive="i_beam",
        targets={"add_fixture": {"role", "extreme"},
                 "add_force": {"role", "extreme"}},
        roles={"add_fixture": {"fixed_end"}, "add_force": {"free_end"}},
        directions={"add_force": {"against_y"}},
        values={"add_force.magnitude": 10_000, "set_geometry.length": 2.0,
                "set_geometry.web_thickness": 0.008}),

    EvalCase(
        "bracket_selected",
        "Bolt the open bracket down through its bolt holes and push 500 N "
        "along X on the face I have selected.",
        "static", ops=("add_fixture", "add_force"), primitive="",
        targets={"add_fixture": {"holes"},
                 "add_force": {"active_selection"}},
        directions={"add_force": {"along_x"}},
        values={"add_force.magnitude": 500}),

    EvalCase(
        "plate_hole",
        "A plate 100 mm wide, 300 mm long and 10 mm thick has a 25 mm hole "
        "in the middle. Hold one end and pull the other end with 20 kN "
        "along its length. What is the peak stress at the hole?",
        "static", ops=("add_fixture", "add_force"),
        primitive="plate_with_hole",
        targets={"add_fixture": {"role", "extreme"},
                 "add_force": {"role", "extreme"}},
        directions={"add_force": {"along_z"}},
        values={"add_force.magnitude": 20_000,
                "set_geometry.hole_diameter": 0.025}),

    EvalCase(
        "control_arm",
        "Hold the open control arm by its big bore and put 2 kN sideways "
        "through the small eye at the other end.",
        "static", ops=("add_fixture", "add_force"), primitive="",
        targets={"add_fixture": {"holes"}, "add_force": {"holes"}},
        distinct_targets=("add_fixture", "add_force"),
        values={"add_force.magnitude": 2000}),

    EvalCase(
        "bracket_primitive",
        "Make an angle bracket: 120 mm base, 100 mm upright, 60 mm wide, "
        "10 mm thick, with an 8 mm fillet inside the corner and two 11 mm "
        "bolt holes in the base. Bolt it down and push the top of the "
        "upright with 500 N along X.",
        "static", ops=("add_fixture", "add_force"),
        primitive="angle_bracket",
        targets={"add_fixture": {"holes"}, "add_force": {"role", "extreme"}},
        roles={"add_force": {"top"}},
        directions={"add_force": {"along_x"}},
        values={"set_geometry.fillet_radius": 0.008,
                "set_geometry.hole_diameter": 0.011}),

    EvalCase(
        "frequency",
        "What is the first natural frequency of a 1 m aluminium cantilever, "
        "20 mm wide and 40 mm deep, clamped at one end?",
        "frequency", ops=("add_fixture", "apply_material"),
        primitive="rectangular_beam",
        roles={"add_fixture": {"fixed_end"}},
        values={"set_geometry.length": 1.0, "set_geometry.height": 0.04}),

    EvalCase(
        "buckling",
        "Will a 3 m steel tube, 60 mm outside diameter with a 3 mm wall, "
        "buckle under 20 kN of axial compression? One end is fixed and the "
        "other is free.",
        "buckling", ops=("add_fixture", "add_force"), primitive="tube",
        roles={"add_fixture": {"fixed_end"}, "add_force": {"free_end"}},
        directions={"add_force": {"against_z"}},
        values={"add_force.magnitude": 20_000, "set_geometry.wall": 0.003}),

    EvalCase(
        "thermal_open_part",
        "The part I have open dissipates 15 W internally and sits in still "
        "air at 25 degC. How hot does it get?",
        "thermal", ops=("add_heat_power", "add_convection"), primitive="",
        values={"add_heat_power.power": 15,
                "add_convection.bulk_temperature": 298.15},
        may_ask=True),

    EvalCase(
        "two_temperatures",
        "Hold the top face of the open plate at 100 degC and the bottom face "
        "at 20 degC. What is the temperature distribution?",
        "thermal", primitive="",
        at_least={"add_temperature": 2},
        roles={"add_temperature": {"top", "bottom"}}),

    EvalCase(
        "nonlinear_strip",
        "A steel strip 200 mm long, 20 mm wide and 1 mm thick is clamped at "
        "one end with 50 N down at the tip. The deflection will be large, "
        "so account for that.",
        "nonlinear", ops=("add_fixture", "add_force"),
        directions={"add_force": {"against_y"}},
        values={"add_force.magnitude": 50}),

    EvalCase(
        "pipe_flow",
        "Water flows at 1 m/s through a 25 mm bore pipe 2 m long. What is "
        "the pressure drop?",
        "flow_internal", ops=("set_flow_domain", "add_flow_bc", "add_goal")),

    EvalCase(
        "drag",
        "Air at 30 m/s flows past the part I have open. What drag force "
        "does it see?",
        "flow_external", ops=("set_flow_domain", "add_goal"), primitive=""),
]


# --------------------------------------------------------------------------
# Judging
# --------------------------------------------------------------------------
def _judge(case: EvalCase, plan, resolved, questions, gaps) -> list[str]:
    bad: list[str] = []
    if plan.domain != case.domain:
        bad.append(f"domain {plan.domain}, expected {case.domain}")
    by_op: dict[str, list] = {}
    for op in resolved:
        by_op.setdefault(op.op, []).append(op)

    for name in case.ops:
        if name not in by_op:
            bad.append(f"no {name}")
    for name, n in case.at_least.items():
        if len(by_op.get(name, [])) < n:
            bad.append(f"{len(by_op.get(name, []))} x {name}, expected {n}+")

    if case.primitive is not None:
        geom = (by_op.get("set_geometry") or [None])[0]
        source = geom.options.get("source") if geom else "active_document"
        shape = geom.options.get("primitive", "") if geom else ""
        got = "" if source != "primitive" else shape
        if got != case.primitive:
            bad.append(f"geometry {got or 'open part'}, expected "
                       f"{case.primitive or 'open part'}")

    for name, kinds in case.targets.items():
        for op in by_op.get(name, []):
            kind = op.target.kind if op.target else None
            if kind not in kinds:
                bad.append(f"{name} targets {op.target.describe() if op.target else None}"
                           f", expected {sorted(kinds)}")
    for name, roles in case.roles.items():
        for op in by_op.get(name, []):
            t = op.target
            if t is None:
                bad.append(f"{name} has no target")
            elif t.kind == "role" and t.role not in roles:
                bad.append(f"{name} targets role {t.role}, expected {sorted(roles)}")
            elif t.kind not in ("role", "extreme"):
                bad.append(f"{name} targets {t.describe()}, expected role "
                           f"{sorted(roles)}")
    for name, dirs in case.directions.items():
        for op in by_op.get(name, []):
            d = op.options.get("direction", "normal")
            if d not in dirs:
                bad.append(f"{name} direction {d}, expected {sorted(dirs)}")
    for key, want in case.values.items():
        name, slot = key.split(".")
        ops = by_op.get(name, [])
        if not ops or slot not in ops[0].q:
            bad.append(f"{key} missing")
            continue
        got = ops[0].q[slot].value
        if abs(got - want) > 0.01 * abs(want):
            bad.append(f"{key} = {got:g}, expected {want:g}")
    if case.distinct_targets:
        a, b = (by_op.get(n, [None])[0] for n in case.distinct_targets)
        if a and b and a.target and b.target and \
                a.target.model_dump(exclude={"said"}) == \
                b.target.model_dump(exclude={"said"}):
            bad.append(f"{case.distinct_targets[0]} and "
                       f"{case.distinct_targets[1]} have the same target")

    if questions and not case.may_ask:
        bad.append("asked: " + " | ".join(q.question for q in questions))
    if gaps:
        bad.append("incomplete: " + "; ".join(gaps))
    if not questions and not gaps and not emitter.unsupported(resolved):
        code = emitter.emit(plan.domain, resolved, study_name=plan.study_name).code
        verdict = validate(code, plan.domain)
        if not verdict.ok:
            bad.append("emitted script failed validation: " + verdict.report())
    return bad


def run(gem, kb, plan_once, argv: list[str]) -> int:
    from ..memory import session as sessions

    chosen = [c for c in CASES if not argv or any(a in c.name for a in argv)]
    report = []
    failures = 0
    for case in chosen:
        started = time.time()
        sess = sessions.load(None)
        sess.add("user", case.message)
        guess, _ = D.classify(case.message, default="static")
        try:
            plan, reply = plan_once(gem, kb, case.message, guess, sess)
            resolved, questions, gaps = planner.finalise(plan)
            bad = _judge(case, plan, resolved, questions, gaps)
            detail = {"domain": plan.domain,
                      "ops": [o.as_dict() for o in resolved],
                      "questions": [q.question for q in questions],
                      "assumptions": plan.assumptions, "reply": reply}
        except Exception as exc:
            bad, detail = [f"planner failed: {exc}"], {}
        failures += bool(bad)
        print(f"\n{'FAIL' if bad else 'ok  '} {case.name} "
              f"({time.time() - started:.0f} s)")
        for b in bad:
            print(f"     - {b}")
        report.append({"case": case.name, "message": case.message,
                       "problems": bad, **detail})

    path = config.RUNS_DIR / f"planner_eval_{datetime.now():%Y%m%d-%H%M%S}.json"
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False),
                    encoding="utf-8")
    print(f"\n{len(chosen) - failures}/{len(chosen)} cases passed "
          f"(model {gem.model}); full plans in {path}")
    return 1 if failures else 0
