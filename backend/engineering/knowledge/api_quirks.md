---
kind: quirk
domains: []
confidence: verified
---

# SolidWorks COM quirks that generated code gets wrong

Every entry here is a failure that produces a confusing error on a
student's machine, or -- worse -- no error at all. They are pinned into
every prompt rather than retrieved, because a plan needs the complete
list, not the entries that happen to share vocabulary with the request.

## VARIANT null, not Python None

Any COM parameter documented as taking `Nothing` or an optional
dispatch object needs an explicit VARIANT. Python's bare `None` raises
a COM type error that names neither the argument nor the call.

```python
import win32com.client, pythoncom
NOTHING = win32com.client.VARIANT(pythoncom.VT_DISPATCH, None)
swModel.Extension.SelectByID2("Front Plane", "PLANE", 0, 0, 0,
                              False, 0, NOTHING, 0)
```

The same object is reused everywhere a null is needed. Constructing a
fresh one per call also works and is just noise.

## Byref error codes need a VARIANT too

Simulation calls return their real error through a byref `long`. Pass a
`VT_BYREF | VT_I4` VARIANT and read `.value` afterwards. Passing `0`
means the call still runs but the error code is discarded, so a failure
looks identical to a success until something downstream is None.

```python
err = win32com.client.VARIANT(pythoncom.VT_BYREF | pythoncom.VT_I4, 0)
study = mgr.CreateNewStudy3(name, study_type, 0, err)
if not study:
    raise RuntimeError(f"CreateNewStudy3 failed, code {err.value}")
```

## RunAnalysis and the *EndEdit members: parentheses or not

In the API reference these are methods. Through pywin32 they behave
like properties on some builds: with no type information for the
object, touching the attribute invokes it and returns the result, so
`cwStudy.RunAnalysis()` then tries to call an int and raises a
TypeError about a non-callable int. Where pywin32 does have type
information the opposite holds -- `cwStudy.RunAnalysis` on its own
returns a bound method that is never called, the study is never solved,
and nothing says so.

Neither spelling is safe on both, so read the member and call it only
if what came back is callable:

```python
def member(obj, name):
    got = getattr(obj, name)
    return got() if callable(got) else got

result = member(cwStudy, "RunAnalysis")
member(cwForce, "ForceEndEdit")
```

## A COM object is callable, so `callable()` is not a type test

pywin32 gives every returned COM object a `__call__` that forwards to
its default member. Code that decides whether to call a member with
`callable(x)` therefore calls the face, body or feature it has just
fetched, and gets "Member not found" from something that was fine:

```python
def member(obj, name):
    got = getattr(obj, name)
    if hasattr(got, "_oleobj_"):
        return got            # a COM object: already invoked on access
    return got() if callable(got) else got
```

## Reading results: the arguments are not optional, and the max is index 3

Confirmed against runs on SOLIDWORKS 2024.

- `GetMinMaxStress(component, element, step, refgeom, unit, errcode)` has
  six arguments; `GetMinMaxDisplacement(component, step, refgeom, unit,
  errcode)` has five. The two do not take the same list.
- Both return `{node_of_min, min, node_of_max, max}`, so the **maximum
  is index 3**. Index 1 is the minimum — zero displacement at the
  restraint, reported as the peak deflection.
- Von Mises is component **9**. 8 is the third principal stress: a
  different number that looks just as plausible in a receipt.
- Temperature comes from `GetMinMaxThermal`. `GetMinMaxTemperature` does
  not exist.
- `GetResonantFrequencies(errcode)` returns groups of four — mode,
  rad/s, Hz, period. `GetBucklingLoadFactors(errcode)` returns **pairs**
  — mode, factor. Read as a flat list it gives 1.0, which reads as a
  part on the point of collapse and is really just the mode number.
- In a study with more than one solution step, step 1 is the FIRST LOAD
  INCREMENT. Ask `GetMaximumAvailableSteps` and read the last: on a
  nonlinear beam that was 0.27 MPa at step 1 against 123 MPa at step 100.

## Face geometry: GetBox, GetArea, Normal -- not GetMassProperties

`IFace2::GetMassProperties` is missing on some builds — it is absent on
the 2024 install this was tested against, where it fails with a bare
AttributeError naming nothing useful. What is there, confirmed:

- `GetBox` -> (x1, y1, z1, x2, y2, z2) in metres, and `GetArea`.
- `face.Normal` on a PLANAR face is the OUTWARD normal: it already
  allows for the face's sense. The underside of a plate reads -Y.
- `GetSurface().CylinderParams` -> (origin x, y, z, axis x, y, z,
  radius). `face.FaceInSurfaceSense()` is True on a hole or the inside
  of a fillet, False on a boss or a rounded end.
- `body.GetBodyBox()` -> the part's extent, same layout as GetBox.

Do not rank faces by the centre of their box to find "the top": the
curved side of a round shaft ties with its flat ends and wins.

## Sketches: take the feature from the tree, and switch inference off

