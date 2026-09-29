"""
Unit normalisation.

Everything inside the planner, the emitter and the physics checks is in
SI base units, because the SolidWorks API is: lengths in metres, forces
in newtons, pressures in pascals, temperatures in kelvin. Nothing else
in the pipeline is allowed to carry a unit string around.

This module is the single place where "500 kN", "2.5 in", "150 psi",
"60 mph" and "200 degC" become numbers, and the single place where a
number goes back out to a human with a unit attached.

WHY THIS IS A SEPARATE MODULE AND NOT A PROMPT INSTRUCTION
----------------------------------------------------------
Asking the model to "convert everything to SI" works most of the time,
which is the worst possible failure rate for a unit conversion. A
silent factor of 1000 on a force is a simulation that runs, finishes,
and reports a plausible-looking wrong answer. So the model is asked for
{value, unit} pairs -- something it is reliable at -- and the
arithmetic happens here, in code that can be tested.

TEMPERATURE IS NOT A SCALE FACTOR
---------------------------------
Celsius and Fahrenheit have offsets, so they cannot live in the same
multiply-by-a-constant table as the rest. A temperature DIFFERENCE
("raise it by 20 degC") converts differently from a temperature
LEVEL ("hold it at 20 degC"). Both exist in thermal boundary
conditions, so both are handled, explicitly, below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Quantity kinds. Used to reject "apply a force of 5 metres" before it
# reaches SolidWorks and becomes a mystery.
# --------------------------------------------------------------------------
LENGTH = "length"
FORCE = "force"
PRESSURE = "pressure"
TEMPERATURE = "temperature"
TEMPERATURE_DELTA = "temperature_delta"
VELOCITY = "velocity"
ACCELERATION = "acceleration"
MASS_FLOW = "mass_flow"
VOLUME_FLOW = "volume_flow"
POWER = "power"
HEAT_FLUX = "heat_flux"
CONVECTION = "convection_coefficient"
TORQUE = "torque"
ANGLE = "angle"
ROTATION = "rotational_speed"
DENSITY = "density"
DIMENSIONLESS = "dimensionless"

# Multiplicative units: value_SI = value * factor.
# Keys are lower-cased and stripped of spaces before lookup.
_FACTORS: dict[str, tuple[str, float]] = {
    # length -> m
    "m": (LENGTH, 1.0), "meter": (LENGTH, 1.0), "meters": (LENGTH, 1.0),
    "metre": (LENGTH, 1.0), "metres": (LENGTH, 1.0),
    "cm": (LENGTH, 1e-2), "mm": (LENGTH, 1e-3), "um": (LENGTH, 1e-6),
    "km": (LENGTH, 1e3),
    "in": (LENGTH, 0.0254), "inch": (LENGTH, 0.0254), "inches": (LENGTH, 0.0254),
    '"': (LENGTH, 0.0254),
    "ft": (LENGTH, 0.3048), "foot": (LENGTH, 0.3048), "feet": (LENGTH, 0.3048),

    # force -> N
    "n": (FORCE, 1.0), "newton": (FORCE, 1.0), "newtons": (FORCE, 1.0),
    "kn": (FORCE, 1e3), "mn": (FORCE, 1e6),
    "lbf": (FORCE, 4.4482216152605), "lb": (FORCE, 4.4482216152605),
    "kgf": (FORCE, 9.80665), "kip": (FORCE, 4448.2216152605),

    # pressure / stress -> Pa
    "pa": (PRESSURE, 1.0), "kpa": (PRESSURE, 1e3),
    "mpa": (PRESSURE, 1e6), "gpa": (PRESSURE, 1e9),
    "bar": (PRESSURE, 1e5), "mbar": (PRESSURE, 1e2),
    "atm": (PRESSURE, 101325.0),
    "psi": (PRESSURE, 6894.757293168), "ksi": (PRESSURE, 6894757.293168),
    "torr": (PRESSURE, 133.322368421), "mmhg": (PRESSURE, 133.322368421),

    # velocity -> m/s
    "m/s": (VELOCITY, 1.0), "mps": (VELOCITY, 1.0),
    "km/h": (VELOCITY, 1 / 3.6), "kph": (VELOCITY, 1 / 3.6),
    "mph": (VELOCITY, 0.44704),
    "ft/s": (VELOCITY, 0.3048), "fps": (VELOCITY, 0.3048),
    "kt": (VELOCITY, 0.514444), "knot": (VELOCITY, 0.514444),
    "knots": (VELOCITY, 0.514444),
    "mach": (VELOCITY, 340.29),   # ISA sea level; see note in physics.py

    # acceleration -> m/s^2
    "m/s2": (ACCELERATION, 1.0), "m/s^2": (ACCELERATION, 1.0),
    "g": (ACCELERATION, 9.80665), "gs": (ACCELERATION, 9.80665),
    "ft/s2": (ACCELERATION, 0.3048), "ft/s^2": (ACCELERATION, 0.3048),

    # mass flow -> kg/s
    "kg/s": (MASS_FLOW, 1.0), "kg/h": (MASS_FLOW, 1 / 3600),
    "g/s": (MASS_FLOW, 1e-3), "lb/s": (MASS_FLOW, 0.45359237),
    "lbm/s": (MASS_FLOW, 0.45359237),

    # volume flow -> m^3/s
    "m3/s": (VOLUME_FLOW, 1.0), "m^3/s": (VOLUME_FLOW, 1.0),
    "l/s": (VOLUME_FLOW, 1e-3), "l/min": (VOLUME_FLOW, 1e-3 / 60),
    "lpm": (VOLUME_FLOW, 1e-3 / 60),
    "m3/h": (VOLUME_FLOW, 1 / 3600), "m^3/h": (VOLUME_FLOW, 1 / 3600),
    "gpm": (VOLUME_FLOW, 6.30901964e-5),
    "cfm": (VOLUME_FLOW, 4.719474432e-4),

    # power -> W
    "w": (POWER, 1.0), "kw": (POWER, 1e3), "mw": (POWER, 1e6),
    "hp": (POWER, 745.699872),
    "btu/h": (POWER, 0.29307107), "btu/hr": (POWER, 0.29307107),

    # heat flux -> W/m^2
    "w/m2": (HEAT_FLUX, 1.0), "w/m^2": (HEAT_FLUX, 1.0),
    "kw/m2": (HEAT_FLUX, 1e3), "kw/m^2": (HEAT_FLUX, 1e3),

    # convection film coefficient -> W/(m^2*K)
    "w/m2k": (CONVECTION, 1.0), "w/m^2k": (CONVECTION, 1.0),
    "w/(m2*k)": (CONVECTION, 1.0), "w/(m^2*k)": (CONVECTION, 1.0),
    "w/m2/k": (CONVECTION, 1.0),

    # torque -> N*m
    "nm": (TORQUE, 1.0), "n*m": (TORQUE, 1.0), "n-m": (TORQUE, 1.0),
    "knm": (TORQUE, 1e3), "lbf*ft": (TORQUE, 1.3558179483),
    "lbft": (TORQUE, 1.3558179483),

    # angle -> rad
    "rad": (ANGLE, 1.0), "deg": (ANGLE, 0.017453292519943295),
    "degree": (ANGLE, 0.017453292519943295),
    "degrees": (ANGLE, 0.017453292519943295),

    # rotation -> rad/s
    "rad/s": (ROTATION, 1.0), "rpm": (ROTATION, 0.10471975511965977),
    "hz": (ROTATION, 6.283185307179586),

    # density -> kg/m^3
    "kg/m3": (DENSITY, 1.0), "kg/m^3": (DENSITY, 1.0),
    "g/cm3": (DENSITY, 1e3), "g/cm^3": (DENSITY, 1e3),

    "": (DIMENSIONLESS, 1.0), "-": (DIMENSIONLESS, 1.0),
}

# Temperature needs offsets, so it is kept out of the factor table
# entirely rather than being special-cased inside it.
_TEMPERATURE_ALIASES = {
    "k": "K", "kelvin": "K",
    "c": "C", "degc": "C", "celsius": "C", "centigrade": "C",
    "°c": "C", "deg c": "C",
    "f": "F", "degf": "F", "fahrenheit": "F", "°f": "F", "deg f": "F",
}

# The SI unit each kind normalises to, for display and for receipts.
SI_UNIT = {
    LENGTH: "m", FORCE: "N", PRESSURE: "Pa", TEMPERATURE: "K",
    TEMPERATURE_DELTA: "K", VELOCITY: "m/s", ACCELERATION: "m/s^2",
    MASS_FLOW: "kg/s",
    VOLUME_FLOW: "m^3/s", POWER: "W", HEAT_FLUX: "W/m^2",
    CONVECTION: "W/(m^2*K)", TORQUE: "N*m", ANGLE: "rad",
    ROTATION: "rad/s", DENSITY: "kg/m^3", DIMENSIONLESS: "",
}


class UnitError(ValueError):
    """Raised for an unknown unit, or one of the wrong kind.

    A distinct type so the server can answer 'I do not know the unit
    "furlongs"' with a 400 instead of a 500 traceback."""


