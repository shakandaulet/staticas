"""
Closed-form cross-checks and pre-flight sanity.

WHY A SIMULATION ASSISTANT NEEDS HAND CALCULATIONS
--------------------------------------------------
A finite element solver will answer almost any question you ask it,
including the ones you asked by mistake. It does not know that the mesh
was too coarse to resolve the fillet, that the flow it just solved as
laminar is at Re = 2e5, or that the "cantilever" was restrained at both
ends. Every one of those produces a converged, confident, wrong number.

So two things happen around the solve:

  BEFORE -- dimensionless numbers decide whether the setup is even
  self-consistent. Reynolds decides laminar vs turbulent; Mach decides
  whether compressibility can be ignored; Biot decides whether a
  lumped-capacitance assumption holds; slenderness decides whether beam
  theory applies at all. These are cheap, they are unambiguous, and
  they catch the class of error that a validator on the generated code
  physically cannot see.

  AFTER -- the result is compared against the textbook solution for the
  idealised version of the same problem. Agreement within engineering
  tolerance is evidence the setup was sane. Disagreement by an order of
  magnitude is not proof the simulation is wrong, but it is always
  worth a sentence to the user, and it is exactly the check the
  previous generation of this project ran by hand on beam cases.

Nothing here talks to SolidWorks or to Gemini. It is arithmetic, and it
is tested.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

INFO, WARN, BLOCK = "info", "warning", "blocking"


@dataclass
class Finding:
    level: str
    message: str
    detail: str = ""
    # "check": about whether the simulation was set up right. "design":
    # about the part -- it yields. A design warning is the simulation
    # doing its job, and must not keep a correct run out of the library.
    kind: str = "check"

    def as_dict(self) -> dict:
        return {"level": self.level, "message": self.message,
                "detail": self.detail, "kind": self.kind}


# --------------------------------------------------------------------------
# Fluid properties, at 20 degC and 1 atm unless stated.
# Coarse on purpose: these decide which REGIME you are in, and a 5%
# error in viscosity never changes the answer to "laminar or turbulent".
# --------------------------------------------------------------------------
FLUIDS: dict[str, dict[str, float]] = {
    "air": {"rho": 1.204, "mu": 1.825e-5, "k": 0.0257, "cp": 1005.0,
            "a": 343.2},
    "water": {"rho": 998.2, "mu": 1.002e-3, "k": 0.598, "cp": 4182.0,
              "a": 1481.0},
    "engine oil": {"rho": 888.0, "mu": 0.8, "k": 0.145, "cp": 1880.0,
                   "a": 1500.0},
    "ammonia": {"rho": 0.73, "mu": 9.9e-6, "k": 0.0247, "cp": 2190.0,
                "a": 430.0},
}


def fluid_props(name: str) -> dict[str, float]:
    key = (name or "air").strip().lower()
    return FLUIDS.get(key, FLUIDS["air"])


# --------------------------------------------------------------------------
# Dimensionless groups
# --------------------------------------------------------------------------
def reynolds(velocity: float, length: float, fluid: str = "air") -> float:
    """Re = rho*V*L/mu. `length` is the characteristic length: pipe
    diameter for internal flow, chord or body length for external."""
    p = fluid_props(fluid)
    return p["rho"] * abs(velocity) * abs(length) / p["mu"]


def mach(velocity: float, fluid: str = "air") -> float:
    return abs(velocity) / fluid_props(fluid)["a"]


def prandtl(fluid: str = "air") -> float:
    p = fluid_props(fluid)
    return p["mu"] * p["cp"] / p["k"]


def biot(h: float, length: float, k_solid: float) -> float:
    """Bi = h*L/k. Below ~0.1 the solid is essentially isothermal and a
    full conduction solve is telling you something you already knew."""
    if k_solid <= 0:
        return float("inf")
    return h * length / k_solid


def grashof(dT: float, length: float, fluid: str = "air",
            beta: float | None = None) -> float:
    """Free-convection driving group. beta ~ 1/T for an ideal gas."""
    p = fluid_props(fluid)
    beta = beta if beta is not None else 1.0 / 293.15
    nu = p["mu"] / p["rho"]
    return 9.80665 * beta * abs(dT) * length ** 3 / (nu ** 2)


# --------------------------------------------------------------------------
# Structural closed forms
# --------------------------------------------------------------------------
def second_moment_rect(width: float, height: float) -> float:
    return width * height ** 3 / 12.0


def second_moment_circle(diameter: float) -> float:
    return math.pi * diameter ** 4 / 64.0


def second_moment_tube(outer_d: float, wall: float) -> float:
    inner = max(outer_d - 2.0 * wall, 0.0)
    return math.pi * (outer_d ** 4 - inner ** 4) / 64.0


def second_moment_box(width: float, height: float, wall: float) -> float:
    """A rectangular hollow section; wall 0 is the solid rectangle."""
    if wall <= 0:
        return second_moment_rect(width, height)
    wi, hi = max(width - 2 * wall, 0.0), max(height - 2 * wall, 0.0)
    return (width * height ** 3 - wi * hi ** 3) / 12.0


def second_moment_i(flange_width: float, height: float, web: float,
                    flange: float) -> float:
    """An I-section about its strong axis."""
    return (flange_width * height ** 3
            - (flange_width - web) * (height - 2 * flange) ** 3) / 12.0


def hole_kt_net(d: float, width: float) -> float:
    """Heywood's fit for a central hole in a strip in tension, on the NET
    section: Kt = 2 + (1 - d/W)^3. Within a couple of percent of
    Peterson's chart for d/W up to about 0.65."""
    return 2.0 + (1.0 - d / width) ** 3


