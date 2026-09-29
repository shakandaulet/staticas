"""
The runtime prelude: everything a generated script needs, already written.

WHY THIS EXISTS AS A FILE RATHER THAN AS PROMPT TEXT
----------------------------------------------------
A generated script is emitted by pasting this module in ahead of the
plan-specific calls. The model does not write any of it and cannot
alter it, which means the VARIANT null, the byref error codes, the
property-without-parentheses calls and the mesh floor are correct in
every script this system produces -- not correct most of the time,
which is what "the model was told about the quirks" buys.

What varies between two simulations is a list of calls with numbers in
them. That is the part the planner produces. The machinery underneath
it is identical every time, so it lives here, is read by the chunker
into the knowledge base, and is unit-testable without SolidWorks in the
room.

WHAT IS AND IS NOT CONFIRMED
----------------------------
Calls marked CONFIRMED come from scripts that ran and produced sane
numbers. Calls marked UNCONFIRMED are reconstructed from documentation
and may have the wrong signature on your build; each one is wrapped so
that failing produces a message naming the call and what it was trying
to do, rather than a COM error code. Flow Simulation is a different
add-in with a different object model: connecting to it, solving a
project and reading its goals are confirmed; building a project is not
done here at all.

This module is standalone: it runs on a lab PC with pywin32 and needs
nothing else from this project.
"""

# --- BEGIN RUNTIME PRELUDE (inlined verbatim into generated scripts) ---
from __future__ import annotations

import json
import math
import sys
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import pythoncom
import win32com.client

# QUIRK: a COM "Nothing". Python's bare None raises a type error that
# names neither the argument nor the call.  CONFIRMED
NOTHING = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)

DEFAULT_MATERIAL_DB = (
    r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\lang\english"
    r"\sldmaterials\solidworks materials.sldmat"
)

# The mesh floor, in millimetres. Mirrors the server-side validator so a
# downloaded script cannot be edited past the limit and still run --
# the check exists on both sides because only one of them is under this
# project's control once the file is downloaded.
MESH_MAX_FLOOR_MM = 25.0
MESH_MIN_FLOOR_MM = 1.0

# Study type codes for CreateNewStudy3, from swsAnalysisStudyType_e in
# the SOLIDWORKS Simulation API reference. Nonlinear is 5, not 4: 4 is
# Optimization, which accepts the same calls and answers a different
# question. create_study() reads the type back and stops on a mismatch.
STUDY_TYPES = {
    "static": 0, "frequency": 1, "buckling": 2, "thermal": 3,
    "nonlinear": 5, "drop": 6, "fatigue": 7,
}

RECEIPT: dict = {
    "run_at": datetime.now().isoformat(timespec="seconds"),
    "steps": [],
    "results": {},
    "warnings": [],
    "selection_log": [],
}

# What the run needs to remember between steps and does not belong in the
# receipt as it stands: COM objects (the part, the restrained faces, the
# user's selection) and running totals of what was applied, which
# read_results() turns into the equilibrium and heat-balance checks.
_STATE: dict = {"selection": None, "model": None, "restrained": [],
                "forces": [], "heat_in_W": 0.0, "convection": [],
                "fixed_temperatures": 0, "radiation": []}


def _err():
    """A fresh byref long for a Simulation call's error code.  CONFIRMED"""
    return win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)


# Unit codes, so a value written into Simulation is read back in the unit
# it was written in rather than in whatever the document happens to be
# set to. From the API reference: swsUnitSystem_e SI = 0,
# swsTemperatureUnit_e Kelvin = 0, swsStrengthUnit_e Pascal = 0,
# swsLinearUnit_e metres = 2.
UNIT_SI = 0
UNIT_KELVIN = 0
UNIT_PASCAL = 0
UNIT_METRES = 2

# Result components. VON is 9: 8 is P3, the third principal stress,
# which is a different number that looks just as plausible in a receipt.
STRESS_VON_MISES = 9
DISPLACEMENT_RESULTANT = 3
THERMAL_TEMPERATURE = 0
RADIATION_SURFACE_TO_AMBIENT = 0

# swsMeshQuality_e, from the type library: Draft 0, High 1.  CONFIRMED
# This project had them the other way round for its whole life, so every
# run it called "draft" was second-order -- which is why the reference
# beam matched F*L^3/3EI to 0.03%. Measured on that beam: quality 0 gave
# 1 915 nodes and 13.67 mm (10% stiff, first-order), quality 1 gave
# 12 123 nodes and 15.24 mm.
MESH_DRAFT = 0
MESH_HIGH = 1


def _member(obj, name):
    """Read a COM member whether pywin32 returns a value or a method.

    With no type information pywin32 invokes a no-argument member the
    moment it is touched and hands back the result; with type
    information it hands back a bound method that still has to be
    called. That difference is the whole reason `study.RunAnalysis`
    works on one machine and quietly does nothing on another, and why
    the same line written with parentheses raises a TypeError about a
    non-callable int. Going through here works either way.

    The COM check is not decoration: a returned COM object is itself
    callable -- pywin32 gives CDispatch a __call__ that forwards to the
    object's default member -- so testing `callable` alone calls the
    face that was just fetched and raises "Member not found"."""
    member = getattr(obj, name)
    if hasattr(member, "_oleobj_"):
        return member          # a COM object: it was invoked on access
    return member() if callable(member) else member


def _set_unit(obj, code: int, what: str) -> None:
    """Pin a load's units before writing a value into it.

    Simulation reads the number in whatever unit the load carries, so
    298.15 written into a load left in Celsius is a part at 571 K. Not
    every build exposes the property, so failing to set it is a warning
    and not the end of the run."""
    try:
        obj.Unit = code
    except Exception as exc:
        warn(f"Could not set the units on {what} ({exc}); the value is "
             f"being written in the study's own unit system.")


def step(name: str, detail: str = "") -> None:
    line = f"{name}: {detail}" if detail else name
    print(f"  {line}", flush=True)
    RECEIPT["steps"].append(line)


def warn(message: str) -> None:
    print(f"  ! {message}", flush=True)
    RECEIPT["warnings"].append(message)


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------
def connect_to_solidworks():
    """Attach to a RUNNING SolidWorks session.  CONFIRMED

    Dispatch starts SolidWorks if it is not open, which on a lab PC
    means a licence check and a two-minute splash screen before
    anything reports back. Saying so beats an apparently hung script."""
    step("Connecting to SolidWorks")
    app = win32com.client.Dispatch("SldWorks.Application")
    app.Visible = True
    try:
        exe = str(_member(app, "GetExecutablePath") or "")
        _STATE["sw_dir"] = str(Path(exe).parent if exe.lower().endswith(".exe")
                               else Path(exe))
    except Exception:
        pass                  # only used to find the material library
    return app


def active_part(app, required: bool = True):
    """The document already open, which is the normal case.  CONFIRMED"""
    model = app.ActiveDoc
    if model is None and required:
        raise RuntimeError(
            "No document is open in SolidWorks. Open the part you want "
            "simulated, then run this script again.")
    _STATE["model"] = model
    return model


def _part_template(app) -> str:
    """A part template to start a new part from.  CONFIRMED

    The preference is empty on a profile nobody has taken through
    Tools > Options > File Locations, which is the state a fresh install
    is in -- and NewDocument("") opens nothing and returns None, so the
    script stopped on a machine where the templates were sitting there
    all along. Look for one before giving up."""
    template = app.GetUserPreferenceStringValue(8)   # swDefaultTemplatePart
    if template and Path(template).exists():
        return template

    for root in (Path(r"C:\ProgramData\SOLIDWORKS"),
                 Path(r"C:\ProgramData\SolidWorks")):
        if not root.exists():
            continue
        found = sorted(root.glob("SOLIDWORKS */templates/*.prtdot"))
        named = [p for p in found if p.stem.lower() == "part"]
        if named or found:
            return str((named or found)[-1])

    raise RuntimeError(
        "No part template was found. Set one under Tools > Options > File "
        "Locations > Document Templates, or open the part you want "
        "simulated so the plan can use the active document instead.")


def new_part(app):
    """A fresh part from the machine's default template.  CONFIRMED"""
    template = _part_template(app)
    model = app.NewDocument(template, 0, 0, 0)
    if not model:
        raise RuntimeError(
            f"Could not open a new part from {template}. Check that the "
            f"template opens by hand.")
    _STATE["model"] = model
    _STATE["app"] = app
    _STATE["built"] = True
    return model


def keep_part() -> None:
    """Save the part this script built, and close the ones earlier runs
    left open.  CONFIRMED

    Every run that builds a part used to leave it open and unsaved, and a
    lab PC ended the day with thirty of them. The part is saved -- study
    and all -- into runs/parts, so it can be opened again; this run's
    stays on screen to be looked at; earlier runs' parts from that folder
    are closed if they have nothing unsaved. A part the user opened is
    never saved or closed from here."""
    app, model = _STATE.get("app"), _STATE.get("model")
    if not (app and model and _STATE.get("built")):
        return
    folder = Path(__file__).resolve().parent / "runs" / "parts"
    folder.mkdir(parents=True, exist_ok=True)
    name = RECEIPT.get("study", {}).get("name", "part")
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S}_{name}.SLDPRT"
    try:
        if model.Extension.SaveAs(str(path), 0, 1, NOTHING, _err(), _err()):
            RECEIPT["part_file"] = str(path)
            step("Part saved", str(path))
    except Exception as exc:
        warn(f"The built part was not saved ({exc}); it is still open.")
    closed = 0
    for doc in list(_member(app, "GetDocuments") or []):
        try:
            where = str(_member(doc, "GetPathName") or "")
            if (where and Path(where).parent == folder
                    and Path(where) != path and not _member(doc, "GetSaveFlag")):
                app.CloseDoc(_member(doc, "GetTitle"))
                closed += 1
        except Exception:
            continue
    if closed:
        step("Closed parts left by earlier runs", str(closed))


SIMULATION_PROG_IDS = ("SldWorks.Simulation", "CosmosWorks.CosmosWorks",
                       "SldWorks.Simulation.1")


def _simulation_addin_dlls(app) -> list:
    """Candidate paths to cosworks.dll, best guess first.  CONFIRMED

    Derived from the running SolidWorks rather than hardcoded, so a
    non-default install location still works; the literal path is only a
    fallback."""
    candidates = []
    try:
        exe = _member(app, "GetExecutablePath")
        if exe:
            candidates.append(Path(exe).parent / "Simulation" / "cosworks.dll")
    except Exception:
        pass
    candidates.append(Path(r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS"
                           r"\Simulation\cosworks.dll"))
    return [p for p in candidates if p.exists()]


def connect_simulation(app):
    """The Simulation (CosmosWorks) add-in.  CONFIRMED

    Three ProgIDs because which one resolves depends on the installed
    edition.

    If none of them resolves, LOAD the add-in rather than giving up.
    Simulation being installed is not the same as being enabled: it is a
    tick-box in Tools > Add-Ins that is off on a fresh profile, and on a
    machine where nobody has ticked it every script stopped here with an
    instruction to go and tick it. LoadAddIn returns 0 on success."""
    step("Connecting to SolidWorks Simulation")
    addin = None
    for prog_id in SIMULATION_PROG_IDS:
        addin = app.GetAddInObject(prog_id)
        if addin:
            break

    if not addin:
        for dll in _simulation_addin_dlls(app):
            status = app.LoadAddIn(str(dll))
            step("Loading the Simulation add-in", f"{dll.name} -> {status}")
            if status == 0:
                break
        for prog_id in SIMULATION_PROG_IDS:
            addin = app.GetAddInObject(prog_id)
            if addin:
                break

    if not addin:
        raise RuntimeError(
            "The Simulation add-in is not available and could not be "
            "loaded. Check that SOLIDWORKS Simulation is installed and "
            "licensed, then enable it under Tools > Add-Ins, including "
            "the 'Start Up' column.")
    cw = addin.CosmosWorks
    if not cw:
        raise RuntimeError(
            "The Simulation add-in loaded but did not initialise. Restart "
            "SolidWorks with the add-in enabled at start-up.")
    return cw


