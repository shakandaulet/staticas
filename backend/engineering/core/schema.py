"""
The typed plan that sits between the language model and SolidWorks.

THE CENTRAL DESIGN DECISION OF THIS PROJECT
-------------------------------------------
The previous generation asked Gemini for a finished Python script. That
works until it doesn't, and when it doesn't the failure is a wrong
number buried in 200 lines of plausible COM calls. Every safeguard --
the validator, the mesh floor, the quirk list -- exists to claw back
reliability the architecture gave away.

Here the model never writes the script. It fills in THIS structure: a
list of typed operations with named, unit-carrying quantities. A
deterministic emitter turns that structure into code. The model does
the part it is genuinely good at (reading "clamp the left end and hang
500 kN off the tip" and knowing that means a fixture plus a force) and
none of the part it is bad at (remembering that ForceEndEdit takes no
parentheses).

That split is also what makes this work beyond statics. Adding
thermodynamics is adding operations and an emitter for them, not
retraining a prompt to be good at a second kind of script.

LOOSE IN, STRICT OUT
--------------------
The model emits `RawOp`: every field optional, quantities as a flat
list of {name, value, unit}. Structured output degrades badly when the
schema has deeply nested one-of branches, and a refusal to parse is a
dead end for the user. So the schema the model sees is forgiving, and
`core.ops.narrow()` does the strict checking afterwards -- where a
failure is a question to ask ("a force in newtons or a pressure in
bar?") instead of a stack trace.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------
# Geometry selection
# --------------------------------------------------------------------------
SelectorKind = Literal[
    "active_selection",  # whatever the user has highlighted in SolidWorks
    "named",             # a persisted name: "Front Plane", "Boss-Extrude1"
    "point",             # a 3D click point in model space, metres
    "extreme",           # the flat faces furthest along an axis
    "role",              # a semantic role: inlet, outlet, fixed end
    "holes",             # round holes: all, one size, or those at one end
    "all_faces",
    "body",
]

EntityType = Literal["FACE", "EDGE", "VERTEX", "PLANE", "SKETCH",
                     "SOLIDBODY", "COMPONENT", "REFERENCE_AXIS"]

SemanticRole = Literal["inlet", "outlet", "wall", "fixed_end", "free_end",
                       "top", "bottom", "side", "front", "back", "whole_body"]


class Selector(BaseModel):
    """Which geometry an operation acts on.

    "APPLY IT TO THIS EDGE" IS THE HARD CASE, SO IT IS THE DEFAULT.
    A chat panel in a browser cannot see the user's cursor in
    SolidWorks. Guessing which edge they meant and silently picking one
    is the worst available answer. `active_selection` instead refers to
    whatever they have highlighted in SolidWorks at the moment the
    script runs -- the user points at the edge in the CAD window, where
    pointing at things is easy, and the assistant applies the load
    there. The emitted script reads SelectionManager and refuses to
    continue if nothing is selected, so a forgotten selection is an
    immediate, readable error rather than a load applied to the wrong
    face.

    The other kinds exist so a plan can be reproducible without a human
    in the loop: `extreme` ("the face at max Z"), `role` ("the free
    end") and `holes` ("the bolt holes") are resolved by the runtime
    prelude from the body's actual geometry, not from hardcoded
    coordinates."""

    kind: SelectorKind = "active_selection"
    entity_type: EntityType = "FACE"

    name: str | None = Field(
        default=None, description="Persisted name, for kind='named'.")
    point: list[float] | None = Field(
        default=None,
        description="[x, y, z] in METRES, for kind='point'. Three numbers.")
    axis: Literal["x", "y", "z"] | None = Field(
        default=None,
        description="Axis for kind='extreme'; for kind='holes', the axis "
                    "whose end the holes are nearest (optional).")
    side: Literal["min", "max"] | None = Field(
        default=None, description="Which end of that axis.")
    role: SemanticRole | None = Field(
        default=None, description="Semantic role, for kind='role'.")
    diameter_mm: float | None = Field(
        default=None,
        description="For kind='holes': only holes of this diameter, in mm.")
    index: int = Field(
        default=0,
        description="0 = the outermost match. For 'extreme', n steps n "
                    "levels of flat faces inward (1 at max Z on a stepped "
                    "shaft is the shoulder); for 'holes' with a side, n "
                    "steps n rows of holes in from that end.")
    said: str = Field(
        default="",
        description="The user's own words for this target, verbatim. "
                    "Echoed back in the plan card so a misread target is "
                    "visible before anything is simulated.")

    def describe(self) -> str:
        if self.kind == "active_selection":
            return f"your current SolidWorks selection ({self.entity_type.lower()})"
        if self.kind == "named":
            return f"{self.entity_type.lower()} '{self.name}'"
        if self.kind == "point":
            p = self.point or [0, 0, 0]
            return (f"{self.entity_type.lower()} at "
                    f"({p[0]:.3g}, {p[1]:.3g}, {p[2]:.3g}) m")
        if self.kind == "extreme":
            level = f" (level {self.index + 1} in)" if self.index else ""
            return f"flat faces at {self.side} {self.axis}{level}"
        if self.kind == "role":
            return f"the {str(self.role).replace('_', ' ')}"
        if self.kind == "holes":
            size = f"{self.diameter_mm:g} mm " if self.diameter_mm else ""
            where = (f" nearest {self.side} {self.axis}"
                     if self.axis and self.side else "")
            return f"the {size}holes{where}"
        if self.kind == "all_faces":
            return "every face of the body"
        return "the whole body"


# --------------------------------------------------------------------------
# Quantities
# --------------------------------------------------------------------------
class QSlot(BaseModel):
    """One named number with the unit the user actually used.

    Named rather than positional because a convection boundary condition
    carries a film coefficient AND a bulk temperature, and positional
    arguments are how those get swapped. The name is checked against the
    operation's spec in core.ops, so `film_coefficient` cannot silently
    land in the slot meant for a temperature."""

    name: str = Field(description="Slot name from the operation's spec, "
                                  "e.g. 'magnitude', 'bulk_temperature'.")
    value: float
    unit: str = Field(
        default="",
        description="Unit AS WRITTEN by the user: kN, psi, mph, degC, GPM. "
                    "Do not convert -- the server converts, and it can be "
                    "tested. Leave empty for a dimensionless number.")


OpKind = Literal[
    # geometry and setup
    "set_geometry", "apply_material", "set_mesh", "set_solver",
    # structural
    "add_fixture", "add_force", "add_pressure", "add_torque",
    "add_gravity", "add_centrifugal", "add_bearing_load", "add_remote_load",
    # thermal
    "add_temperature", "add_convection", "add_heat_flux",
    "add_heat_power", "add_radiation",
    # flow: internal (fluid mechanics) and external (aerodynamics)
    "set_flow_domain", "add_flow_bc", "add_fan", "add_porous_medium",
    "add_rotating_region", "add_goal",
    # dynamics
    "set_frequency_options", "add_initial_velocity",
    # run and report
    "solve", "extract_results",
    # escape hatch
    "custom",
]


class RawOp(BaseModel):
    """One step of the plan, as the model emits it."""
    op: OpKind
    target: Selector | None = None
    quantities: list[QSlot] = Field(default_factory=list)
    options: dict[str, str] = Field(
        default_factory=dict,
        description="Non-numeric choices: direction, fluid, turbulence "
                    "model, fixture type. Keys come from the operation spec.")
    note: str = Field(
        default="",
        description="One short line of plain English describing this step, "
                    "shown to the user in the plan card.")


class Question(BaseModel):
    """Something the plan cannot proceed without.

    Produced by narrowing, not by the model, so it is always tied to a
    real missing slot. A question with no `field` is a judgement call
    the user has to make; one with a `field` is a hole in the plan the
    answer fills directly."""
    field: str = ""
    question: str
    why: str = ""
    options: list[str] = Field(default_factory=list)
    suggested: str = ""


Domain = Literal[
    "static", "frequency", "buckling", "thermal", "nonlinear",
    "fatigue", "drop", "flow_internal", "flow_external",
]


class Plan(BaseModel):
    """The complete, typed intent for one simulation.

    This is what gets shown in the panel, what gets emitted to Python,
    and what gets stored in the session so the next message can say
    'now make it 800 N instead' without restating the whole problem."""

    domain: Domain
    study_name: str = "AI_Study"
    summary: str = Field(
        default="",
        description="One sentence: what this simulation answers.")
    ops: list[RawOp] = Field(default_factory=list)
    assumptions: list[str] = Field(
        default_factory=list,
        description="Anything filled in that the user did not say. Every "
                    "entry here is a place the answer could be wrong for a "
                    "reason the user can see and correct.")
    questions: list[Question] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    def op_notes(self) -> list[str]:
        return [o.note or o.op for o in self.ops]


class PlanResponse(BaseModel):
    """What /assistant/message returns."""
    reply: str
    plan: Plan | None = None
    questions: list[Question] = Field(default_factory=list)
    ready: bool = False
    citations: list[dict[str, Any]] = Field(default_factory=list)
    session_id: str = ""
