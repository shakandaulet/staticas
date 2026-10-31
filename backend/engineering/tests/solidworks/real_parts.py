"""
Real SolidWorks runs -- what the unit suite cannot check.

    python -m engineering.tests.solidworks.real_parts           (from C:\\)
    python -m engineering.tests.solidworks.real_parts beam      (cases matching)

Two groups. Every study type on one reference beam, each against its
hand calculation; and statics on real shapes -- holes, fillets, rounded
ends, SolidWorks' own sample parts.

Needs SolidWorks with the Simulation add-in on this machine, and takes a
few minutes. Not collected by pytest: it drives the CAD window.

WHAT IT CHECKS
--------------
Every case goes the way a plan does -- finalise, emit, validate, run,
cross-check -- and then reads the receipt for WHERE the restraint and
the load went, not only whether the study solved. A load on the wrong
face solves just as happily as one on the right face; the selection log
is the only thing that tells them apart without a person looking.

The parts are the reason this exists. Everything used to be verified on
one beam this project builds itself, along Z, with six flat faces. Real
parts have holes, fillets and rounded ends, and they run along whichever
axis their designer liked: on SolidWorks' own sample parts "the free
end" landed on a big flat side face and "the top" of a round shaft on
its curved surface. The cases below are those parts.
"""

from __future__ import annotations

import json
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pythoncom
import win32com.client

from ... import config
from ...codegen import emitter, runner
from ...codegen.validator import validate
from ...core import physics, planner
from ...core.schema import Plan, QSlot, RawOp, Selector

SAMPLES = Path(r"C:\Users\Public\Documents\SOLIDWORKS\SOLIDWORKS 2024\samples")
WORK = config.RUNS_DIR / "real_parts"
NOTHING = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)


# --------------------------------------------------------------------------
# Plan pieces
# --------------------------------------------------------------------------
def geometry(shape: str, **mm) -> RawOp:
    return RawOp(op="set_geometry",
                 options={"source": "primitive", "primitive": shape},
                 quantities=[QSlot(name=k, value=v, unit="mm")
                             for k, v in mm.items()])


OPEN_PART = RawOp(op="set_geometry", options={"source": "active_document"})
STEEL = RawOp(op="apply_material", options={"name": "Plain Carbon Steel"})
TAIL = [RawOp(op="set_mesh"), RawOp(op="solve"), RawOp(op="extract_results")]


def fix(target: Selector, note: str) -> RawOp:
    return RawOp(op="add_fixture", options={"type": "fixed"}, target=target,
                 note=note)


def force(newtons: float, direction: str, target: Selector, note: str) -> RawOp:
    return RawOp(op="add_force", target=target, note=note,
                 quantities=[QSlot(name="magnitude", value=newtons, unit="N")],
                 options={"direction": direction})


# --------------------------------------------------------------------------
# Expectations: each returns a list of what is wrong, empty when fine
# --------------------------------------------------------------------------
def selected(receipt: dict, note: str) -> dict:
    for entry in receipt.get("selection_log", []):
        if entry.get("target") == note and entry.get("kind") != "role":
            return entry
    return {}


def expect_faces(note: str, kind: str, area_mm2: float | None = None,
                 holes: int | None = None, diameter_mm: float | None = None):
    def check(receipt, _checks):
        got = selected(receipt, note)
        bad = []
        if got.get("kind") != kind:
            return [f"{note}: selected by {got.get('kind')!r}, expected {kind!r}"]
        if area_mm2 is not None:
            area = got.get("area", 0) * 1e6 if "area" in got else \
                sum(got.get("areas_mm2", []))
            if abs(area - area_mm2) > 0.01 * area_mm2:
                bad.append(f"{note}: {area:.1f} mm2 selected, expected "
                           f"{area_mm2:.1f}")
        if holes is not None and got.get("holes") != holes:
            bad.append(f"{note}: {got.get('holes')} holes, expected {holes}")
        if diameter_mm is not None and any(
                abs(d - diameter_mm) > 0.05 for d in got.get("diameters_mm", [])):
            bad.append(f"{note}: diameters {got.get('diameters_mm')}, "
                       f"expected {diameter_mm}")
        return bad
    return check


def expect_agreement(receipt, checks):
    if not checks.compared:
        return ["no closed-form comparison was made"]
    return [f"cross-check: {f.message}" for f in checks.findings
            if f.level == physics.WARN and f.kind == "check"]