@dataclass(frozen=True)
class Quantity:
    """A number that knows what it is.

    `value` is always SI. `original` keeps what the user actually typed
    so the plan card can echo "500 kN" back instead of "500000.0 N",
    which is what makes a mis-parse visible before it is simulated."""
    value: float
    kind: str
    original: str = ""

    @property
    def unit(self) -> str:
        return SI_UNIT.get(self.kind, "")

    def as_dict(self) -> dict:
        return {"value": self.value, "unit": self.unit,
                "kind": self.kind, "original": self.original}

    def __str__(self) -> str:
        return f"{self.value:g} {self.unit}".strip()


def _clean(unit: str) -> str:
    u = unit.strip().lower()
    u = u.replace("·", "*")          # middle dot
    u = u.replace("²", "2").replace("³", "3")   # superscripts
    u = u.replace(" ", "")
    return u


def convert(value: float, unit: str, expect: str | None = None) -> Quantity:
    """Convert one {value, unit} pair to SI.

    `expect` is the quantity kind the caller requires. It is what stops
    a length being accepted where a force belongs -- the check exists
    because an LLM filling a slot called `magnitude` will occasionally
    fill it with the wrong physical quantity, and SolidWorks will
    happily simulate the result."""
    raw = f"{value:g} {unit}".strip()
    key = _clean(unit)

    # Temperature first: it is the one family with an offset.
    if key in _TEMPERATURE_ALIASES:
        scale = _TEMPERATURE_ALIASES[key]
        if expect == TEMPERATURE_DELTA:
            # A DIFFERENCE of 20 degC is 20 K, not 293.15 K. Getting
            # this backwards turns "warm it by 20 degrees" into
            # "hold it at 20 degrees", which is a different problem
            # with a believable answer.
            step = {"K": 1.0, "C": 1.0, "F": 5.0 / 9.0}[scale]
            return Quantity(value * step, TEMPERATURE_DELTA, raw)
        kelvin = {
            "K": lambda v: v,
            "C": lambda v: v + 273.15,
            "F": lambda v: (v - 32.0) * 5.0 / 9.0 + 273.15,
        }[scale](value)
        if kelvin < 0:
            raise UnitError(
                f"{raw} is below absolute zero ({kelvin:.2f} K). "
                "Check whether the unit should be Celsius rather than kelvin."
            )
        return Quantity(kelvin, TEMPERATURE, raw)

    if key not in _FACTORS:
        raise UnitError(
            f"Unknown unit {unit!r}. Supported: "
            f"{', '.join(sorted(set(_FACTORS)))} plus K / degC / degF."
        )

    kind, factor = _FACTORS[key]
    if expect and kind != expect and expect != DIMENSIONLESS:
        raise UnitError(
            f"{raw} is a {kind}, but a {expect} is required here. "
            f"Use one of the {expect} units instead."
        )
    return Quantity(value * factor, kind, raw)