# The Flow Simulation add-in: its COM class, and the type library its
# objects need before they answer to their method names.
FLOW_ADDIN_CLSID = "{2228AF70-4E4C-43FF-9021-0E187326CDBB}"
FLOWORKS_TYPELIB = ("{47A51DD7-BD1E-4B5C-97BE-1DAAE5C7E715}", 0, 4, 0)


def _flow_addin_dll() -> str:
    """Where FW03.dll is, from the add-in's own COM registration."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT,
                             rf"CLSID\{FLOW_ADDIN_CLSID}\InprocServer32")
        return str(winreg.QueryValue(key, None))
    except Exception:
        return (r"C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS Flow Simulation"
                r"\binCFW\FW03.dll")


def connect_flow(app):
    """The Flow Simulation add-in, loaded when it is not.  CONFIRMED

    The ProgID is FloWorks.App -- not the FlowSimulation.* names this
    used to try, which nothing registers. Like Simulation, being
    installed is not being loaded: on a freshly started SolidWorks the
    add-in object is None until LoadAddIn(FW03.dll) returns 0.

    The typed wrappers are generated first. Without them every Flow
    object arrives untyped and every call by name fails."""
    from win32com.client import gencache
    gencache.EnsureModule(*FLOWORKS_TYPELIB)
    addin = app.GetAddInObject("FloWorks.App")
    if addin is None:
        status = app.LoadAddIn(_flow_addin_dll())
        step("Loading the Flow Simulation add-in", f"FW03.dll -> {status}")
        addin = app.GetAddInObject("FloWorks.App")
    if addin is None:
        raise RuntimeError(
            "Flow Simulation is not available in this session. It is a "
            "separate add-in and a separate licence from SolidWorks "
            "Simulation; enable it under Tools > Add-Ins.")
    return addin


def flow_project(app, model, settings: dict | None = None):
    """The Flow Simulation project of the open part, or None.  CONFIRMED

    This runtime does not build Flow projects: their parameters sit
    behind identifiers the registered API does not name, and the API
    that does name them is not registered by the SDK installer. What it
    does is run a project the wizard built -- the checklist printed
    above is that wizard, filled in -- and read the goals back. So no
    project is not an error: it is the step before this one."""
    try:
        addin = connect_flow(app)
        project = addin.GetAPI().GetDocument(model).IActiveProject
    except Exception as exc:
        warn(f"Flow Simulation could not be reached ({exc}). The checklist "
             f"above is what to set up by hand.")
        return None
    if project is None:
        warn("This part has no Flow Simulation project yet. Create it with "
             "the wizard from the checklist above, add the goals it lists, "
             "and run this script again: it will then solve the project and "
             "read the goals back.")
        return None
    name = str(_member(project, "ProjectName"))
    RECEIPT["flow_project_name"] = name
    step("Flow project", name)
    want = (settings or {}).get("analysis_type")
    try:
        got = {0: "external", 1: "internal"}.get(int(project.ProblemType))
    except Exception:
        got = None
    if want and got and want != got:
        warn(f"The project '{name}' is set up as {got} flow and the plan asked "
             f"for {want}. It is solved as it is set up -- change the analysis "
             f"type in the wizard if the plan is right.")
    return project


def solve_flow(project) -> None:
    """Mesh and solve, and wait for the solver to finish.  CONFIRMED

    Solve(build mesh, recalculate, close monitor, run report). The other
    route -- PrepareToRun, then CalculationData.RunSolver = True -- started
    the solver as a TCP server waiting for a monitor that never came:
    the log stopped at "TCP server started", and the results file it
    left was byte for byte the initial state. Solve runs to the end: the
    'hydraulic loss' example in about a minute, the 'drag coefficient'
    one in 91 s."""
    step("Solving the Flow project", "minutes, not seconds")
    started = datetime.now()
    if not project.Solve(True, True, True, False):
        raise RuntimeError(
            "The Flow Simulation solve did not finish. Open the project in "
            "SolidWorks and run it from the Flow Simulation menu to see the "
            "solver's own message.")
    step("Flow solve finished",
         f"{(datetime.now() - started).total_seconds():.0f} s")


# Goals the solver adds for itself; the user never defined these.
FLOW_SERVICE_GOALS = {"dm/m", "Calculate_inc time/NC_Fluid", "Serv Press",
                      "Serv Temp", "Serv Heatf"}


def read_flow_goals(project) -> dict:
    """Every goal the user defined, at the end of the solve.  CONFIRMED

    LoadLastResults, then GetGoals. Goals are numbered from 1 -- index 0
    returns nothing -- and the list ends with the solver's own service
    goals, which are left out. The AVERAGED value is the one reported:
    it is what the solver judged convergence on, and on the drag example
    the last iterate (1.056) sat 7% off the average (0.989).

    From the goals, the two numbers the questions are usually about: a
    pressure drop (the spread of the pressure goals) and a drag
    coefficient (a goal of that name)."""
    post = project.GetPostDocAPI()
    post.LoadLastResults()
    goals = post.GetGoals()
    count = int(goals.GetGoalsCount())
    out: dict = {}
    for i in range(1, count + 1):
        g = goals.GetGoalByIndex(i)
        if g is None:
            continue
        name = str(g.GetGoalName())
        try:
            service = bool(g.IsServiceGoal())
        except Exception:
            service = False
        if service or name in FLOW_SERVICE_GOALS:
            continue
        out[name] = {"value": float(g.GetValue()),
                     "average": float(g.GetAvValue()),
                     "progress_pct": float(g.GetProgress())}
    RECEIPT["results"]["goals"] = out
    step("Goals read", ", ".join(f"{k} = {v['average']:.5g}"
                                 for k, v in out.items()) or "none defined")
    pressures = [v["average"] for k, v in out.items() if "Pressure" in k]
    if len(pressures) >= 2:
        RECEIPT["results"]["pressure_drop_Pa"] = max(pressures) - min(pressures)
    for k, v in out.items():
        if "drag coefficient" in k.lower() or k.strip().lower() == "cd":
            RECEIPT["results"]["drag_coefficient"] = v["average"]
        elif "Force" in k and "drag_force" not in RECEIPT["results"]:
            RECEIPT["results"]["drag_force"] = abs(v["average"])
    unconverged = [k for k, v in out.items() if v["progress_pct"] < 100.0]
    if unconverged:
        warn(f"Not converged on: {', '.join(unconverged)}. The solver stopped "
             f"before these goals settled; read them as indicative.")
    return out


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------
PLANE_FRONT, PLANE_TOP, PLANE_RIGHT = 0, 1, 2


def reference_planes(model) -> list:
    """The three default planes, found by TYPE and never by name.

    *** NEVER SELECT A PLANE BY THE NAME "Front Plane". ***

    Plane names are localised: on a GOST template they are 'Спереди',
    'Сверху' and 'Справа', so SelectByID2("Front Plane", "PLANE", ...)
    selects nothing and returns False. It then fails SILENTLY --
    InsertSketch2 opens a sketch on some default plane anyway, the
    section draws, the beam extrudes and the study solves, with the very
    first selection in the script having never succeeded.

    The first three RefPlane features in the tree are Front, Top and
    Right in creation order, whatever the interface calls them.

    FirstFeature, GetNextFeature and GetTypeName2 come back as values
    rather than methods under pywin32, hence _member()."""
    planes = []
    feat = _member(model, "FirstFeature")
    while feat:
        try:
            if _member(feat, "GetTypeName2") == "RefPlane":
                planes.append(feat)
        except Exception:
            pass          # not every folder feature exposes GetTypeName2
        feat = _member(feat, "GetNextFeature")
    if len(planes) < 3:
        raise RuntimeError(
            f"Expected the three default reference planes, found "
            f"{len(planes)}. Is this a part made from a standard template?")
    return planes[:3]


def select_plane(model, index: int = PLANE_FRONT, mark: int = 0):
    """Select one default plane and hand back the selected object."""
    plane = reference_planes(model)[index]
    model.ClearSelection2(True)
    if not plane.Select2(False, mark):
        raise RuntimeError(f"Could not select reference plane {index}.")
    return model.SelectionManager.GetSelectedObject6(1, -1)


def _open_sketch(model, plane_index=PLANE_FRONT):
    """Start a sketch on a default plane.  CONFIRMED

    AddToDB writes entities straight into the sketch, skipping the
    inference engine. Without it a line drawn near an existing point
    snaps onto it, and an I-beam flange 2 mm from a web comes out 0 mm
    from it -- a different section that still extrudes."""
    select_plane(model, plane_index)
    model.InsertSketch2(True)
    sm = model.SketchManager
    sm.AddToDB = True
    return sm


def _close_sketch(model):
    """Close the open sketch and hand back its feature.  CONFIRMED

    InsertSketch2 both opens and closes a sketch -- there is no separate
    exit call, and looking for one is a well-worn dead end. The feature
    is taken from the END of the tree rather than looked up as "Sketch1":
    that name is localised ("Эскиз1") and counts up with every sketch,
    so a second sketch in the same part was never going to be called
    Sketch1."""
    model.SketchManager.AddToDB = False
    model.InsertSketch2(True)
    return model.FeatureByPositionReverse(0)


def _polyline(sm, points):
    """A closed outline through `points`, in sketch coordinates."""
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        sm.CreateLine(x1, y1, 0, x2, y2, 0)


def _extrude(model, sketch, depth):
    """Blind extrusion of one sketch, merged into the body.  CONFIRMED

    FeatureExtrusion2 takes ~24 positional arguments; this combination
    is the tested one. The sketch is selected by the name its own feature
    reports, never by a name typed here. Direction is the plane normal:
    +Z from the Front plane, +Y from the Top plane."""
    name = sketch.Name
    model.ClearSelection2(True)
    model.Extension.SelectByID2(name, "SKETCH", 0, 0, 0, False, 0,
                                NOTHING, 0)
    feat = model.FeatureManager.FeatureExtrusion2(
        True, False, False, 0, 0, depth, 0.01, False, False, False, False,
        0, 0, False, False, False, False, True, True, True, 0, 0, False)
    if not feat:
        raise RuntimeError(
            f"Extruding {name} failed. The outline is probably not closed, "
            f"or it crosses itself.")
    model.ViewZoomtofit2()
    return feat


def build_rectangular_beam(model, width, height, length):
    """CONFIRMED. All dimensions in METRES -- the API is SI regardless
    of the document's display units. Section in XY, span along +Z."""
    step("Drawing beam", f"{width} x {height} x {length} m")
    sm = _open_sketch(model, PLANE_FRONT)
    sm.CreateCornerRectangle(0, 0, 0, width, height, 0)
    _extrude(model, _close_sketch(model), length)
    return {"kind": "rectangular_beam", "width": width, "height": height,
            "length": length, "area": width * height,
            "I": width * height ** 3 / 12.0}


def build_box(model, width, height, length, wall=0.0):
    """A solid block, or a rectangular hollow section when `wall` is set.
    CONFIRMED -- two rectangles in one sketch extrude as a tube."""
    if not wall:
        geo = build_rectangular_beam(model, width, height, length)
        geo["kind"] = "box"
        return geo
    if 2.0 * wall >= min(width, height):
        raise RuntimeError(
            f"A {wall * 1e3:g} mm wall does not fit in a "
            f"{width * 1e3:g} x {height * 1e3:g} mm section.")
    step("Drawing box section",
         f"{width} x {height} m, wall {wall} m, L={length} m")
    sm = _open_sketch(model, PLANE_FRONT)
    sm.CreateCornerRectangle(0, 0, 0, width, height, 0)
    sm.CreateCornerRectangle(wall, wall, 0, width - wall, height - wall, 0)
    _extrude(model, _close_sketch(model), length)
    wi, hi = width - 2.0 * wall, height - 2.0 * wall
    return {"kind": "box", "width": width, "height": height, "wall": wall,
            "length": length, "area": width * height - wi * hi,
            "I": (width * height ** 3 - wi * hi ** 3) / 12.0}