def expect_balance(receipt, checks):
    """The reactions carry the whole applied load."""
    if not any("carry the whole load" in f.message for f in checks.findings):
        return ["no equilibrium confirmation: "
                + str((receipt.get("results") or {}).get("reaction_force_N"))]
    return []


def expect_material(receipt, _checks):
    props = receipt.get("material_props") or {}
    return [] if props.get("E") and props.get("yield") else \
        ["material properties were not read from the library"]


def expect_converged(max_change_pct: float):
    def check(receipt, _checks):
        conv = receipt.get("convergence") or {}
        changes = conv.get("changes") or {}
        if not changes:
            return ["no convergence record"]
        return [f"{k} moved {v['change_pct']:.2f}% (> {max_change_pct}%)"
                for k, v in changes.items() if v["change_pct"] > max_change_pct]
    return check


def expect_range(key: str, lo: float, hi: float, unit: float = 1.0,
                 label: str = ""):
    def check(receipt, _checks):
        v = (receipt.get("results") or {}).get(key)
        if not isinstance(v, (int, float)):
            return [f"{key}: no value"]
        if not lo <= v / unit <= hi:
            return [f"{key} = {v / unit:.4g}{label}, expected {lo:g}..{hi:g}"
                    f"{label}"]
        return []
    return check


# --------------------------------------------------------------------------
# The cases
# --------------------------------------------------------------------------
@dataclass
class Case:
    name: str
    about: str
    domain: str
    ops: list[RawOp]
    expect: list[Callable]
    sample: str = ""                          # a SolidWorks sample to open
    prepare: Callable | None = None           # builds/selects before the run
    results: dict = field(default_factory=dict)


def _bracket_with_top_selected(app):
    """Build the bracket here, then click the top of its upright the way
    a person would -- a pick at a point on it -- and leave it selected."""
    from ...templates import runtime as rt
    model = rt.new_part(app)
    rt.build_angle_bracket(model, 0.12, 0.10, 0.06, 0.01, 0.008, 0.011)
    model.ClearSelection2(True)
    ok = model.Extension.SelectByID2("", "FACE", 0.005, 0.1, 0.03, False, 0,
                                     NOTHING, 0)
    if not ok:
        raise RuntimeError("could not pick the top of the upright")


BRACKET = dict(length=120, height=100, width=60, thickness=10,
               fillet_radius=8, hole_diameter=11)

# The reference beam every study type is run on, so each number has a
# hand calculation beside it: 50 x 100 x 2000 mm plain carbon steel.
BEAM = geometry("rectangular_beam", width=50, height=100, length=2000)
ROOT = fix(Selector(kind="role", role="fixed_end"), "root")
TIP = Selector(kind="role", role="free_end")

STUDY_CASES = [
    Case("beam_static", "Reference beam, 5 kN down on the free end",
         "static",
         [BEAM, STEEL, ROOT, force(5000, "against_y", TIP, "tip load"), *TAIL],
         # F*L^3/3EI = 15.24 mm with the library's E of 210 GPa.
         [expect_agreement, expect_balance, expect_material,
          expect_range("displacement_max", 15.0, 15.5, 1e-3, " mm"),
          expect_range("factor_of_safety_away_from_supports", 1.6, 2.2)]),

    Case("beam_thermal", "Reference beam, 20 W in, still air at 25 degC",
         "thermal",
         [BEAM, STEEL,
          RawOp(op="add_heat_power", note="heat in",
                target=Selector(kind="body", entity_type="SOLIDBODY"),
                quantities=[QSlot(name="power", value=20, unit="W")]),
          RawOp(op="add_convection", note="still air",
                target=Selector(kind="all_faces"),
                quantities=[QSlot(name="film_coefficient", value=12,
                                  unit="W/m2K"),
                            QSlot(name="bulk_temperature", value=25,
                                  unit="degC")]),
          *TAIL],
         # dT = Q/(h*A) = 20 / (12 * 0.61) = 2.7 K above 298.15 K.
         [expect_agreement,
          expect_range("temperature_max", 300.4, 301.5, 1.0, " K")]),

    Case("beam_frequency", "Reference beam as a cantilever, three modes",
         "frequency",
         [BEAM, STEEL, ROOT,
          RawOp(op="set_frequency_options",
                quantities=[QSlot(name="modes", value=3, unit="")]),
          *TAIL],
         # First bending mode about the weak axis: 10.5 Hz.
         [expect_agreement,
          expect_range("first_frequency", 10.0, 11.0, 1.0, " Hz")]),

    Case("beam_buckling", "Reference beam, 10 kN along the span",
         "buckling",
         [BEAM, STEEL, ROOT, force(10_000, "against_z", TIP, "axial load"),
          *TAIL],
         # Euler, fixed-free (K = 2), weak axis: 135 kN, a factor of 13.5.
         [expect_agreement,
          expect_range("buckling_load_factor", 12.8, 14.2)]),

    Case("beam_nonlinear", "Reference beam, 5 kN down, non-linear solve",
         "nonlinear",
         [BEAM, STEEL, ROOT, force(5000, "against_y", TIP, "tip load"), *TAIL],
         # Small deflection for its span, so the linear answer holds.
         [expect_balance,
          expect_range("displacement_max", 14.5, 16.5, 1e-3, " mm")]),

    Case("beam_converged", "Reference beam, 5 kN down, convergence checked",
         "static",
         [BEAM, STEEL, ROOT, force(5000, "against_y", TIP, "tip load"),
          RawOp(op="set_mesh", options={"convergence": "check"}),
          RawOp(op="solve"), RawOp(op="extract_results")],
         [expect_agreement, expect_converged(2.0)]),
]