# Primitives beam theory describes: a constant section along a long span.
BEAM_SHAPES = {"rectangular_beam", "box", "round_bar", "cylinder", "tube",
               "i_beam"}


def geometry_shape(geom) -> str:
    """The primitive a set_geometry step builds, or '' for an open part.
    A primitive plan that names no shape builds the rectangular beam --
    the emitter's default -- so that is what it is checked as."""
    if geom is None or geom.options.get("source") != "primitive":
        return ""
    return geom.options.get("primitive") or "rectangular_beam"


def section_properties(geom) -> dict | None:
    """The section of a beam-like primitive, or None.

    I_x resists a load along Y (the section's height is along Y), I_y a
    load along X; w and h are the section's extent along X and Y; the
    span runs along Z. Keyed on the SHAPE, not on which dimensions happen
    to be present: a bracket has a width and a height too, and treating
    it as a rectangular cantilever produced a comparison that meant
    nothing."""
    shape = geometry_shape(geom)
    if shape not in BEAM_SHAPES:
        return None
    g = geom.num
    if shape in ("rectangular_beam", "box"):
        w, h, t = g("width"), g("height"), g("wall") if shape == "box" else 0.0
        if w <= 0 or h <= 0:
            return None
        wi, hi = (max(w - 2 * t, 0.0), max(h - 2 * t, 0.0)) if t > 0 else (0.0, 0.0)
        return {"I_x": (w * h ** 3 - wi * hi ** 3) / 12.0,
                "I_y": (h * w ** 3 - hi * wi ** 3) / 12.0,
                "area": w * h - wi * hi, "w": w, "h": h}
    if shape in ("round_bar", "cylinder", "tube"):
        d = g("diameter") if shape != "tube" else g("outer_diameter")
        if d <= 0:
            return None
        inner = max(d - 2 * g("wall"), 0.0) if shape == "tube" else 0.0
        I = math.pi * (d ** 4 - inner ** 4) / 64.0
        return {"I_x": I, "I_y": I, "area": math.pi * (d ** 2 - inner ** 2) / 4.0,
                "w": d, "h": d}
    b, h = g("flange_width"), g("height")
    tw, tf = g("web_thickness"), g("flange_thickness")
    if min(b, h, tw, tf) <= 0:
        return None
    return {"I_x": second_moment_i(b, h, tw, tf),
            "I_y": (2 * tf * b ** 3 + (h - 2 * tf) * tw ** 3) / 12.0,
            "area": 2 * b * tf + (h - 2 * tf) * tw, "w": b, "h": h}


def cantilever_tip_deflection(force: float, length: float,
                              E: float, I: float) -> float:
    """delta = F*L^3 / (3*E*I) for an end load on a cantilever."""
    if E <= 0 or I <= 0:
        return float("nan")
    return force * length ** 3 / (3.0 * E * I)


def cantilever_udl_deflection(total_force: float, length: float,
                              E: float, I: float) -> float:
    """delta = w*L^4/(8*E*I), with w = total/L.

    Worth having separately: a load 'distributed along the whole span'
    deflects the tip 3/8 as much as the same total hung off the end, and
    treating one as the other is a 2.7x error in the direction that
    looks fine."""
    if E <= 0 or I <= 0 or length <= 0:
        return float("nan")
    w = total_force / length
    return w * length ** 4 / (8.0 * E * I)


def bending_stress(moment: float, c: float, I: float) -> float:
    """sigma = M*c/I."""
    if I <= 0:
        return float("nan")
    return moment * c / I


def euler_critical_load(E: float, I: float, length: float,
                        end_condition: str = "fixed_free") -> float:
    """P_cr = pi^2*E*I/(K*L)^2."""
    K = {"pinned_pinned": 1.0, "fixed_free": 2.0,
         "fixed_pinned": 0.699, "fixed_fixed": 0.5}.get(end_condition, 2.0)
    if length <= 0:
        return float("nan")
    return math.pi ** 2 * E * I / (K * length) ** 2


def beam_first_frequency(E: float, I: float, rho: float, area: float,
                         length: float, mode: int = 1) -> float:
    """Natural frequency of a uniform cantilever, Hz.

    f = (beta_n^2 / 2*pi) * sqrt(E*I / (rho*A*L^4))"""
    betas = {1: 1.875104, 2: 4.694091, 3: 7.854757}
    b = betas.get(mode, 1.875104)
    if length <= 0 or rho <= 0 or area <= 0:
        return float("nan")
    return (b ** 2 / (2 * math.pi)) * math.sqrt(E * I / (rho * area * length ** 4))


# --------------------------------------------------------------------------
# Fluid closed forms
# --------------------------------------------------------------------------
def darcy_friction_factor(Re: float, roughness: float = 0.0,
                          diameter: float = 1.0) -> float:
    """Laminar exactly; Swamee-Jain in the turbulent range."""
    if Re <= 0:
        return float("nan")
    if Re < 2300:
        return 64.0 / Re
    rel = roughness / diameter if diameter > 0 else 0.0
    inner = rel / 3.7 + 5.74 / Re ** 0.9
    return 0.25 / (math.log10(inner) ** 2)