def build_round_bar(model, diameter, length):
    """CONFIRMED pattern, circle instead of rectangle. Axis along +Z."""
    step("Drawing round bar", f"d={diameter} m, L={length} m")
    sm = _open_sketch(model, PLANE_FRONT)
    sm.CreateCircleByRadius(0, 0, 0, diameter / 2.0)
    _extrude(model, _close_sketch(model), length)
    r = diameter / 2.0
    return {"kind": "round_bar", "diameter": diameter, "length": length,
            "area": math.pi * r ** 2,
            "I": math.pi * diameter ** 4 / 64.0}


def build_tube(model, outer_diameter, wall, length):
    """CONFIRMED pattern. Two concentric circles extruded together."""
    step("Drawing tube", f"OD={outer_diameter} m, wall={wall} m")
    inner = outer_diameter - 2.0 * wall
    if inner <= 0:
        raise RuntimeError(
            f"A {wall * 1e3:g} mm wall does not fit in a "
            f"{outer_diameter * 1e3:g} mm tube.")
    sm = _open_sketch(model, PLANE_FRONT)
    sm.CreateCircleByRadius(0, 0, 0, outer_diameter / 2.0)
    sm.CreateCircleByRadius(0, 0, 0, inner / 2.0)
    _extrude(model, _close_sketch(model), length)
    return {"kind": "tube", "outer_diameter": outer_diameter, "wall": wall,
            "length": length,
            "area": math.pi * (outer_diameter ** 2 - inner ** 2) / 4.0,
            "I": math.pi * (outer_diameter ** 4 - inner ** 4) / 64.0}


def build_i_beam(model, flange_width, height, web_thickness,
                 flange_thickness, length):
    """An I-section, strong axis horizontal: web along Y, span along +Z.
    CONFIRMED -- a twelve-line outline drawn with AddToDB on."""
    b, h, tw, tf = flange_width, height, web_thickness, flange_thickness
    if tw >= b or 2.0 * tf >= h:
        raise RuntimeError(
            f"That I-section does not close: web {tw * 1e3:g} mm in a "
            f"{b * 1e3:g} mm flange, or two {tf * 1e3:g} mm flanges in "
            f"{h * 1e3:g} mm of depth.")
    step("Drawing I-beam", f"{b} x {h} m, web {tw} m, flange {tf} m, "
                           f"L={length} m")
    xl, xr = (b - tw) / 2.0, (b + tw) / 2.0
    sm = _open_sketch(model, PLANE_FRONT)
    _polyline(sm, [(0, 0), (b, 0), (b, tf), (xr, tf), (xr, h - tf),
                   (b, h - tf), (b, h), (0, h), (0, h - tf), (xl, h - tf),
                   (xl, tf), (0, tf)])
    _extrude(model, _close_sketch(model), length)
    return {"kind": "i_beam", "flange_width": b, "height": h,
            "web_thickness": tw, "flange_thickness": tf, "length": length,
            "area": 2.0 * b * tf + (h - 2.0 * tf) * tw,
            "I": (b * h ** 3 - (b - tw) * (h - 2.0 * tf) ** 3) / 12.0}


def build_plate(model, width, length, thickness):
    """CONFIRMED. Lies flat: X across, Z along (from 0 to -length),
    thickness up +Y -- the Top plane's sketch Y runs along -Z."""
    step("Drawing plate", f"{width} x {length} x {thickness} m")
    sm = _open_sketch(model, PLANE_TOP)
    sm.CreateCornerRectangle(0, 0, 0, width, length, 0)
    _extrude(model, _close_sketch(model), thickness)
    return {"kind": "plate", "width": width, "length": length,
            "thickness": thickness, "area": width * thickness,
            "I": width * thickness ** 3 / 12.0}


def build_plate_with_hole(model, width, length, thickness, hole_diameter):
    """The plate above with a round hole through its middle.  CONFIRMED

    The textbook stress-concentration part: pulled along its length, the
    peak at the hole edge is Kt times the net-section stress, and unlike
    the peak at a fixed face that one is real -- it converges."""
    if hole_diameter >= width:
        raise RuntimeError(
            f"A {hole_diameter * 1e3:g} mm hole does not fit in a "
            f"{width * 1e3:g} mm wide plate.")
    step("Drawing plate with hole", f"{width} x {length} x {thickness} m, "
                                    f"hole {hole_diameter} m")
    sm = _open_sketch(model, PLANE_TOP)
    sm.CreateCornerRectangle(0, 0, 0, width, length, 0)
    sm.CreateCircleByRadius(width / 2.0, length / 2.0, 0, hole_diameter / 2.0)
    _extrude(model, _close_sketch(model), thickness)
    return {"kind": "plate_with_hole", "width": width, "length": length,
            "thickness": thickness, "hole_diameter": hole_diameter,
            "area": width * thickness,
            "net_area": (width - hole_diameter) * thickness}


def build_angle_bracket(model, length, height, width, thickness,
                        fillet_radius=0.0, hole_diameter=0.0):
    """An L-bracket: a base along X, an upright along Y, both `width` deep
    along Z, with an optional fillet in the inside corner and two bolt
    holes through the base.  CONFIRMED

    Built as two extrusions that merge, not as one L and a cut: the
    upright's outline carries the fillet, the base's outline carries the
    holes, and neither needs FeatureCut's twenty-seven arguments. The
    holes sit in the part of the base the upright's outline does not
    cover, so the merge cannot fill them back in."""
    a, h, B, t, r, d = (length, height, width, thickness, fillet_radius,
                        hole_diameter)
    if t >= min(a, h):
        raise RuntimeError("The bracket is thicker than its legs are long.")
    step("Drawing angle bracket",
         f"base {a} m, upright {h} m, width {B} m, t={t} m, fillet {r} m, "
         f"holes {d} m")

    sm = _open_sketch(model, PLANE_FRONT)
    if r > 0:
        for (x1, y1), (x2, y2) in [((0, 0), (t + r, 0)),
                                   ((t + r, 0), (t + r, t)),
                                   ((t, t + r), (t, h)),
                                   ((t, h), (0, h)),
                                   ((0, h), (0, 0))]:
            sm.CreateLine(x1, y1, 0, x2, y2, 0)
        # The fillet: a quarter arc round (t+r, t+r), clockwise (-1) from
        # the point below the centre to the point left of it.
        sm.CreateArc(t + r, t + r, 0, t + r, t, 0, t, t + r, 0, -1)
    else:
        _polyline(sm, [(0, 0), (t, 0), (t, h), (0, h)])
    _extrude(model, _close_sketch(model), B)

    holes = []
    sm = _open_sketch(model, PLANE_TOP)
    # Top-plane sketch Y runs along -Z, so the base's 0..B in Z is
    # 0..-B here.
    sm.CreateCornerRectangle(0, 0, 0, a, -B, 0)
    if d > 0:
        x = (t + r + a) / 2.0
        if x - d / 2.0 <= t + r or x + d / 2.0 >= a:
            raise RuntimeError(
                f"A {d * 1e3:g} mm hole does not fit in the "
                f"{(a - t - r) * 1e3:g} mm of base clear of the upright.")
        zs = [B / 4.0, 3.0 * B / 4.0] if B / 2.0 > 1.5 * d else [B / 2.0]
        for z in zs:
            sm.CreateCircleByRadius(x, -z, 0, d / 2.0)
            holes.append([x, z])
    _extrude(model, _close_sketch(model), t)
    return {"kind": "angle_bracket", "length": a, "height": h, "width": B,
            "thickness": t, "fillet_radius": r, "hole_diameter": d,
            "holes": holes}


def get_body(model):
    """The first solid body of the active part.  CONFIRMED

    GetBodies2(0, True) -> visible solid bodies. The list can be empty
    on an assembly or a surface body, which is a different failure from
    a missing document and is worth saying so."""
    part = model
    bodies = part.GetBodies2(0, True)
    if not bodies:
        raise RuntimeError(
            "No solid body found in the active document. An assembly or a "
            "surface model needs a different setup than this script does.")
    return bodies[0] if isinstance(bodies, (list, tuple)) else bodies


# ---------------------------------------------------------------------------
# Selection -- the part that decides whether the load lands where it was meant
# ---------------------------------------------------------------------------
def _face_centre_and_area(face):
    """(centre, area) for one face, from its bounding box.  CONFIRMED

    Not GetMassProperties: that is not a member of IFace2 on every
    build -- it is missing on the 2024 install this was tested against,
    where it fails with a bare AttributeError naming nothing useful.
    GetBox and GetArea are both there.

    The box centre is NOT how "the top face" is found any more -- see
    select_extreme -- but it and the area together identify a face well
    enough to tell two apart.

    GetBox returns (x1, y1, z1, x2, y2, z2) in metres."""
    box = _member(face, "GetBox")
    area = float(_member(face, "GetArea") or 0.0)
    if not box or len(box) < 6:
        return (0.0, 0.0, 0.0), area
    centre = ((box[0] + box[3]) / 2.0,
              (box[1] + box[4]) / 2.0,
              (box[2] + box[5]) / 2.0)
    return centre, area


def faces_of(body):
    faces = body.GetFaces()
    if not faces:
        return []
    return list(faces) if isinstance(faces, (list, tuple)) else [faces]


def _read_selection(model) -> list:
    sel = model.SelectionManager
    count = sel.GetSelectedObjectCount2(-1)
    objs = [sel.GetSelectedObject6(i, -1) for i in range(1, count + 1)]
    return [o for o in objs if o]


def capture_selection(model) -> None:
    """Read what the user has highlighted BEFORE anything can clear it.
    CONFIRMED

    The selection is live state, and the script's own steps disturb it:
    a directional force selects the Front plane to state its components
    against, which clears whatever was picked, so a target read after
    that step found nothing. Taken first, it is what the user pointed at
    when they pressed run."""
    _STATE["selection"] = _read_selection(model)
    step("Selection captured", f"{len(_STATE['selection'])} entity(ies)")


def select_active(model, expected_count=None, what="this step"):
    """Whatever the user has highlighted in SolidWorks.  CONFIRMED

    This is how "apply it to THIS edge" is answered honestly. The
    browser cannot see the CAD cursor; SolidWorks can.

    Refusing on an empty selection is the important half -- falling back
    to a guessed face applies the load somewhere the user never chose,
    and the study then runs and reports a number that looks fine."""
    captured = _STATE["selection"]
    objs = list(captured) if captured is not None else _read_selection(model)
    count = len(objs)
    if count == 0:
        raise RuntimeError(
            f"Nothing is selected in SolidWorks, but {what} needs a target.\n"
            f"Click the face or edge it belongs on in the SolidWorks window, "
            f"then run this script again.")
    if expected_count is not None and count != expected_count:
        raise RuntimeError(
            f"{count} entities are selected but {what} expects "
            f"{expected_count}.")
    entry = {"target": what, "kind": "active_selection", "count": count}
    try:
        entry["areas_mm2"] = [round(float(_member(o, "GetArea")) * 1e6, 2)
                              for o in objs]
    except Exception:
        pass                  # an edge or a vertex has no area; fine
    RECEIPT["selection_log"].append(entry)
    areas = entry.get("areas_mm2")
    step("Using your selection",
         f"{count} entity(ies)"
         + (f", {sum(areas):.4g} mm2" if areas else "") + f", for {what}")
    return objs


def select_by_point(model, x, y, z, entity_type="FACE", what="a target"):
    """Click at a derived coordinate.  CONFIRMED

    The coordinate must come from the geometry. A literal breaks -- by
    selecting a DIFFERENT face rather than by failing -- the moment a
    dimension changes."""
    model.ClearSelection2(True)
    model.ViewZoomtofit2()
    model.Extension.SelectByID2("", entity_type, x, y, z, False, 0, NOTHING, 0)
    obj = model.SelectionManager.GetSelectedObject6(1, -1)
    if not obj:
        raise RuntimeError(
            f"Nothing was selected for {what} at ({x:.4g}, {y:.4g}, "
            f"{z:.4g}) m. The point did not land on a {entity_type.lower()}: "
            f"check the dimensions the plan was built from, or select the "
            f"target by hand and re-run with the selection active.")
    RECEIPT["selection_log"].append(
        {"target": what, "kind": "point", "point": [x, y, z]})
    return [obj]