def heat(op: str, target: Selector, note: str, **values) -> RawOp:
    units = {"temperature": "degC", "bulk_temperature": "degC",
             "ambient_temperature": "degC", "film_coefficient": "W/m2K",
             "heat_flux": "W/m2", "power": "W", "emissivity": ""}
    return RawOp(op=op, target=target, note=note,
                 quantities=[QSlot(name=k, value=v, unit=units[k])
                             for k, v in values.items()])


ALL_FACES = Selector(kind="all_faces")
STILL_AIR = heat("add_convection", ALL_FACES, "still air",
                 film_coefficient=12, bulk_temperature=25)

# Loads and restraints that had never been run, each on a part with an
# exact answer.
OPS_CASES = [
    Case("temp_prescribed", "Plate 100 x 100 x 10, top held at 100 degC and "
                            "bottom at 20 degC", "thermal",
         [geometry("plate", width=100, length=100, thickness=10), STEEL,
          heat("add_temperature", Selector(kind="role", role="top"), "hot",
               temperature=100),
          heat("add_temperature", Selector(kind="role", role="bottom"), "cold",
               temperature=20), *TAIL],
         [expect_range("temperature_max", 373.0, 373.3, 1.0, " K"),
          expect_range("temperature_min", 293.0, 293.3, 1.0, " K")]),

    Case("heat_flux", "Reference beam, 500 W/m2 into the top face, still air",
         "thermal",
         [BEAM, STEEL,
          heat("add_heat_flux", Selector(kind="role", role="top"), "sun",
               heat_flux=500), STILL_AIR, *TAIL],
         # 500 W/m2 on 0.1 m2 is 50 W; 50 / (12 * 0.61) = 6.8 K.
         [expect_agreement]),

    Case("radiation", "Reference beam, 20 W in, radiating at emissivity 0.9 "
                      "to 25 degC surroundings", "thermal",
         [BEAM, STEEL,
          heat("add_heat_power", Selector(kind="body", entity_type="SOLIDBODY"),
               "heat in", power=20),
          heat("add_radiation", ALL_FACES, "radiation", emissivity=0.9,
               ambient_temperature=25), *TAIL],
         # eps*sigma*A*(T^4 - Ta^4) = 20 W gives T = 304.0 K.
         [expect_agreement,
          expect_range("temperature_max", 303.0, 305.0, 1.0, " K")]),

    Case("immovable_root", "Reference beam held by an immovable root, 5 kN "
                           "on the tip", "static",
         [BEAM, STEEL,
          RawOp(op="add_fixture", options={"type": "immovable"},
                target=Selector(kind="role", role="fixed_end"), note="root"),
          force(5000, "against_y", TIP, "tip load"), *TAIL],
         # A solid has no rotations to free: the same 15.24 mm as fixed.
         [expect_agreement]),

    Case("roller_symmetry", "Block 50 x 100 x 200 on a roller, symmetry on "
                            "two faces, 100 kN pressed on top", "static",
         [geometry("rectangular_beam", width=50, height=100, length=200), STEEL,
          RawOp(op="add_fixture", options={"type": "roller_slider"},
                target=Selector(kind="role", role="bottom"), note="roller"),
          RawOp(op="add_fixture", options={"type": "symmetry"},
                target=Selector(kind="extreme", axis="x", side="min"),
                note="symmetry x"),
          RawOp(op="add_fixture", options={"type": "symmetry"},
                target=Selector(kind="role", role="back"), note="symmetry z"),
          force(100_000, "against_y", Selector(kind="role", role="top"),
                "press"), *TAIL],
         # Free to spread sideways, so the stress is exactly F/A = 10 MPa
         # everywhere, and the far top corner moves sqrt(4.76^2 + 0.67^2 +
         # 2.67^2) = 5.50 um.
         [expect_range("von_mises_max", 9.7, 10.3, 1e6, " MPa"),
          expect_range("displacement_max", 5.3, 5.7, 1e-6, " um")]),

    Case("bracket_hinged", "Angle bracket on hinges at its bolt holes, 500 N "
                           "on the upright", "static",
         [geometry("angle_bracket", **BRACKET), STEEL,
          RawOp(op="add_fixture", options={"type": "fixed_hinge"},
                target=Selector(kind="holes"), note="hinges"),
          force(500, "along_x", Selector(kind="role", role="top"), "push"),
          *TAIL],
         # Softer than fully fixed holes (0.36 mm), never stiffer.
         [expect_range("displacement_max", 0.36, 0.8, 1e-3, " mm")]),

    Case("per_hole", "SolidWorks sample: control arm, 2 kN per hole through "
                     "the eye -- one hole, two faces", "static",
         [OPEN_PART, STEEL,
          fix(Selector(kind="holes", axis="x", side="min"), "bore"),
          RawOp(op="add_force", note="eye load",
                target=Selector(kind="holes", axis="x", side="max"),
                quantities=[QSlot(name="magnitude", value=2000, unit="N")],
                options={"direction": "against_y",
                         "distribution": "per_entity"}), *TAIL],
         # Per hole, not per face: 2 kN in total, not 4.
         [expect_balance],
         sample=r"tutorial\cosmosxpress\aw_control_arm.sldprt"),
]