def pipe_pressure_drop(velocity: float, diameter: float, length: float,
                       fluid: str = "air", roughness: float = 0.0) -> float:
    """Darcy-Weisbach: dp = f * (L/D) * rho*V^2/2."""
    p = fluid_props(fluid)
    Re = reynolds(velocity, diameter, fluid)
    f = darcy_friction_factor(Re, roughness, diameter)
    return f * (length / diameter) * p["rho"] * velocity ** 2 / 2.0


def drag_force(cd: float, velocity: float, area: float,
               fluid: str = "air") -> float:
    """F = 0.5*rho*V^2*A*Cd."""
    return 0.5 * fluid_props(fluid)["rho"] * velocity ** 2 * area * cd


def drag_coefficient(force: float, velocity: float, area: float,
                     fluid: str = "air") -> float:
    q = 0.5 * fluid_props(fluid)["rho"] * velocity ** 2 * area
    return force / q if q > 0 else float("nan")


# --------------------------------------------------------------------------
# Pre-flight checks
# --------------------------------------------------------------------------
@dataclass
class PreflightReport:
    findings: list[Finding] = field(default_factory=list)
    numbers: dict[str, float] = field(default_factory=dict)
    # Set by crosscheck() when a result was actually held against a
    # closed form. "Nothing to compare against" and "agrees with the
    # closed form" are different answers, and only the second earns a run
    # its place in the retrievable library -- without this flag the note
    # about assuming E = 205 GPa was enough to pass for agreement.
    compared: bool = False

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.level == BLOCK]

    def as_dict(self) -> dict:
        return {"findings": [f.as_dict() for f in self.findings],
                "numbers": {k: round(v, 6) for k, v in self.numbers.items()},
                "compared": self.compared}


