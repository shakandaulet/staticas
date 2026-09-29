---
kind: api
domains: [flow_internal]
confidence: working_code
---

# Internal flow: fluid mechanics through the part

Everything in this file concerns **SolidWorks Flow Simulation**, which
is a separate add-in from SolidWorks Simulation with its own object
model. Connecting, solving and reading goals have been run here on the
SolidWorks examples; creating a project from scratch has not, so the
script prints the project as a wizard checklist and runs the project
the part already has. The physics in this file is standard fluid
mechanics and it is the part that usually decides whether a run is
worth starting.

## Connecting to the Flow Simulation API

<!-- confidence: verified -->
<!-- pin -->

The ProgID is `FloWorks.App`, and the chain goes through GetAPI:

```python
flow = swApp.GetAddInObject("FloWorks.App")   # NOT FlowSimulation.*
api = flow.GetAPI()                           # IAppApi
doc = api.GetDocument(swModel)                # IDocumentApi
project = doc.IActiveProject                  # IProjectApiHandler
```

Things that cost an afternoon each if nobody says them:

- **The API is a separate install.** Flow Simulation being installed is
  not enough: its API ships as its own SDK, an unrun installer sitting
  in `SOLIDWORKS Flow Simulation\API\`. Until that is installed there is
  no type library and no registered object at all, and every ProgID
  returns None.
- **The objects carry no type information.** Calls by name all fail
  until wrappers are generated from `binCFW\floworks.tlb` (pywin32:
  `gencache.EnsureModule("{47A51DD7-BD1E-4B5C-97BE-1DAAE5C7E715}", 0,
  4, 0)`). `FlowSimulation.CwApplication`, which reads like the obvious
  name and appears in older notes, is not registered by anything.
- **Installed is not loaded.** On a freshly started SolidWorks
  `GetAddInObject("FloWorks.App")` is None until
  `app.LoadAddIn(<...>\binCFW\FW03.dll)` returns 0. After an exception
  inside SolidWorks ("The server threw an exception") LoadAddIn failed
  too, until SolidWorks was restarted.
- **Two other APIs are on disk and not usable.** NIKAPI (reads goals
  straight from a .fld) and the newer EFD API (EFDLauncher, which can
  create features) are not registered by the SDK installer: "Library
  not registered" / "Class not registered". Registering them needs an
  administrator.

## Solving and reading goals through the API

<!-- confidence: verified -->
<!-- pin -->

```python
if not project.Solve(True, True, True, False):   # mesh, recalc, close monitor, no report
    raise RuntimeError("solve did not finish")
post = project.GetPostDocAPI()
post.LoadLastResults()
goals = post.GetGoals()
for i in range(1, goals.GetGoalsCount() + 1):   # FROM 1: index 0 is None
    g = goals.GetGoalByIndex(i)
    if g is None or g.IsServiceGoal():            # dm/m, Serv Press, ...
        continue
    print(g.GetGoalName(), g.GetAvValue(), g.GetProgress())