FLOW_EXAMPLES = Path(r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS Flow Simulation"
                     r"\Examples")


def expect_goals(receipt, _checks):
    goals = (receipt.get("results") or {}).get("goals") or {}
    if not goals:
        return ["no goals were read back"]
    return [f"goal '{k}' at {v['progress_pct']:.0f}% progress"
            for k, v in goals.items() if v["progress_pct"] < 100.0]


# Flow Simulation: the example projects SolidWorks ships, solved from the
# script and their goals read back. The project itself is the example's;
# what is under test is running it and reading it.
FLOW_CASES = [
    Case("flow_valve", "Flow example b1: water through a ball valve, "
                       "pressure loss", "flow_internal",
         [OPEN_PART,
          RawOp(op="set_flow_domain",
                options={"analysis_type": "internal", "fluid": "Water"},
                quantities=[QSlot(name="ambient_temperature", value=20,
                                  unit="degC")]),
          RawOp(op="add_flow_bc", options={"type": "inlet_velocity"},
                target=Selector(kind="role", role="inlet"),
                quantities=[QSlot(name="velocity", value=1, unit="m/s")]),
          RawOp(op="add_flow_bc", options={"type": "outlet_static_pressure"},
                target=Selector(kind="role", role="outlet"),
                quantities=[QSlot(name="pressure", value=101325, unit="Pa")]),
          RawOp(op="add_goal", options={"quantity": "pressure_drop",
                                        "scope": "surface"}),
          RawOp(op="solve"), RawOp(op="extract_results")],
         # Solved by hand the first time: 16.3 kPa of total-pressure loss.
         [expect_goals,
          expect_range("pressure_drop_Pa", 10_000, 25_000, 1.0, " Pa")],
         sample=str(FLOW_EXAMPLES / "b1 - hydraulic loss" / "Ready" /
                    "valve.sldprt")),

    Case("flow_cylinder", "Flow example b2: flow across a cylinder at "
                          "Re = 1e5, drag coefficient", "flow_external",
         [OPEN_PART,
          RawOp(op="set_flow_domain",
                options={"analysis_type": "external", "fluid": "Water"},
                quantities=[QSlot(name="ambient_temperature", value=20,
                                  unit="degC")]),
          RawOp(op="add_goal", options={"quantity": "drag_force"}),
          RawOp(op="solve"), RawOp(op="extract_results")],
         # A cylinder at Re 1e5 sits on the subcritical plateau, Cd 1.0-1.2.
         [expect_goals,
          expect_range("drag_coefficient", 0.85, 1.3)],
         sample=str(FLOW_EXAMPLES / "b2 - drag coefficient" / "Ready" /
                    "c1m.sldprt")),
]