def preflight(domain: str, ops: list) -> PreflightReport:
    """Regime checks on a narrowed plan, before a line of code is emitted.

    `ops` is a list of core.ops.ResolvedOp. Reads only SI quantities, so
    it cannot be fooled by a unit the user wrote oddly."""
    rep = PreflightReport()
    by_op: dict[str, list] = {}
    for o in ops:
        by_op.setdefault(o.op, []).append(o)

    # ---------------- flow ----------------
    if domain in ("flow_internal", "flow_external"):
        dom_op = (by_op.get("set_flow_domain") or [None])[0]
        fluid = (dom_op.options.get("fluid", "air") if dom_op else "air").lower()
        flow_type = dom_op.options.get("flow_type", "") if dom_op else ""

        velocity = 0.0
        if dom_op:
            velocity = dom_op.num("velocity", 0.0)
        for bc in by_op.get("add_flow_bc", []):
            velocity = max(velocity, bc.num("velocity", 0.0))

        # The characteristic length is a different dimension in the two
        # domains: internal flow develops across the bore, external flow
        # along the body. Taking the largest dimension for both turns
        # water at 0.1 m/s in a 10 mm pipe 2 m long into Re = 200 000 and
        # blocks a laminar setting that was right.
        length = 0.0
        for g in by_op.get("set_geometry", []):
            if domain == "flow_internal":
                bore = g.num("diameter", 0.0)
                if not bore and g.num("outer_diameter", 0.0):
                    bore = max(g.num("outer_diameter")
                               - 2.0 * g.num("wall", 0.0), 0.0)
                length = max(length, bore)
            else:
                for name in ("length", "diameter", "height",
                             "outer_diameter"):
                    length = max(length, g.num(name, 0.0))

        if velocity > 0 and length > 0:
            Re = reynolds(velocity, length, fluid)
            Ma = mach(velocity, fluid)
            rep.numbers["reynolds"] = Re
            rep.numbers["mach"] = Ma

            if Re > 4000 and flow_type == "laminar":
                rep.findings.append(Finding(
                    BLOCK,
                    f"Re = {Re:,.0f} but the project is set to laminar.",
                    "Above roughly Re = 4000 the flow is turbulent. A "
                    "laminar solve here converges and under-predicts both "
                    "pressure drop and heat transfer, often by several "
                    "times. Use laminar_and_turbulent."))
            elif Re < 2300 and flow_type == "turbulent":
                rep.findings.append(Finding(
                    WARN,
                    f"Re = {Re:,.0f} is laminar, but turbulence is forced on.",
                    "A turbulence model in a laminar regime adds artificial "
                    "viscosity and over-predicts losses."))
            elif 2300 <= Re <= 4000:
                rep.findings.append(Finding(
                    INFO,
                    f"Re = {Re:,.0f} is in the transition band.",
                    "Neither laminar nor fully turbulent correlations apply "
                    "cleanly here; treat the answer as indicative."))

            if Ma > 0.3:
                rep.findings.append(Finding(
                    WARN if Ma < 0.8 else BLOCK,
                    f"Mach {Ma:.2f}: this flow is compressible.",
                    "Above M = 0.3 density variation stops being negligible. "
                    "The project must be set up as compressible, and above "
                    "M = 0.8 transonic effects need a solver setting this "
                    "assistant does not configure."))

        if domain == "flow_external":
            for bc in by_op.get("add_flow_bc", []):
                if bc.options.get("type", "").startswith("inlet"):
                    rep.findings.append(Finding(
                        WARN,
                        "An inlet boundary condition was added to an "
                        "EXTERNAL analysis.",
                        "External flow takes its free stream from the "
                        "project's ambient velocity, not from a face. This "
                        "condition will either be ignored or will fight the "
                        "free stream."))
        if not by_op.get("add_goal"):
            rep.findings.append(Finding(
                WARN, "No goal defined.",
                "Flow Simulation judges convergence against goals. With "
                "none, it stops on a travel count and may report a drag "
                "force that is still drifting."))

    # ---------------- thermal ----------------
    if domain == "thermal":
        h_values = [c.num("film_coefficient") for c in by_op.get("add_convection", [])]
        if h_values:
            h = max(h_values)
            rep.numbers["film_coefficient"] = h
            if h > 25 and h < 250:
                regime = "forced air"
            elif h <= 25:
                regime = "free/natural air"
            elif h < 3000:
                regime = "forced liquid"
            else:
                regime = "boiling or condensing"
            rep.findings.append(Finding(
                INFO, f"h = {h:g} W/(m^2*K) is in the {regime} range.",
                "Free air 5-25, forced air 25-250, forced water 300-12000. "
                "A value far outside these usually means a unit slipped."))
            if h > 1e5:
                rep.findings.append(Finding(
                    BLOCK, f"h = {h:g} W/(m^2*K) is not physical.",
                    "Even condensing steam is under 30000. Check the unit."))

        for t in by_op.get("add_temperature", []):
            T = t.num("temperature")
            if T > 5000:
                rep.findings.append(Finding(
                    BLOCK, f"{T:.0f} K is hotter than most metals exist at.",
                    "This is the signature of a Celsius value read as "
                    "kelvin, or of a stray factor of ten."))

    # ---------------- structural ----------------
    if domain in ("static", "nonlinear", "buckling", "fatigue"):
        geom = (by_op.get("set_geometry") or [None])[0]
        section = section_properties(geom) if geom else None
        if section:
            L = geom.num("length")
            h = max(section["w"], section["h"])
            if L > 0 and h > 0:
                ratio = L / h
                rep.numbers["slenderness"] = ratio
                if ratio < 5:
                    rep.findings.append(Finding(
                        WARN,
                        f"Span/depth = {ratio:.1f}: this is a stubby block, "
                        "not a beam.",
                        "Beam theory over-predicts stiffness below about 10 "
                        "because it ignores shear deformation. The FE result "
                        "is the trustworthy one here; do not be alarmed when "
                        "it disagrees with the hand calculation."))
                elif ratio > 100:
                    rep.findings.append(Finding(
                        WARN,
                        f"Span/depth = {ratio:.0f}: very slender.",
                        "Check buckling and large-displacement effects. A "
                        "linear static study assumes deflections stay small "
                        "and will not warn you when they do not."))

        for f in by_op.get("add_force", []):
            if f.num("magnitude") > 1e7:
                rep.findings.append(Finding(
                    WARN, f"{f.num('magnitude'):.3g} N is about "
                          f"{f.num('magnitude') / 9806.65:.0f} tonnes.",
                    "Worth confirming the unit was not kN read as N."))

        if not by_op.get("add_fixture") and domain != "drop":
            rep.findings.append(Finding(
                BLOCK, "No restraint in a structural study.",
                "With nothing held, the part is free to translate and the "
                "stiffness matrix is singular. The solve will fail."))

    return rep


# --------------------------------------------------------------------------
# Post-run cross-check
# --------------------------------------------------------------------------
def _check_plate_with_hole(rep: PreflightReport, geom, forces, results: dict,
                           E: float) -> None:
    """A strip with a central hole, pulled along its length.

    The one stress peak in this project's parts that means something: at
    a hole edge the stress converges with refinement, unlike at a fixed
    face, so the solver's maximum can be held against Kt * net stress.
    Only for a load along the length -- the plate's Z -- or normal to its
    end face, which is the same thing there."""
    W, t = geom.num("width"), geom.num("thickness")
    d, L = geom.num("hole_diameter"), geom.num("length")
    if min(W, t, d, L) <= 0 or d >= W:
        return
    along = [f for f in forces
             if f.options.get("direction", "normal").endswith("z")
             or f.options.get("direction", "normal") in ("normal",
                                                          "reverse_normal")]
    if len(along) != len(forces):
        rep.findings.append(Finding(
            INFO, "No hand check: the load is not along the plate.",
            "The Kt comparison is for a strip pulled along its length."))
        return
    F = sum(f.num("magnitude") for f in forces)
    net = F / ((W - d) * t)
    kt = hole_kt_net(d, W)
    peak = kt * net
    stretch = F * L / (E * W * t)
    rep.numbers.update({"net_section_stress_Pa": net,
                        "stress_concentration_kt": kt,
                        "hole_peak_stress_Pa": peak,
                        "nominal_elongation_m": stretch})

    got_s = results.get("von_mises_max")
    if isinstance(got_s, (int, float)) and got_s > 0:
        rep.compared = True
        ratio = got_s / peak
        if 0.8 <= ratio <= 1.25:
            rep.findings.append(Finding(
                INFO,
                f"Peak stress agrees with Kt = {kt:.2f} at the hole "
                f"({got_s / 1e6:.4g} MPa vs {peak / 1e6:.4g} MPa).",
                "A coarse mesh reads a hole peak a little low; within this "
                "band the load, the restraint and the hole are where they "
                "should be."))
        else:
            rep.findings.append(Finding(
                WARN,
                f"Peak stress is {ratio:.2g}x the Kt value at the hole "
                f"({got_s / 1e6:.4g} MPa vs {peak / 1e6:.4g} MPa).",
                "Low: the mesh is too coarse round the hole. High: the peak "
                "is somewhere else -- at the restrained face, which is a "
                "singularity -- or the load went on the wrong face."))

    got_d = results.get("displacement_max")
    if isinstance(got_d, (int, float)) and got_d > 0:
        rep.compared = True
        ratio = got_d / stretch
        # The hole removes a quarter of the section over a short length,
        # so the plate stretches a few percent more than a plain one.
        if 0.9 <= ratio <= 1.3:
            rep.findings.append(Finding(
                INFO,
                f"Elongation agrees with F*L/(E*A) "
                f"({got_d * 1e6:.4g} um vs {stretch * 1e6:.4g} um).",
                "The hole makes it a few percent softer than a plain strip."))
        else:
            rep.findings.append(Finding(
                WARN,
                f"Elongation is {ratio:.2g}x F*L/(E*A) "
                f"({got_d * 1e6:.4g} um vs {stretch * 1e6:.4g} um).",
                "Check that the restraint is on one end and the load on the "
                "other, and that the load pulls along the length."))