- `"Sketch1"` is a localised name ("Эскиз1") that also counts up with
  every sketch. After `InsertSketch2(True)` closes a sketch,
  `model.FeatureByPositionReverse(0)` is that sketch; select it by the
  `.Name` it reports.
- `SketchManager.AddToDB = True` while drawing, False after. Without it
  a line drawn near an existing point snaps onto it, and an outline
  comes out a different shape that still extrudes.
- `CreateArc(xc, yc, zc, x1, y1, z1, x2, y2, z2, direction)`: direction
  -1 is clockwise, +1 counter-clockwise.
- Extrusion goes along the plane normal: +Z from the Front plane, +Y
  from the Top plane. The Top plane's sketch Y runs along -Z.

## Measured traps in loads, restraints and meshes

Each of these solved without complaint and gave a wrong answer until a
check caught it.

- **Mesh quality: 0 is DRAFT, 1 is HIGH** (swsMeshQuality_e). Swapped,
  every "draft" run was second-order. Draft is ~10% stiff in bending.
- **A force's value goes on EACH selected face.** 1000 N on two faces is
  2000 N; one hole split into two half-cylinders doubles its load. The
  runtime divides the total by the face count before writing it.
- **A positive normal force pushes into the face.**
- **Immovable is for shells and beams**: on a solid AddRestraint fails
  with error 15; fixed is the same restraint there.
- **Radiation needs `ViewFactor` set** (1 for open surroundings); left at
  the default the solve failed with run error 24.
- **Material properties come from the .sldmat file** (UTF-16 XML, SI:
  EX, NUXY, DENS, SIGYLD, KX...). `ICWMaterial.GetPropertyByName` fails
  with a type mismatch, and afterwards every access to the body's
  material throws inside SolidWorks until it is restarted.
- **Results that come back as flat lists**: GetNodes is 4 a node (id,
  x, y, z); GetStress(0, step, ...) is 12 a node (id, then SX SY SZ TXY
  TXZ TYZ P1 P2 P3 VON INT); GetReactionForcesAndMomentsWithSelections
  fills two byref VARIANT out-arguments, the first starting Fx, Fy, Fz,
  |F| for the selection.

## A directional force leaves the Front plane selected

Stating a force along an axis selects a reference plane, and that
replaces whatever the user had selected. Read the user's selection
before anything else runs; a target read after that step gets the
plane, not the face.

## Return types vary by pywin32 build

`GetMaterialPropertyName2` and several siblings return either a bare
string or a tuple depending on the installed pywin32. Code that indexes
into the result works on one machine and raises on the next.

```python
got = body.GetMaterialPropertyName2("", "")
name = got[1] if isinstance(got, tuple) else got
```

## SelectByID2 by coordinate is a click, not a lookup

Selecting a face by a 3D point is exactly equivalent to clicking there
in the graphics area. Two consequences that break generated code:

- The point must lie ON the target face, so it has to be DERIVED from
  the current geometry. A hardcoded coordinate silently selects the
  wrong face -- or nothing -- the moment a dimension changes, and the
  study then runs with the load somewhere else entirely.
- Selection by coordinate can fail for a face that is facing away from
  the current view orientation. Call `ViewZoomtofit2` and, where it
  matters, orient the view before selecting.

Sketch entities have no stable persisted names, so name-based selection
is unreliable for them; coordinates are the robust option there. For
planes, features and bodies the reverse is true -- use the name.

## Selection returns nothing without a check

`GetSelectedObject6` returns None when nothing matched, and every
downstream call then fails with an unrelated message. Check immediately
and say which target failed:

```python
face = swSelMgr.GetSelectedObject6(1, -1)
if not face:
    raise RuntimeError("Nothing selected for the fixture -- the "
                       "coordinate did not land on a face.")
```

## The material database path should be given explicitly

`SetLibraryMaterial` takes a path to a `.sldmat` file. Relying on the
localized default can fail with an encoding error on a non-English
Windows install. Point at the English library explicitly:

```
C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS\lang\english\sldmaterials\solidworks materials.sldmat
```

## The Simulation add-in has three ProgIDs

Which one exists depends on the installed edition and version. Try them
in order and fail with a message that names the add-in rather than a
COM error:

```python
for prog_id in ("SldWorks.Simulation",
                "CosmosWorks.CosmosWorks",
                "SldWorks.Simulation.1"):
    addin = swApp.GetAddInObject(prog_id)
    if addin:
        break
```

`addin.CosmosWorks` is the application object. It can be None even when
the add-in resolved, which means Simulation is installed but not
enabled for this session.

## Units are metres and newtons, not document units

The API works in SI regardless of what the document's unit system is
set to. A part displayed in millimetres still takes `0.05` for 50 mm
through `CreateCornerRectangle`. This is the most common source of a
model that comes out a thousand times too big and still meshes.

Mesh element sizes are the exception worth checking on your own
install: `CreateMesh` size arguments have been observed following the
document unit system rather than metres, which is why the mesh floor in
this project is expressed in millimetres and re-checked after meshing.
