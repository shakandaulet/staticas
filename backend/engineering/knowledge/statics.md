---
kind: api
domains: [static, nonlinear, buckling, fatigue, drop]
confidence: working_code
---

# Structural studies: loads, restraints and what they mean

## AddRestraint — ICWLoadsAndRestraintsManager

<!-- confidence: verified -->

```python
err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
fixture = study.LoadsAndRestraintsManager.AddRestraint(
    restraint_type, [face_object], NOTHING, err)
```

The entity argument is an ARRAY of COM objects, even for one face. A
bare object instead of a one-element list is a type error.

Restraint type codes, from `swsRestraintType_e` in the API reference:

| code | restraint | what it physically means | run here |
|------|-----------|--------------------------|----------|
| 0 | Fixed geometry | everything held | yes |
| 1 | Immovable | translations held, rotations free | shells and beams only: error 15 on a solid, where it is the same as fixed |
| 2 | Symmetric | motion through the plane held (flat faces) | yes |
| 3 | Roller / slider | normal motion held, in-plane free (flat faces) | yes |
| 4 | Hinge | pinned on a cylinder, free to turn about it | yes, on bolt holes |

The roller and symmetry run was a block 50 × 100 × 200 on a roller,
with symmetry on two faces and 100 kN pressed on its top: free to
spread sideways, it carried exactly F/A = 10.000 MPa everywhere and its
far corner moved 5.498 µm against 5.50 from Hooke and Poisson. An
earlier version of this project had roller at 2, so a simple support
came out as a symmetry restraint -- which solves, and answers a stiffer
problem than the one asked.

**Choosing one.** "Fixed" is a weld or a built-in end. "Immovable" is a
pin that cannot slide. "Roller/slider" is what a textbook simple support
usually means -- and using Fixed where the problem says simply supported
roughly halves the deflection and moves the peak moment, which looks
like a mesh problem and is not.

## AddForce — ICWLoadsAndRestraintsManager

<!-- confidence: verified -->

```python
force = study.LoadsAndRestraintsManager.AddForce(
    force_type, [face_object], NOTHING, err)
force.ForceBeginEdit()
force.NormalForceOrTorqueValue = newtons
force.ForceEndEdit            # property. no parentheses.
```

`ForceBeginEdit()` / `ForceEndEdit` bracket every property change. Set
values between them or they do not stick, and nothing reports that they
did not.

**The value goes on EACH selected face.** Measured with the reaction
forces: 1000 N over the two flange tips of an I-beam came back as
2000 N at the supports, for a directional and a normal force alike. The
API has no "total" switch for a solid (not in AddForce, AddForce2,
AddForce3 or ICWForce), so a total is divided by the face count before
it is written. It matters most on holes: SolidWorks often splits one
hole into two half-cylinders, and 2 kN "through the eye" of the sample
control arm went on as 4 kN until the equilibrium check caught it.
"1 kN per bolt" is the number of holes times 1 kN -- holes, not faces.

**Direction.** A positive normal force pushes INTO the face. A load
along a global axis is a directional force, its component stated
against a reference plane; that selects the plane, which replaces
whatever the user had selected.

## AddPressure — ICWLoadsAndRestraintsManager

<!-- confidence: working_code -->

Same shape as AddForce. Pressure is force per unit area, so unlike a
force it does not care how many faces are selected -- each gets the
same pressure. That difference is why "the pressure came out four times
too low" is almost always a force that should have been a pressure.

## Gravity and centrifugal loads

<!-- confidence: working_code -->

Self-weight is applied through the loads manager with a reference
direction, not with a force on a face. Two things worth saying to a
user:

- Gravity is negligible for a small steel bracket under a real load and
  dominant for a long unloaded beam. The test is whether
  `rho * A * L * g` is a noticeable fraction of the applied load.
- The direction is a direction in MODEL space. A part modelled with the
  span along Z and depth along Y needs gravity against Y; getting it
  wrong applies the weight along the beam, where it does nothing
  visible and quietly removes the self-weight case from the answer.

## What the result numbers actually are

<!-- confidence: verified -->

- **von Mises stress** is a scalar combination of the stress state, for
  comparison against a yield strength. It is always positive and it
  hides whether the material is in tension or compression -- which
  matters for brittle materials, where a principal-stress criterion is
  the right one instead.
- **URES** is resultant displacement magnitude, not deflection in the
  load direction. For a simple bending case they are nearly the same;
  for anything with twist they are not.
- **Factor of safety** is yield divided by von Mises, computed
  pointwise. An FOS below 1 means predicted yielding somewhere, which
  for a linear static study means the answer is already outside the
  assumption it was computed under.

## What a run of this actually gave

<!-- confidence: verified -->

SOLIDWORKS 2024, a 50 × 100 × 2000 mm plain carbon steel cantilever
clamped at one end, 5 kN downward on the free end face, high-quality
(second-order) mesh at the 21.5 mm SolidWorks recommends for that body:

| quantity | the run | closed form |
|----------|---------|-------------|
| tip deflection | 15.24 mm | 15.24 mm (F·L³/3EI, E = 210 GPa) |
| von Mises 43 mm from the root | 117.0 MPa | 117.4 MPa (M·c/I there) |
| peak von Mises | 122.7 MPa | -- (at the restraint: a singularity) |
| reaction at the root | 4999.999 N | 5000 N |
| factor of safety away from the root | 1.89 | yield 220.6 MPa |

The hand check used to assume E = 205 GPa, found the run 2% stiff, and
blamed a draft mesh. Both halves were wrong: Plain Carbon Steel is
210 GPa in the library, and the mesh was not draft (see the mesh
section). With the library's E the two agree to 0.03%.

## Real parts: what the runs gave

<!-- confidence: verified -->

SOLIDWORKS 2024, plain carbon steel, draft mesh at the size SolidWorks
recommends for each body. `tests/solidworks/real_parts.py` re-runs all
of these.

| part | set-up | the run | reference |
|---|---|---|---|
| I-beam 100×200, web 8, flanges 12, 2 m | fixed end, 10 kN down on the free end | 5.29 mm | 5.11 mm, F·L³/3EI with the I-section (+3.5%: shear) |
| plate 100×300×10, 25 mm hole | one end fixed, 20 kN pull on the other | 59.4 MPa at the hole | 64.6 MPa = Kt 2.42 × net stress |
| same plate | | 30.0 µm stretch | 28.6 µm, F·L/EA (the hole softens it) |
| stepped shaft (sample) | big end fixed, 1 kN down on the shoulder | 1.78 µm | 1.70 µm, F·L/EA below the shoulder |
| angle bracket 120×100×60, t 10, fillet 8 | bolted through 2 × 11 mm holes, 500 N on the upright's top | 0.36 mm, 71.6 MPa | no closed form: the upright alone gives 0.12 mm, the base bends too |
| anchor plate (sample) | 4 bolt holes fixed, 1 kN up on the eye | 35.7 MPa | reactions 1000 N |
| control arm (sample) | 60 mm bore fixed, 2 kN sideways through the 14 mm eye | 38.3 MPa | reactions 2000 N |
| same bracket on hinges at its holes | as above | 0.3625 mm | a shade softer than fixed holes, as a pin should be |

The control arm read 76.6 MPa before the force was divided among the
eye's two faces: SolidWorks had applied 4 kN, and the equilibrium check
is what said so. With the I-beam and E = 210 GPa beam theory gives
5.11 mm against the run's 5.29: the 3.5% is shear deformation in an
8 mm web, which beam theory leaves out; the stress 50 mm from the root
agreed to 0.01% (78.41 against 78.40 MPa).

The hole is the one peak here worth reading straight off the plot: at a
hole edge the stress converges, so the maximum can be held against Kt
times the net-section stress (Heywood: Kt = 2 + (1 − d/W)³). The run
read it 8% low. The peak at a fixed face does not converge and should
never be compared with anything -- which is why every structural run
now also reports the peak a short distance from every restraint.

## Checks every structural run makes

<!-- confidence: verified -->

- **Equilibrium.** The reactions at the restraints
  (GetReactionForcesAndMomentsWithSelections) against the load the run
  wrote. They disagree when SolidWorks read the load differently from
  how it was meant -- per face instead of in total, a unit, a sign.
- **Stress away from the supports.** Nodal von Mises (GetStress, twelve
  numbers a node, von Mises at index 9 after the node id) outside a zone
  round every restraint: two elements, or half the largest restrained
  face. This is the number to compare with yield and with M·c/I.
- **Factor of safety**, yield over von Mises, at the peak and away from
  the supports; yield comes from the material library file.
- **Convergence**, when asked: solved again with elements 0.7 times the
  size; the beam moved 0.03% (15.24 to 15.25 mm).

## Stress singularities at restraints

<!-- confidence: verified -->

A fixed face in FE is a mathematical singularity: the stress there does
not converge as the mesh is refined, it grows without limit. The peak
von Mises in a cantilever study is therefore almost always AT the
built-in end and is almost always meaningless.

The practical rule: read the stress a distance of roughly one section
depth away from the restraint, or compare against `M*c/I` at a section
away from the support. A user asking "why does the stress double when I
refine the mesh" is asking about this, and the answer is not to refine
further.

## Mesh density and what it costs

<!-- confidence: working_code -->

```python
mesh = study.Mesh
mesh.MesherType = 0
mesh.Quality = 1                 # swsMeshQuality_e: 0 = draft, 1 = HIGH
status = study.CreateMesh(0, max_element, min_element)
if status != 0:
    raise RuntimeError(f"Mesh failed, code {status}")
```

<!-- confidence: verified -->

**0 is draft and 1 is high**, from the type library and from a run.
This project had them the other way round and called every run
"draft" while meshing it second-order. The same beam, same element
size: quality 0 gave 1 915 nodes and 13.67 mm, 10% stiff; quality 1
gave 12 123 nodes and 15.24 mm, the closed form. High is the default;
draft is for a quick look and says so.

Element count scales roughly with the cube of the inverse element size,
so halving the element size is an eight-fold increase in work.

This project floors the element sizes and refuses to lower them. It is
not a performance preference: an unbounded mesh request on a shared
machine hangs SolidWorks for everyone using it.