```

`Solve` blocks until the solver finishes: the "hydraulic loss" example
in under a minute, "drag coefficient" in 91 s. The other route --
`PrepareToRun`, then `CalculationData.RunSolver = True` -- only LOOKS
like it works: the solver starts as a TCP server waiting for a monitor
that never connects, its log stops at "TCP server started", and the
results file it leaves is byte for byte the initial state. That is why
reading goals "raised even with a finished solve": nothing had been
solved.

Report the AVERAGED value (`GetAvValue`): it is what convergence was
judged on. A goal below 100% progress did not converge.

Run on the SolidWorks example "b1 - hydraulic loss" (water through a
ball valve): total pressure 119 714 Pa at the inlet goal, 103 442 Pa at
the outlet goal, a loss of 16 272 Pa.

## Internal means the volume must be SEALED

<!-- confidence: verified -->

This is the error that stops more internal projects than everything
else combined. An internal analysis solves the fluid volume enclosed by
the model, and it can only find that volume if the volume is closed.
Every opening -- every pipe end, every port -- needs a **lid**: a flat
solid feature capping the hole.

Symptoms of forgetting: the wizard reports the geometry is not closed,
or "a fluid volume could not be recognised", and the mesh never builds.

Lids are also where boundary conditions are applied. A velocity inlet
is applied to the INNER face of the inlet lid, not to the pipe mouth.
This is worth stating explicitly when generating a plan, because the
face a user would point at is usually the wrong one.

The practical workflow is: cap every opening, run the Check Geometry
tool, confirm the fluid volume is found, and only then set boundary
conditions.

## Boundary conditions must not over- or under-specify

<!-- confidence: verified -->

A well-posed internal problem needs the flow rate fixed at one place
and the pressure level fixed at another. The rules:

- **Valid**: velocity/mass-flow/volume-flow inlet + static-pressure
  outlet. This is the normal case and what to default to.
- **Valid**: total-pressure inlet + static-pressure outlet. Use when
  the flow rate is the unknown being solved for.
- **Invalid**: flow rate at BOTH inlet and outlet. The pressure level
  is then undetermined -- the solver has no equation fixing it and will
  not converge, or converges to an arbitrary offset.
- **Invalid**: pressure at both ends with no flow specification, in a
  case where mass is not conserved by the geometry.

Mass must balance: the sum of inlet mass flows must equal the sum of
outlet mass flows. For an incompressible fluid with equal-area ports
that is the same as the volume flows balancing.

## Reynolds number decides the model, and it is not optional

<!-- confidence: verified -->

`Re = rho * V * D / mu`, with `D` the hydraulic diameter (`4A/P` for a
non-circular duct, the plain diameter for a round pipe).

- `Re < 2300` — laminar. Pressure drop goes as velocity to the first
  power, the profile is parabolic, mixing is poor.
- `2300 < Re < 4000` — transition. No correlation applies cleanly.
  Quote results here with a caveat and not to three significant
  figures.
- `Re > 4000` — turbulent. Pressure drop goes as roughly velocity
  squared, the profile is blunt, wall roughness starts to matter.

Solving a turbulent flow with the laminar option selected converges and
under-predicts both pressure drop and heat transfer, often by several
times. It is the flow-simulation equivalent of the wrong study type: no
error, wrong answer. Air in a 25 mm duct is already turbulent at about
1.5 m/s, so "turbulent" is the normal case for anything with a fan in
it.

## Water hammer, cavitation and the limits of a steady solve

<!-- confidence: verified -->

A steady incompressible solve says nothing about what happens when a
valve slams. If a user asks about surge, hammer or a pump trip, the
answer is a transient analysis with an appropriate fluid model, and
the honest response is to say the steady project will not show it.

Cavitation appears where local static pressure falls below the vapour
pressure -- at a sudden contraction, on the suction side of a pump, at
a sharp bend. Flow Simulation has a cavitation model that must be
switched on; without it the solver will happily report a negative
absolute pressure, which is the signal to turn it on rather than a
result to report.

## Pressure drop: what to expect before you run

<!-- confidence: verified -->

Darcy-Weisbach for a straight run:

```
dp = f * (L/D) * rho * V^2 / 2
f = 64/Re                    (laminar)
f ~ 0.02 - 0.04              (turbulent, commercial pipe)
```

Fittings are handled by a loss coefficient `K`, with `dp = K*rho*V²/2`:
a sharp 90° elbow is roughly K = 0.9, a smooth long-radius bend 0.2, a
sudden expansion nearly 1.0, a fully open gate valve 0.2.

Running this before the simulation is not wasted work. It takes a
minute, it is right to within a factor of two, and a CFD result that
disagrees with it by a factor of twenty is a setup error rather than a
discovery.

## Goals decide convergence

<!-- confidence: unverified -->

Flow Simulation does not converge on a residual the way a structural
solver converges on a displacement norm. It converges on **goals** --
quantities you declare that it watches until they stop changing. A
project with no goal stops on a travel count instead, and can report a
drag force or a pressure drop that is still drifting.

So: always declare a goal for the quantity actually being asked about.
A surface goal on the inlet lid for static pressure and one on the
outlet, with an equation goal for the difference, is the standard setup
for a pressure-drop question.

## Mesh level

<!-- confidence: unverified -->

The initial mesh is set by a level from 1 to 7. Each step up is roughly
a doubling of cell count in each direction where it refines, so level 5
is not modestly more expensive than level 3 -- it can be an order of
magnitude. Level 3 is a reasonable first pass for a smooth internal
duct; small gaps and thin walls need local refinement rather than a
global level increase, because a global increase spends all its cells
in the open volume where nothing is happening.