def _agreement(rep: PreflightReport, what: str, got: float, ref: float,
               lo: float, hi: float, unit: str, scale: float,
               why_bad: str, why_good: str = "") -> None:
    """One comparison, one finding: INFO inside [lo, hi], WARN outside."""
    rep.compared = True
    ratio = got / ref if ref else float("inf")
    shown = f"{got / scale:.4g} {unit} vs {ref / scale:.4g} {unit}"
    if lo <= ratio <= hi:
        rep.findings.append(Finding(INFO, f"{what} agrees ({shown}).",
                                    why_good))
    else:
        rep.findings.append(Finding(
            WARN, f"{what} is {ratio:.3g}x the hand calculation ({shown}).",
            why_bad))


def _material(rep: PreflightReport, by_op: dict, props: dict) -> tuple:
    """(E, density, yield) from what the run read out of the library;
    the plan's override when it gave one; textbook steel otherwise, said
    out loud."""
    mat = (by_op.get("apply_material") or [None])[0]
    E = props.get("E") or (mat.num("youngs_modulus") if mat else 0.0)
    rho = props.get("density") or (mat.num("density") if mat else 0.0)
    if not E:
        E = 2.1e11
        rep.findings.append(Finding(
            INFO, "Hand check assumes steel: E = 210 GPa, 7800 kg/m^3.",
            "The run did not report its material's properties; the "
            "simulation used whatever the SolidWorks library holds."))
    return E, rho or 7800.0, props.get("yield", 0.0)


def _axis_of(force) -> str:
    """x, y or z for a force stated along an axis; '' otherwise."""
    d = force.options.get("direction", "normal")
    return d[-1] if d.startswith(("along_", "against_")) else ""


def _at_an_end(op, side: str) -> bool:
    """Is this step's target one end of a part that runs along Z?"""
    t = op.target
    if t is None:
        return False
    if t.kind == "role":
        return t.role == ("fixed_end" if side == "min" else "free_end")
    return t.kind == "extreme" and t.axis == "z" and t.side == side \
        and not t.index


def _check_equilibrium(rep: PreflightReport, by_op: dict, results: dict,
                       receipt: dict) -> None:
    """The supports must carry what was applied.

    The reaction is measured by the solver; the applied load is what the
    run wrote into SolidWorks. They disagree when SolidWorks read the
    load differently from how it was meant -- spread per face instead of
    in total, a unit, a sign -- which is the class of error that solves
    cleanly and is otherwise invisible."""
    R = results.get("reaction_force_N")
    written = (receipt.get("loads") or {}).get("forces") or []
    if not (isinstance(R, list) and len(R) == 3 and written):
        return
    if by_op.get("add_pressure") or by_op.get("add_gravity"):
        return          # their share of the reaction is not tracked here
    size = math.sqrt(sum(r * r for r in R))
    vectors = [f.get("vector_N") for f in written]
    if all(v is not None for v in vectors):
        applied = [sum(v[i] for v in vectors) for i in range(3)]
        load = math.sqrt(sum(a * a for a in applied))
        miss = math.sqrt(sum((R[i] + applied[i]) ** 2 for i in range(3)))
    elif len(written) == 1:
        load = float(written[0].get("total_N") or 0.0)
        miss = abs(size - load)
    else:
        return
    if load <= 0:
        return
    rep.numbers.update({"reaction_N": size, "applied_N": load})
    if miss <= 0.02 * load:
        rep.findings.append(Finding(
            INFO, f"The supports carry the whole load ({size:.5g} N "
                  f"against {load:.5g} N applied).", ""))
    else:
        rep.findings.append(Finding(
            WARN, f"The reactions do not balance the load: {size:.5g} N at "
                  f"the supports against {load:.5g} N applied.",
            "SolidWorks applied something other than what was meant: a "
            "force spread per face instead of in total, a unit, or a "
            "direction. Check the load in the study tree before reading "
            "anything else."))