def select_by_name(model, name, entity_type="FACE", what="a target"):
    """CONFIRMED for planes, features and bodies. Sketch entities have
    no stable names -- use a coordinate for those."""
    model.ClearSelection2(True)
    ok = model.Extension.SelectByID2(name, entity_type, 0, 0, 0, False, 0,
                                     NOTHING, 0)
    obj = model.SelectionManager.GetSelectedObject6(1, -1)
    if not ok or not obj:
        raise RuntimeError(
            f"No {entity_type.lower()} named '{name}' for {what}. Names are "
            f"case-sensitive and localised -- on a non-English install "
            f"'Front Plane' may be spelled differently.")
    RECEIPT["selection_log"].append(
        {"target": what, "kind": "named", "name": name})
    return [obj]


_AXES = {"x": 0, "y": 1, "z": 2}


def _plane_normal(face):
    """The OUTWARD unit normal of a planar face, or None.  CONFIRMED

    IFace2.Normal already allows for the face's sense, so it points out
    of the material: the underside of a plate reads -Y, the inside wall
    of an I-beam's web reads towards the flange tip."""
    if not _member(_member(face, "GetSurface"), "IsPlane"):
        return None
    n = face.Normal
    return (float(n[0]), float(n[1]), float(n[2]))


def _cylinder(face):
    """(origin, unit axis, radius, concave) for a cylindrical face, else
    None.  CONFIRMED

    CONCAVE MEANS THE MATERIAL IS OUTSIDE THE CYLINDER: a hole, or the
    inside of a fillet. FaceInSurfaceSense() is True for exactly those --
    checked face by face on two SolidWorks sample parts, 22 cylinders,
    where every bolt hole and bore read True and every boss, rounded end
    and outside corner read False."""
    surf = _member(face, "GetSurface")
    if not _member(surf, "IsCylinder"):
        return None
    p = surf.CylinderParams
    ax = (float(p[3]), float(p[4]), float(p[5]))
    norm = math.sqrt(sum(c * c for c in ax)) or 1.0
    ax = tuple(c / norm for c in ax)
    # One sign per direction, so the two halves of a hole agree.
    lead = next(c for c in ax if abs(c) > 1e-9)
    if lead < 0:
        ax = tuple(-c for c in ax)
    return ((float(p[0]), float(p[1]), float(p[2])), ax, float(p[6]),
            bool(_member(face, "FaceInSurfaceSense")))


def _dot(a, b):
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _body_box(model):
    box = get_body(model).GetBodyBox()
    return [float(v) for v in box]


def _describe_directions(model) -> str:
    """Which way the part's flat faces point, for an error message that
    says what IS there instead of only what is not."""
    counts: dict = {}
    for face in faces_of(get_body(model)):
        n = _plane_normal(face)
        if n is None:
            continue
        for axis, i in _AXES.items():
            for sign, mark in ((1, "+"), (-1, "-")):
                if n[i] * sign >= 0.97:
                    key = f"{mark}{axis.upper()}"
                    counts[key] = counts.get(key, 0) + 1
    parts = [f"{k} ({v})" for k, v in sorted(counts.items())]
    holes = find_holes(model)
    text = "flat faces point " + (", ".join(parts) if parts else "nowhere")
    if holes:
        sizes = sorted({round(h["diameter"] * 1e3, 1) for h in holes})
        text += (f"; {len(holes)} hole(s), "
                 f"diameter {', '.join(f'{s:g}' for s in sizes)} mm")
    return text


def select_extreme(model, axis="z", side="max", what="a target", index=0):
    """The flat faces that point along an axis, outermost first.  CONFIRMED

    This is how "the top face" and "the free end" become reproducible
    without a human present. A face qualifies only if it is PLANAR and
    its outward normal points the way asked (within about 14 degrees);
    the outermost such faces win, and every face at that same level is
    returned together -- both feet of a bracket are "the bottom".
    `index` walks inward a level at a time: index 1 at max Z on a
    stepped shaft is the shoulder.

    It used to rank every face by the centre of its bounding box. That
    picked the right faces on a beam built along Z and the wrong ones on
    real parts: the curved side of a round shaft for "the top" (its box
    centre ties with the ends), a torus on a hook. When nothing flat
    points the right way this now refuses and lists what the part does
    have, rather than handing a curved face to a load that expected a
    flat one."""
    idx = _AXES[axis.lower()]
    sign = 1.0 if side == "max" else -1.0
    label = f"{'+' if sign > 0 else '-'}{axis.upper()}"
    box = _body_box(model)
    extent = box[idx + 3] - box[idx]
    tol = max(1e-5, 1e-3 * extent)

    found = []
    for face in faces_of(get_body(model)):
        n = _plane_normal(face)
        if n is None or n[idx] * sign < 0.97:
            continue
        fbox = _member(face, "GetBox")
        level = fbox[idx + 3] if sign > 0 else fbox[idx]
        found.append((sign * float(level), float(_member(face, "GetArea")),
                      face))
    if not found:
        raise RuntimeError(
            f"No flat face on this part points {label}, so {what} has "
            f"nowhere to go. On this part: {_describe_directions(model)}. "
            f"Select the face in SolidWorks and call it 'this face', or name "
            f"one of those directions or the holes.")

    found.sort(key=lambda t: -t[0])
    levels: list = []
    for item in found:
        if levels and levels[-1][0][0] - item[0] <= tol:
            levels[-1].append(item)
        else:
            levels.append([item])
    if index >= len(levels):
        raise RuntimeError(
            f"{what}: asked for level {index + 1} of the flat faces pointing "
            f"{label}, but there are only {len(levels)}.")
    chosen = levels[index]
    coordinate = sign * chosen[0][0]
    outermost = box[idx + 3] if sign > 0 else box[idx]
    # 5% of the part, not the grouping tolerance: a 0.2 mm edge round
    # proud of a 57 mm plate is not worth a warning, a boss standing 13 mm
    # above the "top" of a 76 mm arm is.
    if index == 0 and abs(outermost - coordinate) > max(tol, 0.05 * extent):
        warn(f"{what}: the outermost flat face pointing {label} is "
             f"{abs(outermost - coordinate) * 1e3:.3g} mm in from the "
             f"part's extreme -- something curved or angled reaches "
             f"further. Check the selection log before trusting the result.")
    area = sum(a for _, a, _ in chosen)
    RECEIPT["selection_log"].append(
        {"target": what, "kind": "extreme", "axis": axis, "side": side,
         "level": index, "coordinate": coordinate, "faces": len(chosen),
         "area": area})
    step("Faces found", f"{len(chosen)} flat, facing {label} at "
                        f"{axis} = {coordinate * 1e3:.4g} mm, "
                        f"{area * 1e6:.4g} mm2, for {what}")
    return [f for _, _, f in chosen]


def find_holes(model) -> list:
    """Every round hole through the body, as groups of faces.  CONFIRMED

    A hole is a CONCAVE cylinder (see _cylinder) whose faces close all
    the way round. SolidWorks often splits a hole into two half-cylinder
    faces, so faces are grouped by shared axis and radius first. Closing
    round is what tells a hole from the inside of a fillet, which is
    concave too but covers a quarter turn; 300 degrees leaves room for a
    hole a slot runs into."""
    groups: list = []
    for face in faces_of(get_body(model)):
        cyl = _cylinder(face)
        if cyl is None or not cyl[3]:
            continue
        origin, ax, r, _ = cyl
        # The point of the axis nearest the world origin: a key two
        # halves of the same hole share.
        s = _dot(origin, ax)
        foot = tuple(origin[i] - s * ax[i] for i in range(3))
        fbox = _member(face, "GetBox")
        corners = [(fbox[i], fbox[j], fbox[k])
                   for i in (0, 3) for j in (1, 4) for k in (2, 5)]
        proj = [_dot(c, ax) for c in corners]
        span = (min(proj), max(proj))
        area = float(_member(face, "GetArea"))
        for g in groups:
            if (abs(g["radius"] - r) <= max(1e-6, 1e-3 * r)
                    and abs(_dot(g["axis"], ax)) > 0.9999
                    and math.dist(g["foot"], foot) <= max(1e-5, 1e-3 * r)):
                g["faces"].append(face)
                g["area"] += area
                g["spans"].append(span)
                break
        else:
            groups.append({"faces": [face], "radius": r, "axis": ax,
                           "foot": foot, "area": area, "spans": [span]})

    holes = []
    for g in groups:
        spans = sorted(g["spans"])
        merged = [list(spans[0])]
        for lo, hi in spans[1:]:
            if lo <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        length = sum(hi - lo for lo, hi in merged)
        if length <= 0:
            continue
        turned = math.degrees(g["area"] / (g["radius"] * length))
        if turned < 300.0:
            continue
        mid = (merged[0][0] + merged[-1][1]) / 2.0
        centre = tuple(g["foot"][i] + mid * g["axis"][i] for i in range(3))
        holes.append({"faces": g["faces"], "diameter": 2.0 * g["radius"],
                      "axis": g["axis"], "centre": centre, "length": length})
    return holes


def select_holes(model, axis=None, side=None, diameter_mm=None, index=0,
                 what="a target"):
    """Round holes: all of them, those of one size, or those nearest one
    end of the part.  CONFIRMED

    "Fix it at the bolt holes" is how most real parts are held, and a
    hole is not an extreme of anything, so without this the only way to
    reach one was to select it by hand. With axis and side the holes
    nearest that end are taken -- every hole at the same distance, so
    the two bolt holes across the end of a bracket come together; index
    steps inward one row at a time."""
    holes = find_holes(model)
    if not holes:
        raise RuntimeError(
            f"{what}: this part has no round holes (a concave cylinder that "
            f"closes all the way round). Select the faces by hand instead.")
    if diameter_mm:
        tol = max(0.05 * diameter_mm, 0.2)
        sized = [h for h in holes
                 if abs(h["diameter"] * 1e3 - diameter_mm) <= tol]
        if not sized:
            have = sorted({round(h["diameter"] * 1e3, 1) for h in holes})
            raise RuntimeError(
                f"{what}: no {diameter_mm:g} mm hole. The holes on this part "
                f"are {', '.join(f'{d:g}' for d in have)} mm.")
        holes = sized
    if axis and side:
        idx = _AXES[axis.lower()]
        sign = 1.0 if side == "max" else -1.0
        box = _body_box(model)
        tol = max(1e-4, 0.02 * (box[idx + 3] - box[idx]))
        holes.sort(key=lambda h: -sign * h["centre"][idx])
        rows: list = []
        for h in holes:
            key = sign * h["centre"][idx]
            if rows and rows[-1][0] - key <= tol:
                rows[-1][1].append(h)
            else:
                rows.append((key, [h]))
        if index >= len(rows):
            raise RuntimeError(
                f"{what}: asked for row {index + 1} of holes from the "
                f"{side} {axis} end, but there are only {len(rows)}.")
        holes = rows[index][1]
    faces = [f for h in holes for f in h["faces"]]
    RECEIPT["selection_log"].append(
        {"target": what, "kind": "holes", "holes": len(holes),
         "faces": len(faces),
         "diameters_mm": [round(h["diameter"] * 1e3, 3) for h in holes],
         "centres_mm": [[round(c * 1e3, 2) for c in h["centre"]]
                        for h in holes]})
    sizes = sorted({round(h["diameter"] * 1e3, 2) for h in holes})
    step("Holes found", f"{len(holes)}, diameter "
                        f"{', '.join(f'{s:g}' for s in sizes)} mm, for {what}")
    return Targets(faces, units=len(holes))


class Targets(list):
    """Faces, plus how many things they are in the user's terms.

    A hole is often two half-cylinder faces, so "1 kN per bolt" over four
    bolts is 4 kN -- not 1 kN times the eight faces SolidWorks split them
    into."""

    def __init__(self, items, units=None):
        super().__init__(items)
        self.units = units if units is not None else len(self)


