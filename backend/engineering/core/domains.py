"""
The physics domains this assistant covers, and what a complete plan
looks like in each of them.

WHY STUDY TYPE CODES ARE A TABLE WITH A CONFIDENCE COLUMN
---------------------------------------------------------
CreateNewStudy3 takes an integer for the study type. The previous
generation of this project passed 1 for what its own docstring called a
static study, and 1 is frequency -- so every "static" run was silently
solving for mode shapes and reporting them as stress. Nothing in the
pipeline could catch it: the call succeeded, the solve succeeded, and
the numbers looked like numbers.

That is the failure mode this table exists to prevent, in two ways.
First, the codes live in exactly one place instead of being a literal
typed into each emitter. Second, every code carries how well it is
known, and the emitted script READS THE TYPE BACK from the study it
just created and aborts if it does not match. A wrong constant then
fails in the first two seconds with a readable message, instead of
after the mesh, with a plausible answer.

FLOW SIMULATION IS A DIFFERENT PRODUCT
--------------------------------------
Fluid mechanics and aerodynamics do not go through the Simulation
add-in at all -- Flow Simulation is a separate add-in with a separate
object model and no study type code. Pretending otherwise by giving it
a number would be tidy and wrong, so `study_type` is None there and the
emitter dispatches on `engine` instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ops import UNVERIFIED, VERIFIED, WORKING

SIMULATION = "simulation"     # the SolidWorks Simulation add-in (FEA)
FLOW = "flow"                 # the Flow Simulation add-in (CFD)


@dataclass(frozen=True)
class DomainSpec:
    id: str
    label: str
    engine: str
    study_type: int | None
    type_confidence: str
    what_it_answers: str
    # Operations a plan in this domain is not complete without.
    required_ops: tuple[str, ...]
    # Operations that are usually needed and worth prompting about.
    expected_ops: tuple[str, ...] = ()
    results: tuple[str, ...] = ()
    # Words that point at this domain, in the languages this tool is
    # used in. A prior, not a decision -- see classify().
    keywords: tuple[str, ...] = ()
    notes: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)


DOMAINS: dict[str, DomainSpec] = {}


def _add(d: DomainSpec) -> None:
    DOMAINS[d.id] = d


_add(DomainSpec(
    id="static", label="Static stress", engine=SIMULATION,
    study_type=0, type_confidence=WORKING,
    what_it_answers="How much does it bend, and where does it yield, "
                    "under a load that does not change with time.",
    required_ops=("apply_material", "add_fixture", "solve"),
    expected_ops=("set_mesh", "extract_results"),
    results=("von_mises_max", "displacement_max", "factor_of_safety"),
    keywords=("stress", "static", "load", "force", "beam", "cantilever",
              "deflection", "bending", "yield", "safety factor", "bolt",
              "bracket", "statics", "напряжен", "статик", "прогиб",
              "нагрузк", "балк", "консол", "изгиб", "текучест", "прочност"),
    notes="A static study needs at least one restraint. Without one the "
          "stiffness matrix is singular and the solve fails outright -- "
          "which is the good outcome; with a nearly-singular restraint it "
          "converges to nonsense instead.",
))

_add(DomainSpec(
    id="frequency", label="Frequency / modal", engine=SIMULATION,
    study_type=1, type_confidence=WORKING,
    what_it_answers="What frequencies does it ring at, and in what shapes.",
    required_ops=("apply_material", "solve"),
    expected_ops=("add_fixture", "set_frequency_options", "extract_results"),
    results=("first_frequency", "mode_frequencies_hz"),
    keywords=("frequency", "modal", "mode", "resonance", "natural",
              "vibration", "eigen", "частот", "резонанс", "колебан",
              "собственн", "вибрац"),
    notes="Loads do not belong in a frequency study unless preload is "
          "explicitly wanted -- adding a force is a common reflex and it "
          "changes nothing in the answer.",
))

_add(DomainSpec(
    id="buckling", label="Buckling", engine=SIMULATION,
    study_type=2, type_confidence=VERIFIED,
    what_it_answers="At what multiple of this load does it collapse "
                    "sideways rather than yield.",
    required_ops=("apply_material", "add_fixture", "solve"),
    expected_ops=("add_force", "extract_results"),
    results=("buckling_load_factor",),
    keywords=("buckling", "buckle", "column", "slender", "critical load",
              "euler", "устойчивост", "потеря устойчивост", "продольн"),
    notes="Buckling reports a LOAD FACTOR, not a stress. A factor of 3 "
          "means collapse at three times the applied load. It needs a load "
          "to multiply, so a buckling study with no force answers nothing.",
))

_add(DomainSpec(
    id="thermal", label="Thermal", engine=SIMULATION,
    study_type=3, type_confidence=WORKING,
    what_it_answers="How hot does it get, and where does the heat go.",
    required_ops=("apply_material", "solve"),
    expected_ops=("add_convection", "extract_results"),
    results=("temperature_max", "temperature_min", "heat_flux_max"),
    keywords=("thermal", "temperature", "heat", "conduction", "convection",
              "radiation", "cooling", "heatsink", "heat sink", "thermo",
              "watt", "тепл", "температур", "нагрев", "охлажд", "теплоотвод",
              "конвекц", "излучен", "термодинам", "радиатор"),
    notes="A steady-state thermal study needs at least one way for heat to "
          "LEAVE. Heat in with no heat out has no steady solution, and the "
          "solver either fails or runs away to an absurd temperature.",
))

_add(DomainSpec(
    id="nonlinear", label="Non-linear static", engine=SIMULATION,
    study_type=5, type_confidence=VERIFIED,
    what_it_answers="What happens past yield, at large deflection, or with "
                    "contact that opens and closes.",
    required_ops=("apply_material", "add_fixture", "solve"),
    expected_ops=("set_solver", "extract_results"),
    results=("von_mises_max", "displacement_max", "plastic_strain_max"),
    keywords=("nonlinear", "non-linear", "plastic", "yielding", "large "
              "displacement", "hyperelastic", "contact", "нелинейн",
              "пластич", "больш перемещ", "контакт"),
))

_add(DomainSpec(
    id="fatigue", label="Fatigue", engine=SIMULATION,
    study_type=7, type_confidence=UNVERIFIED,
    what_it_answers="How many cycles before it cracks.",
    required_ops=("solve",),
    expected_ops=("extract_results",),
    results=("life_cycles", "damage_percent"),
    keywords=("fatigue", "cycles", "s-n", "endurance", "усталост", "цикл"),
    notes="Fatigue is derived from a completed static study, not set up "
          "from scratch: run the static case first and reference it. The "
          "events that do the referencing are not something this "
          "assistant can create, so it gets as far as the study and "
          "stops.",
))

_add(DomainSpec(
    id="drop", label="Drop test", engine=SIMULATION,
    study_type=6, type_confidence=UNVERIFIED,
    what_it_answers="What survives hitting the floor.",
    required_ops=("apply_material", "solve"),
    expected_ops=("add_initial_velocity", "extract_results"),
    results=("von_mises_max", "displacement_max"),
    keywords=("drop", "impact", "fall", "crash", "падени", "удар", "сброс"),
    notes="A drop test supplies its own restraint from the floor contact, "
          "so it takes no fixtures.",
))

_add(DomainSpec(
    id="flow_internal", label="Internal flow", engine=FLOW,
    study_type=None, type_confidence=UNVERIFIED,
    what_it_answers="What the fluid does inside the part: pressure drop, "
                    "flow split, mixing, cooling.",
    required_ops=("set_flow_domain", "add_flow_bc", "add_goal", "solve"),
    expected_ops=("set_mesh", "extract_results"),
    results=("pressure_drop", "mass_flow", "velocity_max",
             "average_temperature"),
    keywords=("pipe", "duct", "manifold", "internal flow", "pressure drop",
              "pump", "valve", "channel", "coolant", "hydraulic", "plumbing",
              "flow rate", "гидравлик", "труб", "канал", "насос", "клапан",
              "расход", "перепад давлен", "течение", "жидкост", "поток",
              "внутренн"),
    notes="Internal analysis requires the fluid volume to be SEALED. Every "
          "opening needs a lid, or Flow Simulation reports the geometry as "
          "not closed and refuses to build the mesh. This is the single "
          "most common reason an internal project will not start. The "
          "script prints the project as a wizard checklist -- every number "
          "decided, converted and regime-checked -- and, when the part "
          "already has a Flow project, solves it and reads the goals back "
          "(pressure drop = the spread of the pressure goals). Always plan "
          "a surface goal on the inlet and one on the outlet.",
))

_add(DomainSpec(
    id="flow_external", label="External flow / aerodynamics", engine=FLOW,
    study_type=None, type_confidence=UNVERIFIED,
    what_it_answers="Drag, lift, wake and surface pressure on a body moving "
                    "through a fluid.",
    required_ops=("set_flow_domain", "add_goal", "solve"),
    expected_ops=("set_mesh", "extract_results"),
    results=("drag_force", "lift_force", "drag_coefficient", "velocity_max"),
    keywords=("aerodynamic", "drag", "lift", "wing", "airfoil", "wind",
              "external flow", "vehicle", "car body", "spoiler", "wind "
              "tunnel", "mach", "аэродинам", "сопротивлен", "подъёмн",
              "подъемн", "крыл", "обтекан", "ветер", "внешн", "профил"),
    notes="External flow takes its free-stream condition from the project's "
          "ambient settings, not from an inlet boundary condition on a "
          "face. Adding an inlet face to an external analysis is a "
          "confusion worth catching early. The script prints the project as "
          "a wizard checklist and, when the part already has a Flow "
          "project, solves it and reads the goals back; a goal named Drag "
          "Coefficient is reported as Cd directly.",
))


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------
_WORD = re.compile(r"[a-zA-Zа-яА-ЯёЁ]+")


def keyword_scores(text: str) -> dict[str, float]:
    """A cheap lexical prior over domains.

    NOT the decision. It seeds the planner prompt with a suggestion
    (which measurably reduces the model wandering into the wrong study
    type on a terse message): the domain section, the operations the
    catalogue lists, and the domain retrieval filters on.

    Substring matching on purpose: Russian is inflected, and 'напряжение
    / напряжения / напряжений' should all hit the same stem."""
    low = (text or "").lower()
    scores: dict[str, float] = {}
    for did, spec in DOMAINS.items():
        hits = 0.0
        for kw in spec.keywords:
            if kw in low:
                # Longer keywords are more specific and worth more than a
                # bare 'heat' appearing inside 'heater housing'.
                hits += 1.0 + len(kw) / 20.0
        if hits:
            scores[did] = hits
    return scores