def _check_safety(rep: PreflightReport, results: dict, yield_pa: float) -> None:
    fos = results.get("factor_of_safety")
    away = results.get("factor_of_safety_away_from_supports")
    if not (yield_pa and isinstance(fos, (int, float))):
        return
    rep.numbers["factor_of_safety"] = fos
    judged = away if isinstance(away, (int, float)) else fos
    if isinstance(away, (int, float)):
        rep.numbers["factor_of_safety_away_from_supports"] = away
    text = (f"Factor of safety {judged:.3g} against a yield strength of "
            f"{yield_pa / 1e6:.4g} MPa"
            + (f" (away from the supports; {fos:.3g} at the peak on a "
               f"restraint, which is a singularity)" if judged is away else ""))
    if judged < 1.0:
        rep.findings.append(Finding(
            WARN, text + ": it yields.",
            "A linear study past yield is outside its own assumption: the "
            "real part redistributes load and deforms permanently. Use a "
            "non-linear study with a plastic material, or a stronger part.",
            kind="design"))
    else:
        rep.findings.append(Finding(INFO, text + ".", "", kind="design"))


def _check_beam(rep: PreflightReport, geom, sec: dict, forces: list,
                results: dict, E: float) -> None:
    """A cantilever along Z: bending about the axis the load acts across,
    or stretching when the load is along the span."""
    L = geom.num("length")
    axes = {_axis_of(f) for f in forces}
    if L <= 0 or len(axes) != 1 or "" in axes:
        rep.findings.append(Finding(
            INFO, "No beam-theory check: the loads are not all along one "
                  "axis.", ""))
        return
    axis = axes.pop()
    F = sum(f.num("magnitude") for f in forces)
    got_d = results.get("displacement_max")

    if axis == "z":
        stretch = F * L / (E * sec["area"])
        rep.numbers["axial_elongation_m"] = stretch
        if isinstance(got_d, (int, float)) and got_d > 0:
            _agreement(rep, "Axial stretch against F*L/(E*A)", got_d, stretch,
                       0.9, 1.15, "um", 1e-6,
                       "Check the restraint is on one end and the load on the "
                       "other, along the span.")
        return

    I = sec["I_x"] if axis == "y" else sec["I_y"]
    c = (sec["h"] if axis == "y" else sec["w"]) / 2.0
    depth = sec["h"] if axis == "y" else sec["w"]
    if L / depth < 8:
        rep.findings.append(Finding(
            INFO, "Too stubby for a beam-theory comparison.",
            f"Span/depth = {L / depth:.1f}. Shear deformation dominates below "
            f"about 8 and the closed form is not the right reference."))
        return
    tip = cantilever_tip_deflection(F, L, E, I)
    udl = cantilever_udl_deflection(F, L, E, I)
    sigma = bending_stress(F * L, c, I)
    rep.numbers.update({"beam_theory_tip_deflection_m": tip,
                        "beam_theory_udl_deflection_m": udl,
                        "beam_theory_max_stress_Pa": sigma})
    if isinstance(got_d, (int, float)) and got_d > 0:
        # Which closed form applies depends on how the load was spread,
        # so both are offered and the closer one is named.
        ref, label = min(((tip, "an end load"), (udl, "a distributed load")),
                         key=lambda t: abs(got_d - t[0]))
        _agreement(rep, f"Deflection against beam theory for {label}",
                   got_d, ref, 0.9, 1.15, "mm", 1e-3,
                   "Common causes, in order of likelihood: the restraint went "
                   "on the wrong face, the load went on as a total when it was "
                   "meant per entity, or the mesh is too coarse to resolve "
                   "bending.",
                   "Shear deformation, which beam theory leaves out, makes a "
                   "real beam a few percent softer.")

    away = results.get("von_mises_away_from_supports")
    reach = results.get("support_exclusion_mm")
    if isinstance(away, (int, float)) and isinstance(reach, (int, float)):
        at = bending_stress(F * max(L - reach / 1000.0, 0.0), c, I)
        rep.numbers["beam_theory_stress_at_exclusion_Pa"] = at
        _agreement(rep, f"Stress {reach:.3g} mm from the support against "
                        f"M*c/I there", away, at, 0.85, 1.2, "MPa", 1e6,
                   "The peak away from the support is somewhere beam theory "
                   "does not describe -- at the load, or in a detail the "
                   "idealised beam does not have.")
    else:
        got_s = results.get("von_mises_max")
        if isinstance(got_s, (int, float)) and sigma > 0 and got_s > 2.5 * sigma:
            rep.findings.append(Finding(
                INFO, f"Peak stress is {got_s / sigma:.1f}x the nominal "
                      f"M*c/I value.",
                "Expected where the restraint meets the part: a fixed face is "
                "a stress singularity in FE, and the peak there grows with "
                "mesh refinement instead of converging."))


