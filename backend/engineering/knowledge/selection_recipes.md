---
kind: recipe
domains: []
confidence: verified
---

# Picking the right geometry: "this face", "the bolt holes", "the free end"

These recipes are injected into every plan rather than retrieved,
because getting the target wrong is the failure that produces a
believable wrong answer rather than an error. A load on the wrong face
solves just as happily as one on the right face.

Everything below was run on SOLIDWORKS 2024 against real parts: three
SolidWorks sample parts, a sample shaft, and an I-beam, a plate with a
hole and an angle bracket built by this project.

## "This face" is the live selection, read before anything else

`active_selection` is whatever the user has highlighted in SolidWorks
when the script starts. The script reads it FIRST (`capture_selection`),
before the study exists. Measured: creating the study, applying the
material and adding restraints leave the selection alone, but a
directional force REPLACES it with the Front plane, which it selects to
state its components against. A target read after one of those got the
plane instead of the face.

- Nothing selected: the script stops and says so. It never falls back
  to a guessed face.
- SolidWorks holds ONE selection. A restraint and a load both on "this
  face" land on the same faces, and a load on a restrained face goes
  straight into the support. The planner asks instead of emitting that.
- Checked: the angle bracket loaded through a face picked by hand gave
  0.3594 mm and 71.58 MPa, the same as the same face found by its role.

## Named geometry is resolved from the part's own shape

| the user says | selector | what it finds |
|---|---|---|
| "the top" / "the bottom" | role top / bottom | flat faces facing +Y / -Y |
| "the front" / "the back" | role front / back | +Z / -Z (the Front view looks from +Z) |
| "the side" | role side | +X (the Right view) |
| "the free end" / "the clamped end" | role free_end / fixed_end | flat end faces at max / min of the part's LONGEST axis |
| "the inlet" / "the outlet" | role inlet / outlet | min / max end of the longest axis |
| "the face at max X" | extreme x max | the same rule on a named axis |
| "the shoulder", "the top of the base" | extreme, index 1 | the next flat level inward |
| "the bolt holes" | holes | every round hole |
| "the hole at the far end" | holes + axis + side | the row of holes nearest that end |
| "the 10 mm holes" | holes + diameter_mm | holes of that size (±5%) |
| "the whole part" | role whole_body | the solid body |

The rules behind the table:

- **Only flat faces count** for directions and ends, and only if their
  outward normal points the way asked (within about 14 degrees). The
  outermost level wins, and every face on that level comes together:
  both flange tips of an I-beam are "the side", both feet of a bracket
  are "the bottom".
- **Nothing flat facing that way means a refusal**, with a list of what
  the part does have -- which directions its flat faces point and what
  holes it has. A rounded end (an eye, a lug, a hook) has no flat face:
  its hole or the live selection is the target there.
- **Ends follow the longest axis**, not Z. The old rule assumed Z, and on
  a control arm running along X "the free end" came back as one of its
  big flat sides. When two axes are within 10% the script says the end
  was a guess.
- A flat face more than 5% in from the part's extreme gets a warning:
  something curved or angled reaches further than the face chosen.

## How a hole is told from a fillet

A hole is a CONCAVE cylindrical face -- `FaceInSurfaceSense()` True,
material outside the cylinder -- whose faces close at least 300 degrees
round. SolidWorks often splits a hole into two half-cylinders, so faces
are grouped by shared axis and radius first. The inside of a fillet is
concave too but covers about 90 degrees; a boss or a rounded end is
convex.

| part | what the selectors found |
|---|---|
| aw_anchor_plate (sample) | 4 bolt holes of 10.31 mm, two per end of X; "top" = the 199.4 mm² top of the eye |
| aw_control_arm (sample, along X) | holes at min X = the 60 mm bore, at max X = the 14 mm eye (two half faces); "free end" refused, naming both holes |
| shaft (sample) | extreme max Z index 1 = the 131.7 mm² shoulder; "top" refused -- it is round |
| aw_hook (sample) | only front/back are flat; the 44 mm eye found as a hole |
| angle bracket (built) | 2 × 11 mm holes; "top" = the 600 mm² top of the upright; index 1 = the top of the base |

## A coordinate is a click

Selecting by a 3D point is a click at that point. The point must be
derived from the current geometry -- a literal selects a different face,
or none, the moment a dimension changes. `ViewZoomtofit2` first.

## Never select more than the step means

A whole body where a face was meant puts the load on every face. With a
total force that is not obviously wrong: the magnitude is right and the
direction is nonsense. The receipt's selection log records what each
step took -- the faces, their area, the holes and their centres -- and
it is the first thing to read when a result looks off.
