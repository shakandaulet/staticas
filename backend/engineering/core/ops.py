"""
The operation catalogue: what the assistant can actually do.

ONE TABLE, FOUR CONSUMERS
-------------------------
`OP_SPECS` below is the single source of truth for every operation the
system supports, and four different parts of the project read it:

  1. The planner prompt   -- the capability list handed to Gemini is
                             GENERATED from this table, so the model is
                             never told about an operation the emitter
                             cannot emit. That drift is the usual reason
                             an LLM tool confidently promises something
                             and then produces nothing.
  2. Narrowing            -- required slots, unit kinds and allowed
                             option values are checked here.
  3. Clarifying questions -- a missing required slot becomes a question
                             with the slot's own help text attached.
  4. The emitter          -- dispatches on op name and trusts that
                             narrowing already guaranteed the slots.

Adding a physics domain means adding rows here plus an emitter. It does
not mean touching the prompt, the UI, or the question logic.

CONFIDENCE IS PART OF THE DATA
------------------------------
Some of these operations map onto SolidWorks API calls that are
verified against working code; others are documented but untested here,
particularly the Flow Simulation ones, which are a different object
model from the Simulation add-in. That difference is recorded per
operation and travels all the way to the user, because "this script
will run" and "this script is my best reconstruction of an API I could
not test" are different claims and should not look identical in a chat
window.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import units as U
from .schema import Question, RawOp, Selector

VERIFIED = "verified"            # confirmed against working code in this repo
WORKING = "working_code"         # from the previous project's tested scripts
UNVERIFIED = "unverified"        # reconstructed from docs, not run here


@dataclass(frozen=True)
class Slot:
    """One named quantity an operation accepts."""
    kind: str                    # a units.* quantity kind
    help: str
    default: float | None = None      # in SI, applied when the user says nothing
    default_note: str = ""            # what to write in plan.assumptions


@dataclass(frozen=True)
class OptionSpec:
    help: str
    choices: tuple[str, ...] = ()     # empty tuple = free text
    default: str = ""
    required: bool = False


@dataclass(frozen=True)
class OpSpec:
    op: str
    label: str
    domains: frozenset[str]
    help: str
    required: tuple[str, ...] = ()
    slots: dict[str, Slot] = field(default_factory=dict)
    options: dict[str, OptionSpec] = field(default_factory=dict)
    needs_target: bool = False
    default_target: str = "active_selection"
    confidence: str = WORKING

    def slot_summary(self) -> str:
        """One line for the planner prompt."""
        req = ", ".join(
            f"{n}[{self.slots[n].kind}]" for n in self.required if n in self.slots)
        opt = ", ".join(
            f"{n}[{s.kind}]" for n, s in self.slots.items()
            if n not in self.required)
        parts = []
        if req:
            parts.append(f"required quantities: {req}")
        if opt:
            parts.append(f"optional: {opt}")
        for name, o in self.options.items():
            tag = "required option" if o.required else "option"
            choices = "|".join(o.choices) if o.choices else "free text"
            parts.append(f"{tag} {name}={choices}")
        if self.needs_target:
            parts.append("needs a target selector")
        return "; ".join(parts) or "no parameters"


# Shorthand so the table below stays readable.
_L, _F, _P = U.LENGTH, U.FORCE, U.PRESSURE
_T, _V, _A = U.TEMPERATURE, U.VELOCITY, U.ACCELERATION
_D = U.DIMENSIONLESS

STRUCTURAL = frozenset({"static", "nonlinear", "buckling", "fatigue", "drop"})
MODAL = frozenset({"frequency", "buckling"})
THERMAL = frozenset({"thermal"})
FLOW = frozenset({"flow_internal", "flow_external"})
ALL = STRUCTURAL | MODAL | THERMAL | FLOW | {"frequency"}


OP_SPECS: dict[str, OpSpec] = {}


def _add(spec: OpSpec) -> None:
    OP_SPECS[spec.op] = spec


# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------
_add(OpSpec(
    op="set_geometry",
    label="Geometry",
    domains=ALL,
    help=("Say where the part comes from. 'active_document' uses whatever "
          "is already open in SolidWorks and is the normal case -- the user "
          "has a model and wants it simulated. 'primitive' builds a shape "
          "from scratch: rectangular_beam / box (width, height, length; box "
          "with a wall is a hollow section), round_bar / cylinder "
          "(diameter, length), tube (outer_diameter, wall, length), i_beam "
          "(flange_width, height, web_thickness, flange_thickness, length) "
          "-- all with the section in XY and the span along +Z; plate "
          "(width along X, length along Z, thickness up Y); plate_with_hole "
          "(the plate plus hole_diameter through its middle); "
          "angle_bracket (length = base along X, height = upright along Y, "
          "width along Z, thickness, optional fillet_radius in the inside "
          "corner and hole_diameter for two bolt holes in the base)."),
    options={
        "source": OptionSpec(
            "Where the geometry comes from.",
            ("active_document", "primitive"), "active_document", required=True),
        "primitive": OptionSpec(
            "Which shape to build when source='primitive'.",
            ("rectangular_beam", "box", "round_bar", "cylinder", "tube",
             "i_beam", "plate", "plate_with_hole", "angle_bracket")),
    },
    slots={
        "length": Slot(_L, "Span, extrusion depth, or a bracket's base leg."),
        "width": Slot(_L, "Cross-section width, or a bracket's depth."),
        "height": Slot(_L, "Cross-section height, or a bracket's upright."),
        "diameter": Slot(_L, "Diameter for a round bar or cylinder."),
        "outer_diameter": Slot(_L, "Outer diameter of a tube."),
        "wall": Slot(_L, "Wall thickness of a tube or a hollow box."),
        "flange_width": Slot(_L, "I-beam flange width."),
        "web_thickness": Slot(_L, "I-beam web thickness."),
        "flange_thickness": Slot(_L, "I-beam flange thickness."),
        "thickness": Slot(_L, "Plate or bracket thickness."),
        "hole_diameter": Slot(_L, "Hole through a plate, or bolt holes in a "
                                  "bracket's base."),
        "fillet_radius": Slot(_L, "Inside-corner fillet of a bracket."),
    },
    confidence=VERIFIED,
))

# The dimensions each primitive cannot be built without. A missing one
# is a question, never a default: the emitter used to fill a missing
# section with 50 x 100 mm, and a beam of a size nobody said has an
# answer to a question nobody asked.
PRIMITIVE_DIMENSIONS: dict[str, tuple[str, ...]] = {
    "rectangular_beam": ("width", "height", "length"),
    "box": ("width", "height", "length"),
    "round_bar": ("diameter", "length"),
    "cylinder": ("diameter", "length"),
    "tube": ("outer_diameter", "wall", "length"),
    "i_beam": ("flange_width", "height", "web_thickness", "flange_thickness",
               "length"),
    "plate": ("width", "length", "thickness"),
    "plate_with_hole": ("width", "length", "thickness", "hole_diameter"),
    "angle_bracket": ("length", "height", "width", "thickness"),
}

_add(OpSpec(
    op="apply_material",
    label="Material",
    domains=ALL,
    help=("Assign a material from the SolidWorks library by name. Use the "
          "library name exactly as SolidWorks spells it, e.g. 'Plain Carbon "
          "Steel', 'AISI 304', '6061-T6'."),
    options={
        "name": OptionSpec("SolidWorks library material name.", (), "", True),
        "database": OptionSpec("Path to a .sldmat file. Leave empty for the "
                               "default English library."),
    },
    slots={
        "youngs_modulus": Slot(_P, "Override E, if the material is custom."),
        "poissons_ratio": Slot(_D, "Override Poisson's ratio."),
        "density": Slot(U.DENSITY, "Override density."),
        "yield_strength": Slot(_P, "Override yield strength."),
        "thermal_conductivity": Slot(_D, "Override k, in W/(m*K)."),
    },
    confidence=WORKING,
))

_add(OpSpec(
    op="set_mesh",
    label="Mesh",
    domains=ALL,
    help=("Mesh density. Element sizes are floored by the validator to keep "
          "the machine usable -- a request for a finer mesh is refused, not "
          "quietly honoured. Set convergence=check when the user asks how "
          "accurate the answer is, or for a result they will rely on: the "
          "study is solved again on a finer mesh and the change reported."),
    options={
        "quality": OptionSpec(
            "high = second-order elements, the default and what every "
            "verified run used; draft = first-order, faster, about 10% too "
            "stiff in bending.", ("draft", "high"), "high"),
        "convergence": OptionSpec(
            "check = solve again with 0.7x elements and report how far the "
            "answer moved (about 3x the solve time).",
            ("off", "check"), "off"),
        "strategy": OptionSpec("Mesher.",
                               ("standard", "curvature_based", "blended"),
                               "standard"),
    },
    slots={
        "max_element": Slot(_L, "Largest element edge length."),
        "min_element": Slot(_L, "Smallest element edge length."),
        "level": Slot(_D, "Flow Simulation initial mesh level, 1-7. "
                          "Higher is finer and much slower."),
        "max_cells": Slot(_D, "Flow Simulation cell budget."),
    },
    confidence=WORKING,
))

_add(OpSpec(
    op="set_solver",
    label="Solver",
    domains=ALL,
    help="Solver choice and the big non-linear switches.",
    options={
        "solver": OptionSpec("Equation solver.",
                             ("auto", "ffeplus", "direct_sparse"), "auto"),
        "large_displacement": OptionSpec(
            "Turn on when deflections are a noticeable fraction of the span; "
            "small-displacement theory is wrong there.",
            ("on", "off"), "off"),
        "time": OptionSpec("Steady state or transient.",
                           ("steady", "transient"), "steady"),
    },
    slots={
        "duration": Slot(_D, "Transient duration in seconds."),
        "time_step": Slot(_D, "Transient step in seconds."),
    },
    confidence=UNVERIFIED,
))

# --------------------------------------------------------------------------
# Structural
# --------------------------------------------------------------------------
_add(OpSpec(
    op="add_fixture",
    label="Fixture / restraint",
    domains=STRUCTURAL | MODAL,
    help=("Restrain geometry. 'fixed' holds it completely -- the built-in "
          "end of a cantilever. 'roller_slider' (flat faces) lets the face "
          "slide in its own plane, which is what a simple support usually "
          "means in a textbook. 'symmetry' (flat faces) is a cut plane of a "
          "half model. 'fixed_hinge' (round holes, cylinders) leaves "
          "rotation about the axis free -- a pin. 'immovable' is for shells "
          "and beams; on a solid it is the same as fixed and goes on as "
          "fixed."),
    options={
        # No elastic_support: it is a different API call
        # (AddElasticConnector) that the runtime does not implement, and
        # offering it produced a plan that could not be emitted.
        "type": OptionSpec(
            "Restraint type.",
            ("fixed", "immovable", "roller_slider", "fixed_hinge",
             "symmetry"),
            "fixed", required=True),
    },
    slots={
        "normal_stiffness": Slot(_D, "Elastic support stiffness, N/m^3."),
        "shear_stiffness": Slot(_D, "Elastic support shear stiffness, N/m^3."),
    },
    needs_target=True,
    confidence=VERIFIED,
))

_add(OpSpec(
    op="add_force",
    label="Force",
    domains=STRUCTURAL,
    help=("A concentrated or distributed force on a face, edge or vertex. "
          "By default the magnitude is the TOTAL spread over the selection; "
          "set distribution='per_entity' for the same force on each."),
    required=("magnitude",),
    slots={
        "magnitude": Slot(_F, "Force. Accepts N, kN, lbf, kgf."),
    },
    options={
        # Default 'against_y' -- downward -- rather than 'normal'. A
        # normal force is perpendicular to whatever face is selected,
        # which on the END face of a beam is along the span: it
        # compresses the beam instead of bending it, and solves either
        # way. Downward is what a load usually means, and when it is not,
        # the direction is in the plan card for the user to see.
        "direction": OptionSpec(
            "Which way it pushes. An axis direction is applied in model "
            "space; 'normal' pushes INTO the selected face (compression), "
            "'reverse_normal' pulls out of it -- on the end face of a beam "
            "both act along the span.",
            ("normal", "reverse_normal", "along_x", "along_y", "along_z",
             "against_x", "against_y", "against_z", "selected_direction"),
            "against_y"),
        "distribution": OptionSpec("Total over the selection, or per entity.",
                                   ("total", "per_entity"), "total"),
    },
    needs_target=True,
    confidence=VERIFIED,
))

_add(OpSpec(
    op="add_pressure",
    label="Pressure",
    domains=STRUCTURAL,
    help="Uniform pressure on a face. Accepts Pa, kPa, MPa, bar, psi, atm.",
    required=("magnitude",),
    slots={"magnitude": Slot(_P, "Pressure.")},
    options={
        "direction": OptionSpec("Normal to the face, or along an axis.",
                                ("normal", "reverse_normal",
                                 "selected_direction"), "normal"),
    },
    needs_target=True,
    confidence=WORKING,
))

_add(OpSpec(
    op="add_torque",
    label="Torque",
    domains=frozenset({"static", "nonlinear", "fatigue"}),
    help="Torque about a reference axis, applied to a cylindrical face.",
    required=("magnitude",),
    slots={"magnitude": Slot(U.TORQUE, "Torque. Accepts N*m, kN*m, lbf*ft.")},
    options={"axis": OptionSpec("Reference axis name.", (), "Axis1")},
    needs_target=True,
    confidence=UNVERIFIED,
))

_add(OpSpec(
    op="add_gravity",
    label="Gravity",
    domains=STRUCTURAL | FLOW,
    help=("Self-weight. Almost always wanted when the part is heavy relative "
          "to its load, and almost always forgotten."),
    slots={"magnitude": Slot(_A, "Acceleration. Defaults to 9.80665 m/s^2.",
                             default=9.80665,
                             default_note="Gravity taken as 9.80665 m/s^2.")},
    options={
        "direction": OptionSpec("Which way is down in model space.",
                                ("against_x", "against_y", "against_z",
                                 "along_x", "along_y", "along_z"),
                                "against_y"),
    },
    confidence=WORKING,
))

_add(OpSpec(
    op="add_centrifugal",
    label="Centrifugal load",
    domains=frozenset({"static", "nonlinear", "frequency"}),
    help="Spin the part about an axis and load it with its own inertia.",
    required=("angular_velocity",),
    slots={
        "angular_velocity": Slot(U.ROTATION, "Speed. Accepts rpm, rad/s, Hz."),
        "angular_acceleration": Slot(_D, "Angular acceleration, rad/s^2."),
    },
    options={"axis": OptionSpec("Axis of rotation.", (), "Axis1")},
    confidence=UNVERIFIED,
))

_add(OpSpec(
    op="add_bearing_load",
    label="Bearing load",
    domains=frozenset({"static", "nonlinear", "fatigue"}),
    help="Sinusoidal or parabolic load on a cylindrical bore, as from a pin.",
    required=("magnitude",),
    slots={"magnitude": Slot(_F, "Total bearing force.")},
    options={"distribution": OptionSpec("Pressure profile.",
                                        ("sinusoidal", "parabolic"),
                                        "sinusoidal")},
    needs_target=True,
    confidence=UNVERIFIED,
))

_add(OpSpec(
    op="add_remote_load",
    label="Remote load",
    domains=frozenset({"static", "nonlinear"}),
    help=("A force acting at a point in space away from the part, "
          "transferred rigidly or elastically to the selected face."),
    required=("magnitude",),
    slots={
        "magnitude": Slot(_F, "Force at the remote point."),
        "x": Slot(_L, "Remote point X."),
        "y": Slot(_L, "Remote point Y."),
        "z": Slot(_L, "Remote point Z."),
    },
    options={"connection": OptionSpec("Coupling to the face.",
                                      ("rigid", "distributed"), "distributed")},
    needs_target=True,
    confidence=UNVERIFIED,
))

# --------------------------------------------------------------------------
# Thermal
# --------------------------------------------------------------------------
_add(OpSpec(
    op="add_temperature",
    label="Prescribed temperature",
    domains=THERMAL | frozenset({"static", "nonlinear"}),
    help=("Hold geometry at a fixed temperature. In a static study this is a "
          "thermal load that produces expansion stress; in a thermal study "
          "it is a boundary condition."),
    required=("temperature",),
    slots={"temperature": Slot(_T, "Accepts K, degC, degF.")},
    needs_target=True,
    confidence=VERIFIED,    # in a thermal study; as a static load it has not run
))

_add(OpSpec(
    op="add_convection",
    label="Convection",
    domains=THERMAL,
    help=("Newton cooling on a face: q = h * (T_surface - T_bulk). Needs "
          "BOTH the film coefficient and the surrounding fluid temperature. "
          "Typical h: 5-25 W/(m^2*K) free air, 25-250 forced air, "
          "50-20000 water."),
    required=("film_coefficient", "bulk_temperature"),
    slots={
        "film_coefficient": Slot(U.CONVECTION, "h, in W/(m^2*K)."),
        "bulk_temperature": Slot(_T, "Temperature of the surrounding fluid."),
    },
    needs_target=True,
    confidence=VERIFIED,
))

_add(OpSpec(
    op="add_heat_flux",
    label="Heat flux",
    domains=THERMAL,
    help="Heat per unit area into a face, in W/m^2.",
    required=("heat_flux",),
    slots={"heat_flux": Slot(U.HEAT_FLUX, "Flux into the surface.")},
    needs_target=True,
    confidence=VERIFIED,
))

_add(OpSpec(
    op="add_heat_power",
    label="Heat power",
    domains=THERMAL,
    help=("Total wattage dissipated in a body or on a face -- how a chip, "
          "a resistor or a motor winding is usually specified."),
    required=("power",),
    slots={"power": Slot(U.POWER, "Total heat generated. Accepts W, kW, hp.")},
    needs_target=True,
    default_target="body",
    confidence=VERIFIED,
))

_add(OpSpec(
    op="add_radiation",
    label="Radiation",
    domains=THERMAL,
    help=("Surface-to-ambient radiation. Matters above roughly 200 degC, "
          "where it stops being a correction and starts being the dominant "
          "path."),
    required=("emissivity", "ambient_temperature"),
    slots={
        "emissivity": Slot(_D, "0 to 1. Polished metal ~0.05, oxidised "
                               "steel ~0.8, black paint ~0.95."),
        "ambient_temperature": Slot(_T, "Temperature radiated to."),
        "view_factor": Slot(_D, "0 to 1. Defaults to 1."),
    },
    needs_target=True,
    confidence=VERIFIED,
))

# --------------------------------------------------------------------------
# Flow: internal (fluid mechanics) and external (aerodynamics)
# --------------------------------------------------------------------------
_add(OpSpec(
    op="set_flow_domain",
    label="Flow project",
    domains=FLOW,
    help=("Creates the Flow Simulation project. 'internal' is flow THROUGH "
          "the model -- pipes, manifolds, heat exchangers, and it requires "
          "the volume to be sealed by lids. 'external' is flow AROUND it -- "
          "wings, vehicles, buildings. Getting this wrong is the single "
          "most common setup error in Flow Simulation, so it is a required "
          "option with no default."),
    required=("ambient_temperature",),
    options={
        "analysis_type": OptionSpec("Through, or around.",
                                    ("internal", "external"), "", True),
        "fluid": OptionSpec("Fluid name as Flow Simulation spells it: Air, "
                            "Water, Ammonia, Engine Oil.", (), "Air"),
        "flow_type": OptionSpec("Turbulence treatment.",
                                ("laminar", "turbulent",
                                 "laminar_and_turbulent"),
                                "laminar_and_turbulent"),
        "heat_conduction": OptionSpec(
            "Solve conduction inside the solid too (conjugate heat transfer).",
            ("on", "off"), "off"),
        "gravity": OptionSpec("Include buoyancy.", ("on", "off"), "off"),
        "time": OptionSpec("Steady or transient.",
                           ("steady", "transient"), "steady"),
    },
    slots={
        "ambient_pressure": Slot(_P, "Reference static pressure.",
                                 default=101325.0,
                                 default_note="Ambient pressure taken as "
                                              "1 atm (101325 Pa)."),
        "ambient_temperature": Slot(_T, "Reference temperature."),
        "velocity": Slot(_V, "Free-stream velocity, for an external analysis."),
        "turbulence_intensity": Slot(_D, "Percent. 0.1 for a wind tunnel, "
                                         "1-5 for most real flows."),
    },
    confidence=UNVERIFIED,
))

_add(OpSpec(
    op="add_flow_bc",
    label="Flow boundary condition",
    domains=FLOW,
    help=("An inlet, an outlet or a wall condition on a face. An internal "
          "analysis needs at least one inlet and one outlet, and the pair "
          "must not both be flow-rate conditions -- specifying mass flow at "
          "both ends leaves the pressure level undetermined and the solver "
          "will not converge."),
    options={
        "type": OptionSpec(
            "Which condition.",
            ("inlet_velocity", "inlet_mass_flow", "inlet_volume_flow",
             "inlet_total_pressure", "inlet_static_pressure",
             "outlet_static_pressure", "outlet_total_pressure",
             "outlet_mass_flow", "outlet_volume_flow",
             "environment_pressure", "real_wall", "ideal_wall"),
            "", True),
    },
    slots={
        "velocity": Slot(_V, "For an inlet_velocity condition."),
        "mass_flow": Slot(U.MASS_FLOW, "For a mass-flow condition."),
        "volume_flow": Slot(U.VOLUME_FLOW, "For a volume-flow condition."),
        "pressure": Slot(_P, "For a pressure condition."),
        "temperature": Slot(_T, "Fluid temperature at this boundary."),
        "wall_temperature": Slot(_T, "For a real_wall at fixed temperature."),
        "heat_flux": Slot(U.HEAT_FLUX, "For a real_wall with applied flux."),
        "roughness": Slot(_L, "Wall roughness height."),
    },
    needs_target=True,
    confidence=UNVERIFIED,
))

_add(OpSpec(
    op="add_fan",
    label="Fan",
    domains=FLOW,
    help="A fan curve or a fixed-flow fan on a face.",
    options={
        "fan_type": OptionSpec("Where it sits.",
                               ("external_inlet", "external_outlet",
                                "internal"), "external_inlet"),
        "curve": OptionSpec("Fan curve name from the engineering database."),
    },
    slots={
        "volume_flow": Slot(U.VOLUME_FLOW, "Fixed volume flow, if no curve."),
        "pressure": Slot(_P, "Static pressure rise, if no curve."),
    },
    needs_target=True,
    confidence=UNVERIFIED,
))

_add(OpSpec(
    op="add_porous_medium",
    label="Porous medium",
    domains=FLOW,
    help="Model a filter, a screen or a packed bed without resolving it.",
    options={"material": OptionSpec("Porous material name.", (), "")},
    slots={
        "porosity": Slot(_D, "Void fraction, 0 to 1."),
        "permeability": Slot(_D, "Darcy permeability, m^2."),
    },
    needs_target=True,
    default_target="body",
    confidence=UNVERIFIED,
))

_add(OpSpec(
    op="add_rotating_region",
    label="Rotating region",
    domains=FLOW,
    help="Spin a sub-volume for a fan, impeller or propeller.",
    required=("angular_velocity",),
    slots={"angular_velocity": Slot(U.ROTATION, "Accepts rpm, rad/s.")},
    options={"method": OptionSpec("Averaging or sliding mesh.",
                                  ("averaging", "sliding"), "averaging")},
    needs_target=True,
    default_target="body",
    confidence=UNVERIFIED,
))

_add(OpSpec(
    op="add_goal",
    label="Goal",
    domains=FLOW | THERMAL,
    help=("Flow Simulation converges on GOALS, not on a fixed residual, so a "
          "project without a goal tied to the quantity you care about can "
          "report 'converged' with that quantity still drifting. Always add "
          "one for the answer being asked for."),
    options={
        "quantity": OptionSpec(
            "What to watch.",
            ("drag_force", "lift_force", "force_x", "force_y", "force_z",
             "pressure_drop", "mass_flow", "volume_flow", "average_velocity",
             "max_velocity", "average_pressure", "static_pressure",
             "max_temperature", "average_temperature", "heat_transfer_rate",
             "torque"),
            "", True),
        "scope": OptionSpec("Where it is measured.",
                            ("global", "surface", "volume", "point",
                             "equation"), "global"),
    },
    needs_target=False,
    confidence=UNVERIFIED,
))

# --------------------------------------------------------------------------
# Dynamics
# --------------------------------------------------------------------------
_add(OpSpec(
    op="set_frequency_options",
    label="Frequency options",
    domains=MODAL,
    help="How many modes to extract, and the frequency window of interest.",
    slots={
        "modes": Slot(_D, "Number of modes.", default=5,
                      default_note="Extracting the first 5 modes."),
        "upper_bound": Slot(U.ROTATION, "Highest frequency of interest."),
    },
    confidence=VERIFIED,
))

_add(OpSpec(
    op="add_initial_velocity",
    label="Drop conditions",
    domains=frozenset({"drop"}),
    help="Impact velocity, or the height it is dropped from.",
    slots={
        "velocity": Slot(_V, "Impact speed."),
        "height": Slot(_L, "Drop height. Converted to a speed by sqrt(2gh)."),
    },
    confidence=UNVERIFIED,
))

# --------------------------------------------------------------------------
# Run and report
# --------------------------------------------------------------------------
_add(OpSpec(
    op="solve",
    label="Solve",
    domains=ALL,
    help="Run the study. Always the last-but-one step.",
    confidence=VERIFIED,
))

_add(OpSpec(
    op="extract_results",
    label="Results",
    domains=ALL,
    help=("Read numbers back out and write them into the run receipt. "
          "Without this the simulation finishes and tells nobody anything."),
    options={
        "quantities": OptionSpec(
            "Comma-separated: von_mises_max, displacement_max, "
            "factor_of_safety, temperature_max, temperature_min, "
            "drag_force, lift_force, pressure_drop, mass_flow, "
            "velocity_max, first_frequency.", (), "auto"),
    },
    confidence=WORKING,
))

_add(OpSpec(
    op="custom",
    label="Custom step",
    domains=ALL,
    help=("An escape hatch for something this catalogue does not cover. The "
          "instruction is handed to Gemini for code generation with the "
          "retrieved API reference attached, and the result goes through the "
          "same validator as everything else. Use it sparingly: a custom "
          "step is the one part of a plan that is not deterministic."),
    options={"instruction": OptionSpec("What the step must do.", (), "", True)},
    confidence=UNVERIFIED,
))


# --------------------------------------------------------------------------
# Narrowing: loose model output -> strict, SI, checked operations
# --------------------------------------------------------------------------
@dataclass
class ResolvedOp:
    """A narrowed operation. Every quantity is SI and every option legal."""
    op: str
    spec: OpSpec
    target: Selector | None
    q: dict[str, U.Quantity]
    options: dict[str, str]
    note: str
    assumptions: list[str] = field(default_factory=list)

    def num(self, name: str, default: float = 0.0) -> float:
        got = self.q.get(name)
        return got.value if got else default

    def as_dict(self) -> dict:
        return {
            "op": self.op,
            "label": self.spec.label,
            "target": self.target.model_dump() if self.target else None,
            "target_text": self.target.describe() if self.target else "",
            "quantities": {k: v.as_dict() for k, v in self.q.items()},
            "options": self.options,
            "note": self.note,
            "confidence": self.spec.confidence,
        }


def narrow(raw: RawOp, domain: str) -> tuple[ResolvedOp | None, list[Question]]:
    """Check one operation against its spec.

    Returns (resolved, questions). A non-empty question list means the
    operation is not runnable yet and the assistant should ask rather
    than guess -- which is the whole point. Guessing a missing bulk
    temperature produces a thermal result that is wrong by whatever the
    guess was off by, and nothing downstream can detect it."""
    qs: list[Question] = []
    spec = OP_SPECS.get(raw.op)
    if spec is None:
        return None, [Question(
            question=f"I do not have an operation called '{raw.op}'.",
            why="It is not in the catalogue, so nothing can emit it.")]

    if domain not in spec.domains:
        return None, [Question(
            field="domain",
            question=(f"'{spec.label}' is not available in a "
                      f"{domain.replace('_', ' ')} study."),
            why=(f"Supported there: "
                 f"{', '.join(sorted(spec.domains))}."),
            options=sorted(spec.domains))]

    # --- quantities ---
    resolved: dict[str, U.Quantity] = {}
    assumptions: list[str] = []
    seen = {s.name: s for s in raw.quantities}

    for name, slot_spec in spec.slots.items():
        given = seen.get(name)
        if given is not None:
            try:
                resolved[name] = U.convert(given.value, given.unit, slot_spec.kind)
            except U.UnitError as e:
                qs.append(Question(
                    field=f"{raw.op}.{name}",
                    question=f"{spec.label}: {e}",
                    why=slot_spec.help))
            continue
        if slot_spec.default is not None:
            resolved[name] = U.Quantity(slot_spec.default, slot_spec.kind,
                                        "default")
            if slot_spec.default_note:
                assumptions.append(slot_spec.default_note)

    for name in spec.required:
        if name in resolved:
            continue
        if any(q.field == f"{raw.op}.{name}" for q in qs):
            continue      # already reported as a bad unit
        slot_spec = spec.slots.get(name)
        qs.append(Question(
            field=f"{raw.op}.{name}",
            question=f"What {name.replace('_', ' ')} for the "
                     f"{spec.label.lower()}?",
            why=slot_spec.help if slot_spec else "",
        ))

    # A quantity the model invented that this operation has no slot for
    # is a signal it misunderstood the step -- surface it rather than
    # dropping it silently.
    for name in seen:
        if name not in spec.slots:
            qs.append(Question(
                field=f"{raw.op}.{name}",
                question=(f"'{spec.label}' has no '{name}' to set. "
                          f"Did you mean one of: "
                          f"{', '.join(spec.slots) or 'none'}?"),
                why="The value was dropped rather than applied somewhere "
                    "it does not belong."))

    # --- options ---
    options: dict[str, str] = {}
    for name, ospec in spec.options.items():
        value = (raw.options.get(name) or "").strip()
        if not value:
            if ospec.required:
                qs.append(Question(
                    field=f"{raw.op}.{name}",
                    question=f"{spec.label}: which {name.replace('_', ' ')}?",
                    why=ospec.help,
                    options=list(ospec.choices)))
                continue
            value = ospec.default
            if not value:
                # An optional option with no default is genuinely absent,
                # not empty. Validating "" against the choice list turns
                # every unused option into a question -- which is how a
                # complete plan ends up asking which primitive shape to
                # build for a part that is already open in SolidWorks.
                continue
        if ospec.choices and value not in ospec.choices:
            qs.append(Question(
                field=f"{raw.op}.{name}",
                question=(f"{spec.label}: '{value}' is not a valid "
                          f"{name.replace('_', ' ')}."),
                why=ospec.help,
                options=list(ospec.choices)))
            continue
        if value:
            options[name] = value

    # --- a primitive needs its dimensions ---
    if raw.op == "set_geometry" and options.get("source") == "primitive":
        shape = options.get("primitive", "")
        if not shape:
            qs.append(Question(
                field="set_geometry.primitive",
                question="Which shape should be built?",
                why="The part is built from scratch, so its shape decides "
                    "everything after it.",
                options=list(PRIMITIVE_DIMENSIONS)))
        for dim in PRIMITIVE_DIMENSIONS.get(shape, ()):
            if dim not in resolved and not any(
                    q.field == f"set_geometry.{dim}" for q in qs):
                qs.append(Question(
                    field=f"set_geometry.{dim}",
                    question=(f"What {dim.replace('_', ' ')} for the "
                              f"{shape.replace('_', ' ')}?"),
                    why="The part is built from these numbers; a guessed "
                        "dimension gives the answer for a different part."))

    # --- target ---
    target = raw.target
    if spec.needs_target and target is None:
        # Default to the live SolidWorks selection rather than asking.
        # "Apply it to this face" is the normal way a person talks about
        # geometry, and the honest resolution is the selection they are
        # already looking at -- not an interrogation, and not a guess.
        target = Selector(kind=spec.default_target
                          if spec.default_target != "active_selection"
                          else "active_selection",
                          entity_type="SOLIDBODY"
                          if spec.default_target == "body" else "FACE")
        assumptions.append(
            f"{spec.label}: applied to whatever is selected in SolidWorks "
            f"when the script runs.")

    if qs:
        return None, qs

    return ResolvedOp(op=raw.op, spec=spec, target=target, q=resolved,
                      options=options, note=raw.note,
                      assumptions=assumptions), []


def capability_digest(domain: str | None = None,
                      emittable: set[str] | None = None) -> str:
    """The operation catalogue, rendered for the planner prompt.

    Generated rather than written by hand so the model's idea of what
    this system can do cannot drift away from what it can actually do.

    `emittable` narrows it further, to the operations the emitter can
    currently write. The catalogue is deliberately the wider of the two
    -- it is what lets the system RECOGNISE a centrifugal load before it
    can emit one -- but there is no reason to offer the model a step it
    will then be refused for choosing. The set is passed in rather than
    imported because it lives in codegen, which imports this module."""
    lines: list[str] = []
    for spec in OP_SPECS.values():
        if domain and domain not in spec.domains:
            continue
        if emittable is not None and spec.op not in emittable:
            continue
        flag = "" if spec.confidence != UNVERIFIED else "  [API UNVERIFIED]"
        # Which studies an operation is legal in only needs saying when
        # the caller did not filter by domain -- which is the normal case
        # now, so that the model picks the study type itself instead of
        # inheriting a keyword guess.
        if domain:
            where = ""
        elif spec.domains == ALL:
            where = "  [any study]"
        else:
            where = f"  [{', '.join(sorted(spec.domains))}]"
        lines.append(f"- {spec.op}{where}{flag}: {spec.help}")
        lines.append(f"    {spec.slot_summary()}")
    return "\n".join(lines)