def _check_frequency(rep: PreflightReport, by_op: dict, results: dict,
                     props: dict) -> None:
    """First bending mode of a cantilever: about the WEAKER axis."""
    geom = (by_op.get("set_geometry") or [None])[0]
    sec = section_properties(geom) if geom else None
    fixtures = by_op.get("add_fixture", [])
    got = results.get("first_frequency")
    if not (sec and len(fixtures) == 1 and _at_an_end(fixtures[0], "min")
            and isinstance(got, (int, float))):
        return
    L = geom.num("length")
    if L / max(sec["w"], sec["h"]) < 8:
        return
    E, rho, _ = _material(rep, by_op, props)
    ref = beam_first_frequency(E, min(sec["I_x"], sec["I_y"]), rho,
                               sec["area"], L)
    rep.numbers["beam_theory_first_frequency_Hz"] = ref
    _agreement(rep, "First frequency against the cantilever formula", got,
               ref, 0.93, 1.07, "Hz", 1.0,
               "The restraint is probably not a single clamped end, or the "
               "material's density is not what the check assumed.")


def _check_buckling(rep: PreflightReport, by_op: dict, results: dict,
                    props: dict) -> None:
    """Euler for a fixed-free column, about the weaker axis."""
    geom = (by_op.get("set_geometry") or [None])[0]
    sec = section_properties(geom) if geom else None
    fixtures = by_op.get("add_fixture", [])
    forces = by_op.get("add_force", [])
    got = results.get("buckling_load_factor")
    if not (sec and forces and len(fixtures) == 1
            and _at_an_end(fixtures[0], "min")
            and all(_axis_of(f) == "z" and _at_an_end(f, "max") for f in forces)
            and isinstance(got, (int, float))):
        return
    F = sum(f.num("magnitude") for f in forces)
    if F <= 0:
        return
    E, _, _ = _material(rep, by_op, props)
    P = euler_critical_load(E, min(sec["I_x"], sec["I_y"]),
                            geom.num("length"), "fixed_free")
    rep.numbers.update({"euler_critical_load_N": P,
                        "euler_load_factor": P / F})
    _agreement(rep, "Buckling load factor against Euler (fixed-free)", got,
               P / F, 0.9, 1.1, "", 1.0,
               "A factor far from Euler usually means the ends are not held "
               "as assumed, or the load is not along the axis.")


STEFAN_BOLTZMANN = 5.670374e-8


def _check_heat_balance(rep: PreflightReport, results: dict,
                        receipt: dict) -> None:
    """Steady state: every watt in leaves through convection and
    radiation.

    Q = sum h*A*(T - T_bulk) + sum eps*sigma*A*(T^4 - T_amb^4), solved
    for one T, for a part near-uniform in temperature -- which a metal
    part in air nearly always is (Biot is tiny). Compared as a RISE above
    the coolest surrounding temperature: in kelvin, a 100% error in the
    rise is still a 1% error."""
    loads = receipt.get("loads") or {}
    conv = loads.get("convection") or []
    rad = loads.get("radiation") or []
    if not isinstance(rad, list):
        rad = []            # receipts written before radiation was itemised
    Q = float(loads.get("heat_in_W") or 0.0)
    t_min, t_max = results.get("temperature_min"), results.get("temperature_max")
    if not (conv or rad) or Q <= 0 or loads.get("fixed_temperatures") \
            or not all(isinstance(t, (int, float)) for t in (t_min, t_max)):
        return

    def out(T: float) -> float:
        return (sum(c["h"] * c["area_m2"] * (T - c["bulk_K"]) for c in conv)
                + sum(r["emissivity"] * STEFAN_BOLTZMANN * r["area_m2"]
                      * (T ** 4 - r["ambient_K"] ** 4) for r in rad))

    base = min([c["bulk_K"] for c in conv] + [r["ambient_K"] for r in rad])
    lo, hi = base, base + 1.0
    while out(hi) < Q and hi < base + 1e5:
        hi = base + 2.0 * (hi - base)
    if out(hi) < Q:
        return              # no area to lose heat through
    for _ in range(80):
        mid = (lo + hi) / 2.0
        lo, hi = (mid, hi) if out(mid) < Q else (lo, mid)
    T_eq = (lo + hi) / 2.0
    rise, got = T_eq - base, (t_min + t_max) / 2.0 - base
    rep.numbers["heat_balance_temperature_K"] = T_eq
    uneven = (t_max - t_min) > 0.3 * rise
    _agreement(rep, "Temperature rise against the heat balance Q/(h*A)", got,
               rise, 0.7 if uneven else 0.95, 1.3 if uneven else 1.05, "K",
               1.0,
               "Heat is going somewhere the balance does not count -- a face "
               "without convection that should have it, or power applied to "
               "more entities than meant.",
               "The part is far from uniform, so this is a rough check."
               if uneven else "")