_NUM_UNIT = re.compile(
    r"^\s*([-+]?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?)\s*(.*?)\s*$"
)


def parse(text: str, expect: str | None = None) -> Quantity:
    """Parse a free-form string like '500 kN' or '2,5 m' into SI.

    The comma decimal separator is accepted because this tool is used in
    places where that is the normal way to write a number, and rejecting
    it produces a baffling error about an unknown unit ',5'."""
    m = _NUM_UNIT.match(text or "")
    if not m:
        raise UnitError(f"Could not read a number out of {text!r}.")
    number = float(m.group(1).replace(",", "."))
    return convert(number, m.group(2), expect)


def humanize(q: Quantity, prefer: str | None = None) -> str:
    """Render an SI quantity back in a unit a person would use.

    Results come out of SolidWorks in SI. 3.4e8 Pa is correct and
    unreadable; 340 MPa is the same number in the form an engineer
    checks against a yield strength without reaching for a calculator."""
    v = q.value
    if prefer:
        try:
            _, factor = _FACTORS[_clean(prefer)]
            return f"{v / factor:.4g} {prefer}"
        except KeyError:
            pass

    if q.kind == PRESSURE:
        if abs(v) >= 1e9:
            return f"{v / 1e9:.4g} GPa"
        if abs(v) >= 1e6:
            return f"{v / 1e6:.4g} MPa"
        if abs(v) >= 1e3:
            return f"{v / 1e3:.4g} kPa"
    if q.kind == FORCE and abs(v) >= 1e3:
        return f"{v / 1e3:.4g} kN"
    if q.kind == LENGTH and abs(v) < 0.01:
        return f"{v * 1e3:.4g} mm"
    if q.kind == TEMPERATURE:
        return f"{v - 273.15:.4g} degC"
    if q.kind == POWER and abs(v) >= 1e3:
        return f"{v / 1e3:.4g} kW"
    return f"{v:.4g} {q.unit}".strip()