CASES = STUDY_CASES + OPS_CASES + FLOW_CASES + [
    Case("i_beam", "I-beam cantilever, 10 kN down on the free end",
         "static",
         [geometry("i_beam", flange_width=100, height=200, web_thickness=8,
                   flange_thickness=12, length=2000), STEEL,
          fix(Selector(kind="role", role="fixed_end"), "root"),
          force(10_000, "against_y", Selector(kind="role", role="free_end"),
                "tip load"), *TAIL],
         [expect_faces("root", "extreme", area_mm2=3808),
          expect_faces("tip load", "extreme", area_mm2=3808),
          expect_agreement]),

    Case("plate_hole", "Plate with a hole, pulled along its length by 20 kN",
         "static",
         [geometry("plate_with_hole", width=100, length=300, thickness=10,
                   hole_diameter=25), STEEL,
          fix(Selector(kind="role", role="fixed_end"), "held end"),
          force(20_000, "along_z", Selector(kind="role", role="free_end"),
                "pull"), *TAIL],
         [expect_faces("held end", "extreme", area_mm2=1000),
          expect_faces("pull", "extreme", area_mm2=1000),
          expect_agreement]),

    Case("bracket", "Angle bracket bolted through its base, 500 N on the "
                    "top of the upright",
         "static",
         [geometry("angle_bracket", **BRACKET), STEEL,
          fix(Selector(kind="holes"), "bolts"),
          force(500, "along_x", Selector(kind="role", role="top"), "push"),
          *TAIL],
         [expect_faces("bolts", "holes", holes=2, diameter_mm=11),
          expect_faces("push", "extreme", area_mm2=600),
          # The upright alone, as a cantilever off the base, gives
          # 0.12 mm; the base bends about the bolts as well.
          expect_range("displacement_max", 0.12, 2.0, 1e-3, " mm")]),

    Case("bracket_selected", "The same bracket and load, the upright's top "
                             "picked by hand in SolidWorks",
         "static",
         [OPEN_PART, STEEL,
          fix(Selector(kind="holes"), "bolts"),
          force(500, "along_x", Selector(kind="active_selection"), "push"),
          *TAIL],
         [expect_faces("bolts", "holes", holes=2, diameter_mm=11),
          expect_faces("push", "active_selection", area_mm2=600)],
         prepare=_bracket_with_top_selected),

    Case("anchor_plate", "SolidWorks sample: anchor plate held at its four "
                         "bolt holes, pulled up 1 kN by the top of the eye",
         "static",
         [OPEN_PART, STEEL,
          fix(Selector(kind="holes", diameter_mm=10.3), "bolts"),
          force(1000, "along_y", Selector(kind="role", role="top"), "pull"),
          *TAIL],
         [expect_faces("bolts", "holes", holes=4, diameter_mm=10.31),
          expect_faces("pull", "extreme", area_mm2=199.4),
          expect_range("von_mises_max", 1, 500, 1e6, " MPa")],
         sample=r"tutorial\cosmosxpress\aw_anchor_plate.sldprt"),

    Case("control_arm", "SolidWorks sample: control arm held by its big "
                        "bore, 2 kN sideways through the eye at the far end",
         "static",
         [OPEN_PART, STEEL,
          fix(Selector(kind="holes", axis="x", side="min"), "bore"),
          force(2000, "against_y", Selector(kind="holes", axis="x", side="max"),
                "eye load"), *TAIL],
         [expect_faces("bore", "holes", holes=1, diameter_mm=60),
          expect_faces("eye load", "holes", holes=1, diameter_mm=14),
          # The eye is two half-cylinder faces; before the force was
          # divided among them, the supports carried 4 kN, not 2.
          expect_balance,
          expect_range("von_mises_max", 1, 500, 1e6, " MPa")],
         sample=r"tutorial\cosmosxpress\aw_control_arm.sldprt"),

    Case("shaft_shoulder", "SolidWorks sample: stepped shaft stood on its "
                           "big end, 1 kN pressed down on the shoulder",
         "static",
         [OPEN_PART, STEEL,
          fix(Selector(kind="role", role="fixed_end"), "base"),
          force(1000, "against_z",
                Selector(kind="extreme", axis="z", side="max", index=1),
                "shoulder load"), *TAIL],
         [expect_faces("base", "extreme", area_mm2=285.0),
          # index 0 at max Z is the 41 mm2 tip; index 1 is the shoulder.
          expect_faces("shoulder load", "extreme", area_mm2=131.7),
          # F*L/(E*A) for the 101.6 mm below the shoulder: 1.74 um.
          expect_range("displacement_max", 1.2, 3.0, 1e-6, " um")],
         sample=r"Simulation Examples\Educational Examples\shaft.sldprt"),
]