def _long_axis(model, what):
    """The axis the part is longest along, which is what "the end" of it
    means.  CONFIRMED

    The roles used to assume the part ran along Z, as the beams this
    project builds do. The sample control arm runs along X and the hook
    along Y, and on both "the free end" came back as one of the big flat
    faces -- the load then went on across the part instead of at its
    end, and the study solved."""
    box = _body_box(model)
    ext = sorted(((box[i + 3] - box[i], a) for a, i in _AXES.items()),
                 reverse=True)
    if ext[1][0] >= 0.9 * ext[0][0]:
        warn(f"{what}: the part is about as long along "
             f"{ext[1][1].upper()} ({ext[1][0] * 1e3:.4g} mm) as along "
             f"{ext[0][1].upper()} ({ext[0][0] * 1e3:.4g} mm), so which "
             f"end is meant is a guess -- {ext[0][1].upper()} was used.")
    return ext[0][1]


# Views as SolidWorks names them: the Front view looks at the part from
# +Z, the Top view from +Y, the Right view from +X. "front" was mapped to
# min Z before, which is the face the Front view does not show.
ROLE_DIRECTIONS = {"top": ("y", "max"), "bottom": ("y", "min"),
                   "front": ("z", "max"), "back": ("z", "min"),
                   "side": ("x", "max")}


def select_role(model, role, what="a target"):
    """A semantic role, resolved from the part's own shape.  CONFIRMED

    Ends (fixed_end, free_end, inlet, outlet) are the flat faces at the
    two ends of the LONGEST axis -- min for the fixed end and the inlet,
    max for the free end and the outlet. Directions follow the SolidWorks
    views. A role this function does not know is an error: 'wall' and
    'whole_body' used to fall through to "the top face", silently."""
    if role in ROLE_DIRECTIONS:
        axis, side = ROLE_DIRECTIONS[role]
    elif role in ("fixed_end", "free_end", "inlet", "outlet"):
        axis = _long_axis(model, what)
        side = "max" if role in ("free_end", "outlet") else "min"
    elif role == "whole_body":
        return [get_body(model)]
    elif role == "wall":
        axis = _long_axis(model, what)
        ends = []
        for s in ("min", "max"):
            try:
                ends += select_extreme(model, axis, s, what)
            except RuntimeError:
                pass
        end_ids = {(round(a, 12), tuple(round(c, 9) for c in ctr))
                   for ctr, a in (_face_centre_and_area(f) for f in ends)}
        walls = []
        for f in faces_of(get_body(model)):
            ctr, a = _face_centre_and_area(f)
            if (round(a, 12), tuple(round(c, 9) for c in ctr)) not in end_ids:
                walls.append(f)
        RECEIPT["selection_log"].append(
            {"target": what, "kind": "role", "role": role, "faces": len(walls),
             "resolved_as": f"every face but the two {axis.upper()} ends"})
        return walls
    else:
        raise RuntimeError(f"Unknown role '{role}' for {what}.")
    RECEIPT["selection_log"].append(
        {"target": what, "kind": "role", "role": role,
         "resolved_as": f"{side} {axis}"})
    return select_extreme(model, axis, side, what)


def resolve(model, selector: dict, what="a target"):
    """Turn one selector from the plan into a list of COM entities."""
    kind = selector.get("kind", "active_selection")
    etype = selector.get("entity_type", "FACE")
    if kind == "active_selection":
        return select_active(model, what=what)
    if kind == "named":
        return select_by_name(model, selector["name"], etype, what)
    if kind == "point":
        p = selector.get("point") or [0, 0, 0]
        return select_by_point(model, p[0], p[1], p[2], etype, what)
    if kind == "extreme":
        return select_extreme(model, selector.get("axis", "z"),
                              selector.get("side", "max"), what,
                              selector.get("index", 0))
    if kind == "holes":
        return select_holes(model, selector.get("axis"), selector.get("side"),
                            selector.get("diameter_mm"),
                            selector.get("index", 0), what)
    if kind == "role":
        return select_role(model, selector.get("role", ""), what)
    if kind == "body":
        return [get_body(model)]
    if kind == "all_faces":
        return faces_of(get_body(model))
    raise RuntimeError(f"Unknown selector kind '{kind}' for {what}.")


# ---------------------------------------------------------------------------
# Studies
# ---------------------------------------------------------------------------
def create_study(cw_app, name: str, domain: str):
    """Create a study and VERIFY ITS TYPE.  CONFIRMED for static

    The verification is the point. A wrong study-type constant produces
    a study that accepts every boundary condition, meshes, solves and
    answers a different question -- the exact bug that shipped in the
    previous generation of this project, where a 'static' study was
    silently solving for mode shapes. Reading the type back costs one
    call and turns a plausible wrong answer into an immediate error."""
    doc = cw_app.ActiveDoc
    if not doc:
        raise RuntimeError(
            "Simulation cannot see the active part. Click into the "
            "SolidWorks window once, then re-run.")
    code = STUDY_TYPES.get(domain)
    if code is None:
        raise RuntimeError(f"No study type code known for '{domain}'.")

    err = _err()
    study = doc.StudyManager.CreateNewStudy3(name, code, 0, err)
    if not study:
        raise RuntimeError(
            f"Could not create the {domain} study (error {err.value}). "
            f"A non-zero code here usually means the Simulation licence "
            f"does not cover this study type.")

    try:
        actual = study.AnalysisType
    except Exception:
        actual = None
    if actual is not None and int(actual) != code:
        raise RuntimeError(
            f"Study type mismatch: asked for {domain} (code {code}) and "
            f"SolidWorks created type {actual}. The constant in this "
            f"script is wrong for this SolidWorks version -- STOPPING "
            f"rather than solving the wrong problem.")
    step("Study created", f"{name} ({domain}, type {code})")
    RECEIPT["study"] = {"name": name, "domain": domain, "type_code": code,
                        "type_verified": actual is not None}
    return study


def material_database() -> str:
    """The SolidWorks material library on this machine.  CONFIRMED

    The English library under Program Files is the usual answer, but not
    the only one: another install drive, another language, a version
    folder. Looked for in the place SolidWorks itself reports, then in
    the default install roots, English first -- its material names are
    the ones the planner is told to use."""
    if Path(DEFAULT_MATERIAL_DB).exists():
        return DEFAULT_MATERIAL_DB
    roots = [Path(_STATE["sw_dir"])] if _STATE.get("sw_dir") else []
    for base in (Path(r"C:\Program Files"), Path(r"D:\Program Files")):
        roots += sorted(base.glob("SOLIDWORKS Corp*/SOLIDWORKS*"))
    for root in roots:
        found = sorted(root.glob("lang/*/sldmaterials/*.sldmat"))
        english = [p for p in found if p.parts[-3].lower() == "english"]
        for p in english + found:
            if p.name.lower() == "solidworks materials.sldmat":
                return str(p)
    raise RuntimeError(
        "The SolidWorks material library (solidworks materials.sldmat) was "
        "not found. Give its path as the material's database option.")


def material_properties(name: str, db_path: str = "") -> dict | None:
    """E, Poisson's ratio, density, yield and the thermal constants of a
    library material, read from the library file.  CONFIRMED

    Not through Simulation's ICWMaterial: GetPropertyByName failed with
    a type mismatch however its out-argument was passed, and after that
    every access to the body's material threw inside SolidWorks until it
    was restarted. The .sldmat file is the data SolidWorks reads -- UTF-16
    XML, SI values, one <material name=...> per entry -- so reading it
    gives the same numbers without touching the live session.

    The hand checks need these. They assumed E = 205 GPa, and Plain
    Carbon Steel in the library is 210: the 2% "mesh stiffness" once
    written up here was that assumption, not the mesh."""
    try:
        root = ET.fromstring(Path(db_path or material_database()).read_bytes())
    except Exception as exc:
        warn(f"Could not read the material library ({exc}); hand checks "
             f"will assume textbook steel.")
        return None
    keys = {"EX": "E", "NUXY": "nu", "DENS": "density", "SIGYLD": "yield",
            "SIGXT": "tensile", "KX": "k", "C": "c", "ALPX": "alpha"}
    for el in root.iter():
        if el.tag.split("}")[-1] != "material" or el.get("name") != name:
            continue
        props: dict = {"name": name}
        for child in el.iter():
            key = keys.get(child.tag.split("}")[-1])
            if key and child.get("value"):
                try:
                    props[key] = float(child.get("value"))
                except ValueError:
                    pass
        return props
    return None


def _part_material(model) -> str:
    """The material the part itself carries, when the plan set none."""
    try:
        got = model.GetMaterialPropertyName2("", "")
        return (got[-1] if isinstance(got, (list, tuple)) else got) or ""
    except Exception:
        return ""


def apply_material(study, name: str, db_path: str = "") -> None:
    """Assign a library material to the first solid body.  CONFIRMED

    The database path is given explicitly: relying on the localised
    default can fail with an encoding error on a non-English Windows
    install, and that failure names neither the file nor the locale."""
    err = _err()
    component = study.SolidManager.GetComponentAt(0, err)
    if not component:
        raise RuntimeError("Simulation found no solid component to apply a "
                           "material to.")
    body = component.GetSolidBodyAt(0, err)
    path = db_path or material_database()
    if not body.SetLibraryMaterial(path, name):
        raise RuntimeError(
            f"SolidWorks rejected the material '{name}'. Either the name is "
            f"not spelled as the library spells it, or the database is not "
            f"at:\n  {path}")
    RECEIPT["material"] = name
    props = material_properties(name, path)
    if props:
        RECEIPT["material_props"] = props
        step("Material applied",
             f"{name} (E {props.get('E', 0) / 1e9:.4g} GPa, yield "
             f"{props.get('yield', 0) / 1e6:.4g} MPa)")
    else:
        step("Material applied", name)


def create_mesh(study, max_element_mm: float, min_element_mm: float,
                quality: int = MESH_HIGH) -> None:
    """Mesh, with the floor enforced HERE as well as in the validator.

    Both sides check because only one of them is under this project's
    control once the script has been written out. A script edited to
    request a 0.5 mm element on a 2 m beam will hang SolidWorks for as
    long as the machine has memory, and on a shared lab PC that is
    everyone's problem, not just the author's.

    The floor applies to what was ASKED FOR. SolidWorks' own
    recommendation for this body is allowed to be finer than it, because
    a fixed 50 mm element does not mesh every part: a 100 mm tube with an
    8 mm wall fails with mesh error 8, one element being six times the
    wall thickness. The recommendation for that tube was 21.5 mm, which
    is both meshable and cheap."""
    if max_element_mm < MESH_MAX_FLOOR_MM or min_element_mm < MESH_MIN_FLOOR_MM:
        raise RuntimeError(
            f"Mesh sizes below the floor "
            f"({MESH_MAX_FLOOR_MM}/{MESH_MIN_FLOOR_MM} mm) were requested. "
            f"Refusing: a fine mesh on a shared machine takes it out of "
            f"service for everyone.")
    mesh = study.Mesh
    if not mesh:
        raise RuntimeError("Could not access the mesh engine for this study.")
    try:
        el = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_R8, 0.0)
        tol = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_R8, 0.0)
        mesh.GetDefaultElementSizeAndTolerance(0, el, tol)
        if el.value and 0 < el.value < max_element_mm:
            step("Element size reduced to what the body needs",
                 f"{el.value:.4g} mm instead of {max_element_mm:g} mm")
            max_element_mm = float(el.value)
            min_element_mm = max_element_mm / 20.0    # the 50/2.5 ratio
    except Exception as exc:
        warn(f"Could not ask SolidWorks for a default element size ({exc}); "
             f"meshing at {max_element_mm:g} mm.")
    _mesh_at(study, max_element_mm, min_element_mm, quality)