def _check_internal_flow(rep: PreflightReport, by_op: dict,
                         results: dict) -> None:
    """A pressure drop, as a loss coefficient and -- for a straight pipe --
    against Darcy-Weisbach.

    The loss coefficient is the number a pressure drop is compared by:
    zeta = dp / (rho V^2 / 2), with the plan's inlet velocity. Darcy is
    right to within a factor of two for a straight run; a CFD answer
    twenty times off it is a setup error, not a discovery."""
    dp = results.get("pressure_drop_Pa")
    if not isinstance(dp, (int, float)) or dp <= 0:
        return
    dom_op = (by_op.get("set_flow_domain") or [None])[0]
    fluid = (dom_op.options.get("fluid", "air") if dom_op else "air").lower()
    v = dom_op.num("velocity") if dom_op else 0.0
    for bc in by_op.get("add_flow_bc", []):
        v = v or bc.num("velocity")
    rep.numbers["pressure_drop_Pa"] = dp
    if v <= 0:
        rep.findings.append(Finding(
            INFO, f"Pressure drop {dp:.4g} Pa.",
            "No inlet velocity in the plan, so no loss coefficient."))
        return
    q = 0.5 * fluid_props(fluid)["rho"] * v ** 2
    zeta = dp / q
    rep.numbers["loss_coefficient"] = zeta
    rep.findings.append(Finding(
        INFO, f"Pressure drop {dp:.4g} Pa: a loss coefficient of {zeta:.3g} "
              f"at the plan's {v:g} m/s (zeta = dp / (rho V^2 / 2)).",
        "For scale: a fully open gate valve 0.2, a smooth bend 0.2, a sharp "
        "90 degree elbow 0.9, a sudden expansion about 1, a partly closed "
        "valve tens. It is only as right as the velocity: if the project's "
        "inlet is set differently from the plan, so is this."))
    geom = (by_op.get("set_geometry") or [None])[0]
    if geometry_shape(geom) == "tube":
        D = geom.num("outer_diameter") - 2.0 * geom.num("wall")
        L = geom.num("length")
        if D > 0 and L > 0:
            ref = pipe_pressure_drop(v, D, L, fluid)
            rep.numbers["darcy_pressure_drop_Pa"] = ref
            _agreement(rep, "Pressure drop against Darcy-Weisbach", dp, ref,
                       0.5, 2.0, "Pa", 1.0,
                       "Beyond a factor of two for a straight pipe is a setup "
                       "error: the laminar box ticked for a turbulent flow, the "
                       "wrong lids, or the goals on the wrong faces.")


def crosscheck(domain: str, ops: list, results: dict,
               receipt: dict | None = None) -> PreflightReport:
    """Compare a finished run against the textbook answer.

    Deliberately conservative about what it claims. Agreement is
    evidence, not proof; disagreement is a prompt to look, not a verdict.
    Where the idealisation does not apply -- a stubby beam, a shape with
    no closed form -- it says nothing rather than inventing a
    comparison.

    `receipt` is the run's own record: the material properties it read
    from the library, what it applied and to how much area. Without it
    the checks fall back to what the plan said."""
    rep = PreflightReport()
    receipt = receipt or {}
    by_op: dict[str, list] = {}
    for o in ops:
        by_op.setdefault(o.op, []).append(o)
    geom = (by_op.get("set_geometry") or [None])[0]
    props = receipt.get("material_props") or {}

    if domain in ("static", "nonlinear"):
        _check_equilibrium(rep, by_op, results, receipt)
        _check_safety(rep, results, props.get("yield", 0.0))

    if domain == "static":
        forces = by_op.get("add_force", [])
        shape = geometry_shape(geom)
        sec = section_properties(geom) if geom else None
        cantilever = (len(by_op.get("add_fixture", [])) == 1
                      and _at_an_end(by_op["add_fixture"][0], "min")
                      and all(_at_an_end(f, "max") for f in forces))
        if forces and shape == "plate_with_hole":
            E, _, _ = _material(rep, by_op, props)
            _check_plate_with_hole(rep, geom, forces, results, E)
        elif forces and sec and cantilever:
            E, _, _ = _material(rep, by_op, props)
            _check_beam(rep, geom, sec, forces, results, E)

    if domain == "frequency":
        _check_frequency(rep, by_op, results, props)

    if domain == "buckling":
        _check_buckling(rep, by_op, results, props)

    if domain == "thermal":
        _check_heat_balance(rep, results, receipt)

    if domain == "flow_internal":
        _check_internal_flow(rep, by_op, results)

    if domain == "flow_external" and isinstance(results.get("drag_coefficient"),
                                                (int, float)):
        cd = results["drag_coefficient"]
        rep.numbers["drag_coefficient"] = cd
        rep.findings.append(Finding(
            INFO, f"Cd = {cd:.3g}, from the project's own goal.",
            "For reference: a streamlined body 0.04-0.1, a modern car "
            "0.25-0.35, a sphere about 0.47, a long cylinder across the flow "
            "about 1.0-1.2 below Re 2e5, a flat plate normal to flow about "
            "1.2."))
    elif domain == "flow_external":
        dom_op = (by_op.get("set_flow_domain") or [None])[0]
        if dom_op:
            v = dom_op.num("velocity")
            fluid = dom_op.options.get("fluid", "air")
            F = results.get("drag_force")
            area = results.get("reference_area")
            if all(isinstance(x, (int, float)) and x > 0 for x in (v, F, area)):
                cd = drag_coefficient(F, v, area, fluid)
                rep.numbers["drag_coefficient"] = cd
                rep.findings.append(Finding(
                    INFO, f"Implied Cd = {cd:.3f}.",
                    "For reference: a streamlined body 0.04-0.1, a modern "
                    "car 0.25-0.35, a sphere about 0.47, a flat plate "
                    "normal to flow about 1.2. A value far outside that "
                    "range usually means the reference area is not what "
                    "the coefficient assumes."))

    return rep