# --------------------------------------------------------------------------
# Running
# --------------------------------------------------------------------------
def _connect():
    app = win32com.client.Dispatch("SldWorks.Application")
    app.Visible = True
    return app


def _open_sample(app, rel: str):
    """A fresh copy every time: a study left in the part by the last run
    would otherwise collide with this one's by name."""
    from ...templates.runtime import _member
    WORK.mkdir(parents=True, exist_ok=True)
    src = Path(rel) if Path(rel).is_absolute() else SAMPLES / rel
    dst = WORK / src.name
    # By path: CloseDoc wants the title, which has no extension, so
    # closing "valve.sldprt" left "valve" open from the previous run.
    for doc in list(_member(app, "GetDocuments") or []):
        if str(_member(doc, "GetPathName") or "").lower() == str(dst).lower():
            app.CloseDoc(_member(doc, "GetTitle"))
    shutil.copy2(src, dst)
    err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    wrn = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
    model = app.OpenDoc6(str(dst), 1, 1, "", err, wrn)
    if not model:
        raise RuntimeError(f"could not open {dst} (error {err.value})")
    app.ActivateDoc3(dst.name, False, 0, err)


def run_case(app, case: Case) -> list[str]:
    plan = Plan(domain=case.domain, study_name=f"Real_{case.name}",
                summary=case.about, ops=case.ops)
    resolved, questions, gaps = planner.finalise(plan)
    if questions or gaps:
        return [f"plan incomplete: {[q.question for q in questions]} {gaps}"]
    out = emitter.emit(case.domain, resolved, study_name=plan.study_name,
                       summary=plan.summary)
    verdict = validate(out.code, case.domain)
    if not verdict.ok:
        return [f"validator: {verdict.report()}"]

    if case.sample:
        _open_sample(app, case.sample)
    if case.prepare:
        case.prepare(app)

    result = runner.run_script(out.code, case.domain)
    receipt = result.receipt or {}
    case.results = receipt.get("results", {}) or {}
    if not result.ok:
        return [f"run failed: {result.error}"]
    checks = physics.crosscheck(case.domain, resolved, case.results, receipt)
    problems = []
    expects = list(case.expect)
    # Every structural run must return its load through its supports:
    # that check caught a force SolidWorks applied twice over.
    if case.domain in ("static", "nonlinear") and expect_balance not in expects:
        expects.append(expect_balance)
    for expect in expects:
        problems.extend(expect(receipt, checks))
    for w in receipt.get("warnings", []):
        print(f"      warning: {w}")
    for f in checks.findings:
        print(f"      check: {f.level}: {f.message}")
    return problems


def _open_titles(app) -> set[str]:
    from ...templates.runtime import _member
    return {_member(d, "GetTitle") for d in (_member(app, "GetDocuments") or [])}


def _close_new(app, before: set[str]) -> None:
    """Close what this case opened -- the part it built, the sample copy
    -- and nothing that was open before it. Without this every run left
    a part behind, and a lab PC ended the day with thirty of them."""
    for title in _open_titles(app) - before:
        app.CloseDoc(title)


def main(argv: list[str]) -> int:
    chosen = [c for c in CASES if not argv or any(a in c.name for a in argv)]
    app = _connect()
    failures = 0
    for case in chosen:
        print(f"\n=== {case.name}: {case.about}", flush=True)
        started = time.time()
        before = _open_titles(app)
        try:
            problems = run_case(app, case)
        except Exception as exc:          # a harness error is a failure too
            problems = [f"harness: {exc}"]
        finally:
            _close_new(app, before)
        shown = {k: v for k, v in case.results.items()
                 if isinstance(v, (int, float))}
        print(f"    results: {json.dumps(shown)}")
        print(f"    {'FAIL' if problems else 'ok'} "
              f"({time.time() - started:.0f} s)")
        for p in problems:
            print(f"      - {p}")
        failures += bool(problems)
    print(f"\n{len(chosen) - failures}/{len(chosen)} cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
