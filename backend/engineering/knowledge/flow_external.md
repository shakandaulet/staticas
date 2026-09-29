---
kind: api
domains: [flow_external]
confidence: working_code
---

# External flow: aerodynamics around the part

The API is reached exactly as for internal flow -- `FloWorks.App`, then
`GetAPI()` -- and the internal-flow file carries the solve-and-read
sequence. The script prints the project as a wizard checklist; when
the part already has a Flow project it solves it and reads the goals,
and a goal named "Drag Coefficient" is reported as Cd directly.

Run on the SolidWorks example "b2 - drag coefficient" (a cylinder
across the flow at Re = 10⁵): solved from the script in 91-103 s,
force goal 1.01 N, Cd 0.99 averaged over the last iterations (the last
iterate alone read 1.056) -- the subcritical plateau for a cylinder,
1.0-1.2. Report the AVERAGED goal: a bluff body sheds vortices and the
instantaneous value swings.

The aerodynamics below is standard and is the part that decides whether
a run is set up correctly at all.

## External does not take an inlet boundary condition

<!-- confidence: verified -->

The free stream comes from the **project's ambient conditions** --
velocity, pressure, temperature set on the project itself -- not from a
velocity inlet on a face. Adding an inlet face to an external analysis
either does nothing or fights the free stream, and it is a common
confusion for anyone whose last CFD setup was internal.

What an external analysis does need on faces: nothing, in the simple
case. The body sits in the computational domain and the domain
boundaries carry the free-stream condition.

## The computational domain is a modelling decision

<!-- confidence: verified -->

The default box is usually too small. Rules of thumb, in body lengths
from the body:

| direction | clearance |
|-----------|-----------|
| upstream | 3 – 5 |
| downstream | 8 – 15 (the wake needs room) |
| sides and above | 3 – 5 |

Too small and the boundaries constrain the flow -- an artificial
blockage that raises velocity around the body and inflates drag.
Downstream is the direction people under-size, because the wake is
invisible in the CAD window and is the largest thing in the solution.

Halving the domain with a symmetry plane where the geometry and the
flow are both symmetric is nearly free and roughly halves the cell
count. It is wrong the moment the flow is asymmetric -- vortex
shedding behind a bluff body is asymmetric even when the body is not,
so a symmetry plane suppresses the shedding and under-predicts drag.

## Drag and lift are goals, and the reference area must be stated

<!-- confidence: verified -->

Flow Simulation reports a FORCE. A coefficient needs a reference area
and the two must be quoted together:

```
Cd = F_drag / (0.5 * rho * V^2 * A_ref)
```

`A_ref` is frontal projected area for a vehicle or a bluff body, and
planform (wing) area for an aircraft. The same wing quoted against the
wrong one differs by roughly an order of magnitude, so "Cd = 0.9" means
nothing without the area it was divided by.

Reference values worth carrying: streamlined body 0.04, modern car
0.25–0.35, a sphere ~0.47, a cyclist ~0.9, a flat plate normal to flow
~1.2. Anything outside 0.02–2 for a normal object is almost certainly a
reference-area error rather than a discovery.

## Mach number: when compressibility starts to matter

<!-- confidence: verified -->

`M = V / a`, with `a` ≈ 343 m/s in air at 20 °C.

- `M < 0.3` (about 100 m/s, 370 km/h) — density variation is under
  about 5%, incompressible treatment is fine.
- `0.3 < M < 0.8` — compressible, must be set up as such.
- `M > 0.8` — transonic. Shocks appear, drag rises sharply, and the
  solver setup needs care beyond what this assistant configures.

Cars, cyclists, drones, buildings and HVAC are all comfortably
incompressible. The mistake runs the other way: setting up a
compressible solve for a 30 km/h flow costs time and buys nothing.

## Reynolds number here too, but with a different length

<!-- confidence: verified -->

For external flow the characteristic length is the chord, the body
length, or the diameter of a cylinder -- whatever the flow develops
over. Consequences:

- A car at 100 km/h has `Re` around 5 × 10⁶: fully turbulent, boundary
  layer thin, separation position governed by geometry.
- A small drone propeller blade can sit at `Re` of 10⁴–10⁵, where
  laminar separation bubbles dominate and where a fully turbulent model
  gives an answer that is confidently wrong about the stall.

The `laminar and turbulent` option is the safe default because it lets
transition happen rather than assuming it everywhere.

## Boundary layer resolution

<!-- confidence: unverified -->

Drag is a surface-shear integral, so it is only as good as the
near-wall mesh. Flow Simulation uses wall functions on a coarse near
wall mesh and a two-scale approach where the mesh permits. Practical
consequence: a coarse mesh gives a plausible pressure field and an
unreliable viscous drag, so a coarse external run is more trustworthy
for a bluff body -- where pressure drag dominates -- than for a
streamlined one, where skin friction is most of the total.

Say this when reporting a drag number from a coarse mesh. It is the
difference between "about right" and "this number is the part that is
wrong".

## Wind tunnel turbulence intensity

<!-- confidence: unverified -->

Default free-stream turbulence intensity is around 0.1% -- a clean wind
tunnel. Real atmospheric flow is 1–10%, and higher turbulence delays
separation and changes drag on a bluff body measurably. For anything
outdoors, raising it is more realistic than leaving the default.