def classify(text: str, default: str = "static") -> tuple[str, float]:
    """Best-guess domain and a rough confidence in [0, 1]."""
    scores = keyword_scores(text)
    if not scores:
        return default, 0.0
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
    # Confidence is the margin, not the raw score: two domains both
    # scoring 4 means the message is genuinely ambiguous, and the right
    # response is to ask rather than to pick.
    margin = (best_score - runner_up) / max(best_score, 1e-9)
    return best, round(min(1.0, margin), 3)


def completeness(domain: str, present_ops: list[str]) -> list[str]:
    """What a plan in this domain is still missing.

    Separate from per-operation narrowing: narrowing checks that each
    step is well formed, this checks that the steps add up to a study
    that can run. A static plan with a load and no restraint has two
    perfectly valid operations and is still not a simulation."""
    spec = DOMAINS.get(domain)
    if not spec:
        return [f"Unknown domain '{domain}'."]

    missing = [op for op in spec.required_ops if op not in present_ops]
    out = [f"missing required step: {op}" for op in missing]

    if domain in ("static", "nonlinear", "buckling") and \
            not any(o.startswith("add_") and o not in
                    ("add_fixture", "add_initial_velocity")
                    for o in present_ops):
        out.append("no load applied -- the part would be simulated unloaded")

    if domain == "thermal":
        heat_in = {"add_heat_power", "add_heat_flux", "add_temperature"}
        heat_out = {"add_convection", "add_radiation", "add_temperature"}
        if not heat_in.intersection(present_ops):
            out.append("no heat source -- nothing drives the temperature field")
        if not heat_out.intersection(present_ops):
            out.append("no way for heat to leave -- a steady-state thermal "
                       "study with only a source has no solution")

    if domain == "flow_internal":
        types_present = present_ops.count("add_flow_bc")
        if types_present < 2:
            out.append("internal flow needs at least an inlet AND an outlet "
                       "condition")

    return out


def describe_all() -> list[dict]:
    """For GET /domains -- what the panel shows in its domain picker."""
    return [{
        "id": d.id,
        "label": d.label,
        "engine": d.engine,
        "answers": d.what_it_answers,
        "results": list(d.results),
        "confidence": d.type_confidence,
        "notes": d.notes,
    } for d in DOMAINS.values()]