def _mesh_at(study, max_element_mm: float, min_element_mm: float,
             quality: int) -> None:
    """The meshing call itself. Callers decide the size; the one limit
    here is absolute -- no element under a millimetre, whoever asks. The
    request floor above is for what a plan may ask for; this one is for
    what a refinement computed from SolidWorks' own recommendation may
    reach on a small part."""
    if max_element_mm < MESH_MIN_FLOOR_MM:
        raise RuntimeError(
            f"A {max_element_mm:.3g} mm element is below the "
            f"{MESH_MIN_FLOOR_MM} mm this runtime will mesh at.")
    mesh = study.Mesh
    mesh.MesherType = 0
    mesh.Quality = quality
    label = "high quality" if quality == MESH_HIGH else "draft"
    step("Meshing", f"max {max_element_mm:.4g} mm, min {min_element_mm:.4g} "
                    f"mm, {label}")
    status = study.CreateMesh(0, max_element_mm, min_element_mm)
    if status != 0:
        raise RuntimeError(
            f"Meshing failed (code {status}). The usual causes are a sliver "
            f"face too small for the element size, or a body that is not "
            f"solid.")
    nodes = 0
    try:
        nodes = int(_member(mesh, "NodeCount") or 0)
    except Exception:
        pass
    RECEIPT["mesh"] = {"max_mm": max_element_mm, "min_mm": min_element_mm,
                       "quality": label, "nodes": nodes}


# ---------------------------------------------------------------------------
# Structural boundary conditions
# ---------------------------------------------------------------------------
# swsRestraintType_e: fixed 0, immovable 1, symmetric 2, roller 3,
# hinge 4. The earlier table had roller at 2 and hinge at 3, so a simple
# support went on as a symmetry restraint -- which solves, and answers a
# stiffer problem than the one that was asked.
RESTRAINT_CODES = {"fixed": 0, "immovable": 1, "symmetry": 2,
                   "roller_slider": 3, "fixed_hinge": 4}


def add_restraint(study, entities, kind="fixed", what="the fixture"):
    """Entities must be a LIST even for one face.  CONFIRMED for fixed,
    roller_slider (on a flat face), symmetry (on a flat face) and
    fixed_hinge (on round holes): each run on a part with an exact
    answer -- a block on a roller between two symmetry planes carried
    exactly F/A.

    IMMOVABLE IS FOR SHELLS AND BEAMS. On a solid it fails with error 15
    (swsRestraintErrorInvalidMesh): a solid node has no rotations to
    leave free, so immovable and fixed are the same restraint there, and
    fixed is what goes on -- said, not silently."""
    if kind not in RESTRAINT_CODES:
        raise RuntimeError(
            f"No restraint code for '{kind}'. An elastic support is a "
            f"separate call (AddElasticConnector) that this runtime does "
            f"not implement -- use fixed, immovable, roller_slider, "
            f"fixed_hinge or symmetry.")
    err = _err()
    fixture = study.LoadsAndRestraintsManager.AddRestraint(
        RESTRAINT_CODES[kind], entities, NOTHING, err)
    if not fixture and kind == "immovable" and err.value == 15:
        step("Immovable on a solid is fixed",
             "a solid has no rotations to free, so the two are the same")
        err = _err()
        fixture = study.LoadsAndRestraintsManager.AddRestraint(
            RESTRAINT_CODES["fixed"], entities, NOTHING, err)
    if not fixture:
        raise RuntimeError(
            f"Could not apply {what} (error {err.value}). Check that the "
            f"selected entity is a face of the meshed body.")
    _STATE["restrained"].extend(entities)
    step("Restraint applied", f"{kind} on {len(entities)} entity(ies)")
    return fixture


# swsForceType_e
FORCE_DIRECTIONAL = 0     # components stated against a reference plane
FORCE_NORMAL = 1          # normal to whichever face is selected
FORCE_TORQUE = 2


def add_force(study, model, entities, newtons, direction="normal",
              per_entity=False, what="the load"):
    """CONFIRMED.

    "DOWNWARD" IS NOT "NORMAL". A normal force pushes perpendicular to
    the selected face: straight down on a flat top face, along the span
    on the end face of a beam, and radially inward on the single
    cylindrical face of a round bar -- squeezing it instead of bending
    it, and solving without complaint either way. So a direction along
    an axis is applied as a DIRECTIONAL force with its components stated
    against the front reference plane, and only "normal" is left to the
    face.

    THE VALUE GOES ON EACH FACE. Measured, not read: 1000 N over the two
    flange tips of an I-beam came back as 2000 N at the supports, for a
    directional force and for a normal one alike, and 2 kN through the
    eye of the control arm -- one hole, two half-cylinder faces -- as
    4 kN. Neither AddForce, AddForce2, AddForce3 nor ICWForce has a
    "total" switch for a solid, so the total is divided by the face count
    here, and SolidWorks multiplies it back. The docstring said the
    opposite for months; the equilibrium check is what caught it.

    A positive normal force pushes INTO the face (measured the same way);
    'reverse_normal' pulls out of it."""
    err = _err()
    count = getattr(entities, "units", len(entities))
    total = newtons * count if per_entity else newtons
    each = total / max(len(entities), 1)
    axis = direction.replace("along_", "").replace("against_", "")

    if axis in ("x", "y", "z"):
        sign = -1.0 if direction.startswith("against") else 1.0
        plane = select_plane(model, PLANE_FRONT)
        force = study.LoadsAndRestraintsManager.AddForce(
            FORCE_DIRECTIONAL, entities, plane, err)
        if not force:
            raise RuntimeError(f"Could not apply {what} (error {err.value}).")
        # (bX, bY, bZ, Fx, Fy, Fz): switch on one component and give it
        # the magnitude, so the direction is stated in axes rather than
        # inherited from the shape of the face.
        components = {
            "x": (True, False, False, sign * each, 0.0, 0.0),
            "y": (False, True, False, 0.0, sign * each, 0.0),
            "z": (False, False, True, 0.0, 0.0, sign * each),
        }[axis]
        _member(force, "ForceBeginEdit")
        _set_unit(force, UNIT_SI, what)
        force.SetForceComponentValues2(*components)
        _member(force, "ForceEndEdit")
        vector = [0.0, 0.0, 0.0]
        vector["xyz".index(axis)] = sign * total
        _STATE["forces"].append({"what": what, "total_N": total,
                                 "vector_N": vector})
    else:
        if direction not in ("normal", "reverse_normal",
                             "selected_direction"):
            warn(f"{what}: '{direction}' is not a direction this runtime "
                 f"knows, so the load went on normal to the selected face.")
        force = study.LoadsAndRestraintsManager.AddForce(
            FORCE_NORMAL, entities, NOTHING, err)
        if not force:
            raise RuntimeError(f"Could not apply {what} (error {err.value}).")
        _member(force, "ForceBeginEdit")
        _set_unit(force, UNIT_SI, what)
        force.NormalForceOrTorqueValue = (
            -each if direction == "reverse_normal" else each)
        _member(force, "ForceEndEdit")
        # Normal to each face, so no single vector unless the equilibrium
        # check can take the magnitude on its own.
        _STATE["forces"].append({"what": what, "total_N": total,
                                 "vector_N": None})

    if per_entity and count > 1:
        warn(f"{what}: {newtons:g} N on each of {count} "
             f"{'holes' if hasattr(entities, 'units') else 'entities'} is "
             f"{total:g} N in total.")
    if len(entities) > 1:
        step("Force applied", f"{total:g} N in total, as {each:.6g} N on each "
                              f"of {len(entities)} faces (SolidWorks applies "
                              f"a force per face)")
    else:
        step("Force applied", f"{total:g} N on 1 entity")
    return force


def add_pressure(study, entities, pascals, what="the pressure"):
    """UNCONFIRMED call, CONFIRMED pattern.

    Unlike a force, pressure does not divide across the selection --
    every selected face gets the same pressure. That difference is why
    a force that should have been a pressure comes out low by the face
    count."""
    err = _err()
    press = study.LoadsAndRestraintsManager.AddPressure(0, entities, NOTHING, err)
    if not press:
        raise RuntimeError(f"Could not apply {what} (error {err.value}).")
    _member(press, "PressureBeginEdit")
    _set_unit(press, UNIT_SI, what)
    press.PressureValue = pascals
    _member(press, "PressureEndEdit")
    step("Pressure applied", f"{pascals:g} Pa on {len(entities)} face(s)")
    return press


def add_gravity(study, magnitude=9.80665, direction="against_y"):
    """UNCONFIRMED.

    The direction is a direction in MODEL space. A beam modelled with
    its span along Z needs gravity against Y; applying it along the span
    does nothing visible and quietly removes self-weight from the
    answer."""
    err = _err()
    try:
        grav = study.LoadsAndRestraintsManager.AddGravity(
            NOTHING, NOTHING, err)
        _member(grav, "GravityBeginEdit")
        _set_unit(grav, UNIT_SI, "gravity")
        axis = direction[-1].lower()
        sign = -1.0 if direction.startswith("against") else 1.0
        setattr(grav, {"x": "GravityValueX", "y": "GravityValueY",
                       "z": "GravityValueZ"}[axis], sign * magnitude)
        _member(grav, "GravityEndEdit")
        step("Gravity applied", f"{magnitude:g} m/s^2 {direction}")
        return grav
    except Exception as e:
        warn(f"Gravity could not be applied on this build ({e}). "
             f"Self-weight is NOT included in this result.")
        return None


# ---------------------------------------------------------------------------
# Thermal boundary conditions
# ---------------------------------------------------------------------------
#
# The thermal loads take the entity array and the error code, and
# nothing else. The type and reference-geometry arguments the structural
# calls carry are not part of them -- passing those extra arguments is
# what made every thermal script in the previous version die on its
# first load, before anything was solved.


def add_temperature(study, entities, kelvin, what="the temperature"):
    """Prescribed temperature.  CONFIRMED -- a plate held at 100 degC on
    top and 20 degC underneath read back 373.15 K and 293.15 K."""
    err = _err()
    tmp = study.LoadsAndRestraintsManager.AddTemperature(entities, err)
    if not tmp:
        raise RuntimeError(f"Could not apply {what} (error {err.value}).")
    _member(tmp, "TemperatureBeginEdit")
    _set_unit(tmp, UNIT_KELVIN, what)
    tmp.TemperatureValue = kelvin
    _member(tmp, "TemperatureEndEdit")
    _STATE["fixed_temperatures"] += 1
    step("Temperature applied", f"{kelvin:.2f} K ({kelvin - 273.15:.1f} degC)")
    return tmp


def _area(entities) -> float:
    """Total area of the faces among `entities`; a body has none."""
    total = 0.0
    for e in entities:
        try:
            total += float(_member(e, "GetArea") or 0.0)
        except Exception:
            pass
    return total


def add_convection(study, entities, h, bulk_kelvin, what="convection"):
    """Newton cooling.  CONFIRMED

    Both numbers are required by the physics, not just by the API:
    q = h*(T_surface - T_bulk). Applying convection with a default bulk
    temperature is the thermal equivalent of a load with no magnitude."""
    err = _err()
    conv = study.LoadsAndRestraintsManager.AddConvection(entities, err)
    if not conv:
        raise RuntimeError(f"Could not apply {what} (error {err.value}).")
    _member(conv, "ConvectionBeginEdit")
    _set_unit(conv, UNIT_SI, what)
    conv.ConvectionCoefficient = h
    conv.BulkAmbientTemperature = bulk_kelvin
    _member(conv, "ConvectionEndEdit")
    _STATE["convection"].append({"h": h, "bulk_K": bulk_kelvin,
                                 "area_m2": _area(entities)})
    step("Convection applied",
         f"h={h:g} W/(m^2*K), T_bulk={bulk_kelvin - 273.15:.1f} degC")
    return conv


