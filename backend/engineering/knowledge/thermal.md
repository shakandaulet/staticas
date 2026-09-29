---
kind: api
domains: [thermal, static, nonlinear]
confidence: working_code
---

# Thermal studies: heat in, heat out, and the number that decides

## Creating a thermal study

<!-- confidence: working_code -->

```python
err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
study = doc.StudyManager.CreateNewStudy3("Thermal", 3, 0, err)
```

Study type 3 is thermal. Read the type back off the created study and
compare before going further -- a wrong constant here produces a study
that accepts a convection boundary condition, meshes, solves, and
reports something that is not a temperature field.

## Every thermal study needs a way OUT

<!-- confidence: verified -->

This is the single most common setup error, and it is a physics error
rather than an API one. A steady-state thermal study with a heat source
and no convection, radiation or fixed-temperature boundary has **no
solution**: energy goes in and nothing removes it, so there is no
temperature at which the part is in balance. SolidWorks either fails to
converge or runs away to an absurd number.

The fix is never "increase the iteration limit". It is to add the
boundary condition the physical part actually has -- air around it,
a heatsink it bolts to, a coolant channel.

A transient study is different: with no outlet it simply heats up
forever, which is a valid answer to a badly posed question.

## Convection — the film coefficient is the whole model

<!-- confidence: working_code -->

Newton cooling: `q = h * (T_surface - T_bulk)`. Both numbers are
required. Applying convection with a bulk temperature left at its
default is the thermal equivalent of a load with no magnitude.

Typical values for `h`, in W/(m²·K):

| situation | h |
|-----------|---|
| still air, natural convection | 5 – 25 |
| forced air, fan | 25 – 250 |
| still water | 50 – 1 000 |
| forced water | 300 – 12 000 |
| boiling / condensing | 3 000 – 100 000 |

`h` is not a material property. It depends on the geometry, the fluid
and the flow speed, and choosing it IS the modelling assumption -- the
FE solve afterwards is the easy part. A thermal answer is only as good
as the `h` that was assumed, and the honest way to present one is with
the assumed value stated next to it.

When `h` genuinely cannot be estimated, the answer is not a better
guess: it is an internal-flow analysis that computes the heat transfer
instead of assuming it.

## Heat power versus heat flux

<!-- confidence: working_code -->

- **Heat power** is total watts into a body or face. This is how real
  components are specified: a 15 W processor, a 2 kW element.
- **Heat flux** is watts per square metre. This is how a boundary
  condition is specified when the area matters.

Confusing them is an error of exactly the surface area, which on a
small chip face is a factor of thousands. If a user says "a 20 W chip",
that is heat POWER on the body, not a flux.

## What the runs gave, against the heat balance

<!-- confidence: verified -->

Every thermal run is checked against the steady-state balance: the
watts going in (heat power, plus flux times area) must leave through
convection and radiation, `Q = sum h*A*(T - T_bulk) + sum
eps*sigma*A*(T^4 - T_amb^4)`, solved for one temperature. A metal part
in air is near-uniform, so that T is the part's temperature; the check
compares the RISE above the surroundings, because in kelvin a 100%
error in the rise is still a 1% error.

On the 50 × 100 × 2000 mm steel reference beam, 0.61 m² of surface:

| set-up | the run | the balance |
|--------|---------|-------------|
| 20 W in, still air h = 12 at 25 °C | rise 2.69 K (300.94 K max) | 2.73 K |
| 500 W/m² on the top face (50 W), same air | rise 6.77 K | 6.83 K |
| 20 W in, radiating only, emissivity 0.9, 25 °C | 304.0 K | 304.0 K |
| plate held at 100 °C on top, 20 °C underneath | 373.15 / 293.15 K | exactly those |

Radiation needs its view factor SET (`rad.ViewFactor = 1.0` for a part
seeing open surroundings). Left at the default the solve failed with
run error 24: 20 W going in and nowhere for it to go.

## Radiation matters more than people expect

<!-- confidence: verified -->

Radiated power goes as `emissivity * sigma * (T^4 - T_amb^4)`. Because
of the fourth power it is negligible near room temperature and
dominant when hot. A rough crossover: below about 100 °C radiation is a
small correction; above about 300 °C it is often the main path and
leaving it out over-predicts the temperature substantially.

Emissivity by surface, not by material: polished aluminium ≈ 0.05,
oxidised steel ≈ 0.8, most paint ≈ 0.9 regardless of colour in the
infrared. A bare shiny heatsink radiates almost nothing, which is why
they are anodised black.

## The Biot number decides whether this study is needed at all

<!-- confidence: verified -->

`Bi = h * L / k`, with `L` a characteristic thickness and `k` the
solid's conductivity.

- `Bi < 0.1` — the solid is essentially isothermal. An FE conduction
  solve will produce a pretty plot of a temperature field that is
  uniform to within a few percent, and a one-line lumped-capacitance
  hand calculation would have answered the question. Worth telling the
  user before they wait for a mesh.
- `Bi > 1` — internal gradients dominate, the temperature field is the
  interesting part, and the simulation is doing real work.

## Thermal load in a static study

<!-- confidence: working_code -->

A temperature field causes expansion, and constrained expansion causes
stress. Two ways to get there:

1. Apply a prescribed temperature directly in the static study, for a
   simple uniform or linear field.
2. Run a thermal study first, then import its result as a load into a
   static study. This is the right route whenever the temperature field
   itself is non-trivial.

The stress scales with the CONSTRAINT, not with the temperature alone.
A part free to expand develops no thermal stress no matter how hot it
gets; the same part welded at both ends develops `E * alpha * dT`,
which for steel is roughly 2.4 MPa per °C and reaches yield after
about 100 °C of restrained rise. That number is worth quoting to
anyone surprised by a thermal stress result.

## Reading results back

<!-- confidence: working_code -->

`ICWResults::GetMinMaxThermal(component, step, refgeom, unit, errcode)`
with component 0 (nodal temperature) and unit 0 (kelvin). It returns
`{node_min, minimum, node_max, maximum}`, so the maximum is the fourth
entry, not the second.

Ask for the unit rather than inheriting the study's: a maximum of "350"
is either a warm part or a catastrophically hot one depending on
whether it came back in kelvin or Celsius, and the difference is not
visible in the number.