def add_heat_power(study, entities, watts, what="heat power"):
    """Total watts, not watts per square metre.  CONFIRMED

    The value property is HPValue. And note the distribution, which is
    the opposite of a force: the API applies this value to EACH selected
    entity, so two faces at 20 W is 40 W going in."""
    err = _err()
    hp = study.LoadsAndRestraintsManager.AddHeatPower(entities, err)
    if not hp:
        raise RuntimeError(f"Could not apply {what} (error {err.value}).")
    _member(hp, "HeatPowerBeginEdit")
    _set_unit(hp, UNIT_SI, what)
    hp.HPValue = watts
    _member(hp, "HeatPowerEndEdit")
    _STATE["heat_in_W"] += watts * len(entities)
    if len(entities) > 1:
        warn(f"{what}: heat power goes on EACH of the {len(entities)} "
             f"selected entities, so {watts * len(entities):g} W is "
             f"entering the model, not {watts:g} W.")
    step("Heat power applied", f"{watts:g} W per entity")
    return hp


def add_heat_flux(study, entities, watts_per_m2, what="heat flux"):
    """CONFIRMED. The value property is HFValue. 500 W/m^2 on the top of
    the reference beam in still air rose 6.77 K against 6.83 K from the
    heat balance."""
    err = _err()
    hf = study.LoadsAndRestraintsManager.AddHeatFlux(entities, err)
    if not hf:
        raise RuntimeError(f"Could not apply {what} (error {err.value}).")
    _member(hf, "HeatFluxBeginEdit")
    _set_unit(hf, UNIT_SI, what)
    hf.HFValue = watts_per_m2
    _member(hf, "HeatFluxEndEdit")
    _STATE["heat_in_W"] += watts_per_m2 * _area(entities)
    step("Heat flux applied", f"{watts_per_m2:g} W/m^2")
    return hf


def add_radiation(study, entities, emissivity, ambient_kelvin,
                  view_factor=1.0):
    """Surface-to-ambient radiation.  CONFIRMED -- 20 W radiating from the
    reference beam at emissivity 0.9 settled at 304.0 K, as the
    Stefan-Boltzmann balance says.

    Negligible near room temperature, dominant when hot -- the
    fourth-power law has no middle ground.

    The emissivity property is spelled `Emmisivity` in the API, one 'i'
    short. The correct spelling is not a property of the object at all,
    so it is not a typo that can be quietly tolerated."""
    err = _err()
    rad = study.LoadsAndRestraintsManager.AddRadiation(
        RADIATION_SURFACE_TO_AMBIENT, entities, err)
    if not rad:
        raise RuntimeError(f"Could not apply radiation (error {err.value}).")
    _member(rad, "RadiationBeginEdit")
    _set_unit(rad, UNIT_KELVIN, "radiation")
    rad.Emmisivity = emissivity
    rad.AmbientTemperature = ambient_kelvin
    # Set, never left to the default: without it the solve failed (run
    # error 24) with 20 W going in and nowhere for it to go.
    rad.ViewFactor = view_factor
    _member(rad, "RadiationEndEdit")
    _STATE["radiation"].append({"emissivity": emissivity,
                                "ambient_K": ambient_kelvin,
                                "area_m2": _area(entities)})
    step("Radiation applied",
         f"eps={emissivity:g}, T_amb={ambient_kelvin - 273.15:.1f} degC")
    return rad


# ---------------------------------------------------------------------------
# Solve and read back
# ---------------------------------------------------------------------------
def set_frequency_modes(study, modes: int, domain: str = "frequency") -> None:
    """How many modes to extract.  CONFIRMED

    Through the study's own options object. ICWStudy has no
    SetFrequencyOptions method -- which is what this used to call, so
    the mode count silently stayed at whatever the study defaulted to."""
    modes = int(modes)
    try:
        options = (study.BucklingStudyOptions if domain == "buckling"
                   else study.FrequencyStudyOptions)
        options.NoOfFrequencies = modes
        step("Modes requested", str(modes))
    except Exception as exc:
        warn(f"Could not set the mode count ({exc}); the study default "
             f"applies.")
    RECEIPT["modes_requested"] = modes


def solve(study):
    """Run the study.  CONFIRMED

    RunAnalysis goes through _member(): pywin32 hands it back as a value
    on a build with no type information and as a method on one that has
    it, and only one of the two spellings works on each."""
    step("Solving")
    result = _member(study, "RunAnalysis")
    if result != 0:
        raise RuntimeError(
            f"The solve failed (code {result}). Common causes: no restraint "
            f"(the model is free to move), a mesh that did not complete, or "
            f"a material with no properties for this study type.")
    step("Solve finished")
    RECEIPT["solved"] = True


def _min_max(values):
    """{node_with_min, minimum, node_with_max, maximum} -> (min, max).

    Stress, displacement and thermal results all come back in that
    shape. Reading index 1 as the maximum -- which is what this did
    before -- reports the minimum instead: zero displacement at the
    restraint, or the coolest node in the part, printed as the peak."""
    return float(values[1]), float(values[3])


def _resonant_frequencies(results):
    """GetResonantFrequencies returns groups of four: mode number,
    frequency in rad/s, frequency in Hz, period in seconds.

    Confirmed against a run: a 50 x 100 x 2000 mm steel cantilever came
    back 10.50, 20.95, 65.58 Hz, against 10.5, 21.0 and 65.8 Hz from the
    closed form."""
    raw = list(results.GetResonantFrequencies(_err()) or [])
    return [{"mode": int(raw[i]), "hz": float(raw[i + 2])}
            for i in range(0, len(raw) - 3, 4)]


def _buckling_factors(results):
    """GetBucklingLoadFactors returns PAIRS: mode number, load factor.

    Confirmed against a run, and worth the check it took: element 0 is
    the mode number, so reading the array as a flat list of factors
    returns 1.0 -- a part apparently right on the point of collapse,
    which is both alarming and wrong. The real first factor for that run
    was 13.52, against 13.5 from the Euler load."""
    raw = list(results.GetBucklingLoadFactors(_err()) or [])
    return [{"mode": int(raw[i]), "factor": float(raw[i + 1])}
            for i in range(0, len(raw) - 1, 2)]


def _box_distance(p, box) -> float:
    """Distance from a point to an axis-aligned box; 0 inside it."""
    d2 = 0.0
    for i in range(3):
        lo, hi = box[i], box[i + 3]
        gap = lo - p[i] if p[i] < lo else (p[i] - hi if p[i] > hi else 0.0)
        d2 += gap * gap
    return math.sqrt(d2)


def _reactions(results, step_number: int, out: dict) -> None:
    """The total reaction at the restraints, as a vector.  CONFIRMED

    The equilibrium check: whatever went into the part has to come out
    through its supports, so this vector is minus the load the solver
    actually carried -- measured, not assumed. On the reference beam,
    5 kN down on the tip came back as 4999.999 N up at the root.

    The selection arrives in two out-arguments, passed as byref
    VARIANTs; the first holds Fx, Fy, Fz, |F|, Mx, My, Mz, |M| for the
    selection followed by the same eight for the whole model."""
    faces = list(_STATE["restrained"])
    if not faces:
        return
    sel = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
    each = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_VARIANT, None)
    results.GetReactionForcesAndMomentsWithSelections(
        step_number, NOTHING, UNIT_SI, faces, sel, each, _err())
    values = list(sel.value or [])
    if len(values) >= 3:
        out["reaction_force_N"] = [float(v) for v in values[:3]]


def _stress_away_from_supports(study, results, step_number: int,
                               out: dict) -> None:
    """The peak von Mises stress a short distance from every restraint.
    CONFIRMED

    A fixed face is a singularity: the stress at its edge grows with
    every refinement and means nothing. What a hand check and a yield
    comparison need is the peak outside that zone. Nodal von Mises comes
    from GetStress -- twelve numbers a node, the node id then eleven
    components with von Mises at index 9 -- and node positions from
    GetNodes, four a node. The zone is two elements, or half the size of
    the largest restrained face, whichever is larger; the receipt says
    which."""
    boxes, areas = [], []
    for face in _STATE["restrained"]:
        try:
            boxes.append([float(v) for v in _member(face, "GetBox")])
            areas.append(float(_member(face, "GetArea")))
        except Exception:
            continue            # a body or an edge: no box to measure from
    if not boxes:
        return
    element = float(RECEIPT.get("mesh", {}).get("max_mm") or 0.0) / 1000.0
    reach = max(2.0 * element, 0.5 * math.sqrt(max(areas)))
    raw = list(_member(study.Mesh, "GetNodes") or [])
    where = {int(raw[i]): (raw[i + 1], raw[i + 2], raw[i + 3])
             for i in range(0, len(raw) - 3, 4)}
    stress = list(results.GetStress(0, step_number, NOTHING, UNIT_PASCAL,
                                    _err()) or [])
    width = len(stress) // len(where) if where else 0
    if width < STRESS_VON_MISES + 2:
        warn("Nodal stresses came back in an unexpected layout; the stress "
             "away from the supports was not computed.")
        return
    peak = 0.0
    for i in range(0, len(stress) - width + 1, width):
        p = where.get(int(stress[i]))
        if p is None or min(_box_distance(p, b) for b in boxes) < reach:
            continue
        peak = max(peak, float(stress[i + 1 + STRESS_VON_MISES]))
    if peak > 0:
        out["von_mises_away_from_supports"] = peak
        out["support_exclusion_mm"] = reach * 1000.0


def _factor_of_safety(out: dict) -> None:
    """Yield over von Mises, at the peak and away from the supports.
    CONFIRMED against GetMinMaxFactorOfSafety: 1.797 on the reference
    beam from both."""
    ys = (RECEIPT.get("material_props") or {}).get("yield")
    if not ys:
        return
    if out.get("von_mises_max"):
        out["factor_of_safety"] = ys / out["von_mises_max"]
    if out.get("von_mises_away_from_supports"):
        out["factor_of_safety_away_from_supports"] = (
            ys / out["von_mises_away_from_supports"])


def _record_loads() -> None:
    """What was applied, in the receipt, for the checks made afterwards."""
    RECEIPT["loads"] = {
        "forces": _STATE["forces"],
        "restrained_entities": len(_STATE["restrained"]),
        "heat_in_W": _STATE["heat_in_W"],
        "convection": _STATE["convection"],
        "fixed_temperatures": _STATE["fixed_temperatures"],
        "radiation": _STATE["radiation"],
    }
    if "material_props" not in RECEIPT and _STATE.get("model") is not None:
        name = _part_material(_STATE["model"])
        props = material_properties(name) if name else None
        if props:
            RECEIPT["material"] = name
            RECEIPT["material_props"] = props


def read_results(study, domain: str) -> dict:
    """Pull numbers out of a finished study.

    Every call here is wrapped. A script that dies while READING results
    has thrown away a simulation that already succeeded, so a failure to
    read one quantity is recorded and the rest are still attempted.

    The argument lists come from the API reference and none of them is
    optional: GetMinMaxStress takes component, element, step, reference
    geometry, unit and error code, and the component for von Mises is 9.
    8 is the third principal stress -- a different number that looks
    exactly as plausible in a receipt."""
    out: dict = {}
    results = None
    try:
        results = study.Results
    except Exception as e:
        warn(f"Could not open the results object ({e}).")
        return out

    if results is None:
        warn("The study reports no results object. Did the solve complete?")
        return out

    def attempt(label, fn):
        try:
            value = fn()
            if value is not None:
                out[label] = value
        except Exception as exc:
            warn(f"Could not read {label}: {exc}")

    # WHICH SOLUTION STEP. A static study has one and the answer is on
    # it. A nonlinear study has as many as the solver took, and step 1 is
    # the FIRST LOAD INCREMENT: on the beam this was tested against, step
    # 1 reported 0.27 MPa and 0.05 mm, and step 100 -- the same load the
    # static study carried -- reported 123 MPa and 15.3 mm. Reading the
    # first step is not a rounding error, it is a different question.
    step_number = 1
    try:
        step_number = max(int(_member(results, "GetMaximumAvailableSteps")
                              or 1), 1)
    except Exception as exc:
        warn(f"Could not read how many solution steps this study has "
             f"({exc}); reading the first, which is only the whole answer "
             f"in a single-step study.")
    if step_number > 1:
        step(f"Reading results at step {step_number}")

    if domain in ("static", "nonlinear", "fatigue", "drop"):
        attempt("von_mises_max", lambda: _min_max(results.GetMinMaxStress(
            STRESS_VON_MISES, 0, step_number, NOTHING, UNIT_PASCAL,
            _err()))[1])
        attempt("displacement_max", lambda: _min_max(
            results.GetMinMaxDisplacement(DISPLACEMENT_RESULTANT,
                                          step_number, NOTHING, UNIT_METRES,
                                          _err()))[1])
    if domain == "buckling":
        # The load factor and nothing else. A buckling study has no
        # stress to read -- GetMinMaxStress comes back empty -- and its
        # displacements are a normalised mode shape, so reporting them
        # as a deflection in millimetres would be inventing a
        # measurement. Multiply the applied load by the factor to get
        # the predicted collapse load.
        try:
            factors = _buckling_factors(results)
            if factors:
                out["buckling_load_factor"] = factors[0]["factor"]
                if len(factors) > 1:
                    out["buckling_factors"] = [f["factor"] for f in factors]
        except Exception as exc:
            warn(f"Could not read the buckling load factors: {exc}")
    if domain == "thermal":
        attempt("temperature_min", lambda: _min_max(results.GetMinMaxThermal(
            THERMAL_TEMPERATURE, step_number, NOTHING, UNIT_KELVIN,
            _err()))[0])
        attempt("temperature_max", lambda: _min_max(results.GetMinMaxThermal(
            THERMAL_TEMPERATURE, step_number, NOTHING, UNIT_KELVIN,
            _err()))[1])
    if domain == "frequency":
        try:
            modes = _resonant_frequencies(results)
            if modes:
                out["first_frequency"] = modes[0]["hz"]
                out["mode_frequencies_hz"] = [m["hz"] for m in modes]
        except Exception as exc:
            warn(f"Could not read the resonant frequencies: {exc}")

    _record_loads()
    if domain in ("static", "nonlinear"):
        # Each of these is a check on the answer rather than the answer,
        # so a failure is a warning and the numbers above still stand.
        for label, fn in (("the reaction forces", _reactions),
                          ("the stress away from the supports",
                           lambda r, s, o: _stress_away_from_supports(
                               study, r, s, o))):
            try:
                fn(results, step_number, out)
            except Exception as exc:
                warn(f"Could not read {label}: {exc}")
        _factor_of_safety(out)

    if not out:
        warn("No results could be read automatically. The study solved -- "
             "open it in SolidWorks and read the plots there. The result "
             "API signatures differ between versions and this build's were "
             "not recognised.")
    RECEIPT["results"].update(out)
    return out


# Which plots to save for each study type: (label, swsPlotResultTypes_e,
# component, unit). Stress 2 / von Mises 9 / Pa; displacement 1 / URES 3
# / metres; thermal 5 / temperature 0 / kelvin. For modes and buckling
# the displacement plot IS the first mode shape.
PLOTS = {
    "static": (("stress", 2, 9, 0), ("displacement", 1, 3, 2)),
    "nonlinear": (("stress", 2, 9, 0), ("displacement", 1, 3, 2)),
    "thermal": (("temperature", 5, 0, 0),),
    "frequency": (("mode_shape", 1, 3, 2),),
    "buckling": (("mode_shape", 1, 3, 2),),
}


def save_plots(study, domain: str, folder: str = "") -> list:
    """Save the result plots as images next to the receipt.  CONFIRMED

    CreatePlot, ActivatePlot, an isometric view, then the part saved as
    a .png -- SaveAs writes the graphics area, legend and all. The
    selection is cleared first: a directional force leaves the Front
    plane selected, and it shows up highlighted in the picture."""
    model = _STATE.get("model")
    if model is None:
        return []
    out = Path(folder or Path(__file__).resolve().parent / "runs" / "plots")
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        # The Front plane a directional force was stated against stays on
        # show, label and all, across the middle of every picture.
        for plane in reference_planes(model):
            plane.Select2(False, 0)
            model.BlankRefGeom()
        model.ClearSelection2(True)
    except Exception:
        pass
    name = RECEIPT.get("study", {}).get("name", "study")
    saved = []
    results = study.Results
    for label, rtype, comp, unit in PLOTS.get(domain, ()):
        try:
            plot = results.CreatePlot(rtype, comp, unit, False, _err())
            if plot is not None:
                _member(plot, "ActivatePlot")
            else:
                # A modal study will not create a displacement plot on
                # request, but the solve already made its own: activate
                # that one by name.
                names = [str(n) for n in (_member(results, "GetPlotNames") or [])]
                prefixes = {"stress": ("Stress",),
                            "displacement": ("Displacement",),
                            "temperature": ("Thermal",),
                            "mode_shape": ("Amplitude", "Displacement")}[label]
                existing = next((n for p in prefixes for n in names
                                 if n.startswith(p)), None)
                if existing is None:
                    raise RuntimeError("no plot of this kind to show")
                results.ActivatePlot(existing)
            model.ClearSelection2(True)
            model.ShowNamedView2("*Isometric", 7)
            model.ViewZoomtofit2()
            path = out / f"{stamp}_{name}_{label}.png"
            if not model.Extension.SaveAs(str(path), 0, 1, NOTHING, _err(),
                                          _err()):
                raise RuntimeError("SaveAs refused the image")
            saved.append(str(path))
        except Exception as exc:
            warn(f"The {label} plot was not saved ({exc}); open the study in "
                 f"SolidWorks to see it.")
    if saved:
        RECEIPT["plots"] = saved
        step("Plots saved", ", ".join(Path(p).name for p in saved))
    return saved


# A finer mesh on a part that already has this many nodes is not a check
# a shared machine should be running unasked.
MAX_REFINED_NODES = 400_000

# What "the answer" is for each study type, for the convergence check.
# The peak stress is left out on purpose: at a restraint it never
# converges, so its moving says nothing about the mesh.
CONVERGENCE_KEYS = {
    "static": ("displacement_max", "von_mises_away_from_supports"),
    "nonlinear": ("displacement_max", "von_mises_away_from_supports"),
    "frequency": ("first_frequency",),
    "buckling": ("buckling_load_factor",),
    "thermal": ("temperature_max",),
}


def check_convergence(study, domain: str, tolerance_pct: float = 5.0) -> None:
    """Solve again with elements 0.7 times the size and say how far the
    answer moved.  CONFIRMED

    One mesh gives a number; two give a number and how much to trust it.
    0.7 is about three times the elements -- enough to see a trend,
    cheap enough to run by default when asked. The finer result is kept
    as THE result, since it is the better of the two. Temperatures are
    compared as a rise above the coolest bulk temperature, not in
    kelvin, where 1% is three degrees."""
    first = dict(RECEIPT["results"])
    mesh_info = RECEIPT.get("mesh", {})
    size = float(mesh_info.get("max_mm") or 0.0)
    nodes = int(mesh_info.get("nodes") or 0)
    if not size:
        warn("Convergence not checked: the first mesh size is unknown.")
        return
    if nodes * 3 > MAX_REFINED_NODES:
        warn(f"Convergence not checked: the finer mesh would have about "
             f"{nodes * 3:,} nodes, past the {MAX_REFINED_NODES:,} this "
             f"runtime allows itself.")
        return
    quality = MESH_DRAFT if mesh_info.get("quality") == "draft" else MESH_HIGH
    finer = size * 0.7
    step("Checking convergence", f"re-solving at {finer:.4g} mm")
    _mesh_at(study, finer, finer / 20.0, quality)
    solve(study)
    second = read_results(study, domain)

    base = 0.0
    if domain == "thermal":
        bulks = [c["bulk_K"] for c in _STATE["convection"]]
        base = min(bulks) if bulks else first.get("temperature_min", 0.0)
    changes = {}
    for key in CONVERGENCE_KEYS.get(domain, ()):
        a, b = first.get(key), second.get(key)
        if not isinstance(a, (int, float)) or not isinstance(b, (int, float)):
            continue
        span = abs(b - base) if domain == "thermal" else abs(b)
        if span > 0:
            changes[key] = {"coarse": a, "fine": b,
                            "change_pct": 100.0 * abs(b - a) / span}
    RECEIPT["convergence"] = {"element_mm": [size, finer],
                              "tolerance_pct": tolerance_pct,
                              "changes": changes}
    if not changes:
        warn("Convergence could not be judged: no comparable result.")
        return
    worst_key = max(changes, key=lambda k: changes[k]["change_pct"])
    worst = changes[worst_key]["change_pct"]
    RECEIPT["convergence"]["converged"] = worst <= tolerance_pct
    if worst > tolerance_pct:
        warn(f"Not converged: {worst_key} moved {worst:.1f}% when the "
             f"elements shrank from {size:.3g} to {finer:.3g} mm. The finer "
             f"result is reported; refine further before trusting it to "
             f"better than that.")
    else:
        step("Converged", f"largest change {worst:.2f}% ({worst_key})")


def write_receipt(path_hint: str = "") -> Path:
    """Write the run receipt next to this script.

    The receipt is the return leg: it carries what was set up, what was
    selected and what came out, and uploading it teaches the knowledge
    base from a real run. It is written even on failure, because a run
    that failed is the more informative one."""
    runs = Path(path_hint or Path(__file__).resolve().parent) / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = runs / f"run_{stamp}.json"
    target.write_text(json.dumps(RECEIPT, indent=2, ensure_ascii=False),
                      encoding="utf-8")
    print(f"\n  Receipt written: {target}")
    return target


def print_flow_setup(receipt: dict) -> None:
    """Print a Flow Simulation project setup as a checklist: the wizard's
    settings, then the boundary conditions and goals to add.

    The project is built from this by hand, not from COM calls: the
    registered API solves a project and reads its goals (both run here)
    but does not name the parameters a new project is made of. Every
    setting in the checklist is decided, converted and regime-checked,
    in the order the wizard asks for them; typing it in takes about a
    minute, and the script then does the rest on its next run."""
    settings = receipt.get("flow_project", receipt)
    print("\n  " + "=" * 66)
    print("  FLOW SIMULATION PROJECT -- set these in the wizard")
    print("  " + "=" * 66)
    order = [
        ("analysis_type", "Analysis type"),
        ("fluid", "Fluid"),
        ("flow_type", "Flow type"),
        ("heat_conduction", "Heat conduction in solids"),
        ("gravity", "Gravity / buoyancy"),
        ("time", "Time dependency"),
        ("ambient_pressure_Pa", "Ambient pressure [Pa]"),
        ("ambient_temperature_K", "Ambient temperature [K]"),
        ("velocity_m_s", "Free-stream velocity [m/s]"),
        ("turbulence_intensity_pct", "Turbulence intensity [%]"),
    ]
    for key, label in order:
        if key in settings and settings[key] not in ("", None, 0, 0.0):
            value = settings[key]
            extra = ""
            if key == "ambient_temperature_K":
                extra = f"  ({value - 273.15:.1f} degC)"
            if key == "velocity_m_s":
                extra = f"  ({value * 3.6:.1f} km/h)"
            print(f"    {label:<34} {value}{extra}")
    for bc in receipt.get("flow_bcs", []):
        values = ", ".join(f"{k} {v:g}" for k, v in bc.items()
                           if isinstance(v, (int, float)))
        print(f"    Boundary condition: {bc.get('type', '?')} on "
              f"{bc.get('target') or 'the face you pick'}"
              + (f" ({values}, SI)" if values else ""))
    for goal in receipt.get("goals", []):
        print(f"    Goal: {goal.get('scope', 'global')} {goal.get('quantity', '?')}"
              f" on {goal.get('target', 'the whole model')}")
    if settings.get("analysis_type") == "internal":
        print("\n    REMINDER: an internal analysis needs every opening")
        print("    capped with a lid before the fluid volume can be found.")
    print("  " + "=" * 66 + "\n")


def fail(exc: BaseException) -> None:
    """Record and report, then exit non-zero so the caller knows."""
    RECEIPT["error"] = str(exc)
    RECEIPT["traceback"] = traceback.format_exc()[-4000:]
    print(f"\n  FAILED: {exc}\n", file=sys.stderr)
    write_receipt()
    sys.exit(1)

# --- END RUNTIME PRELUDE ---
