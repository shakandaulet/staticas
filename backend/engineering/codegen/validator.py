"""
The gate every script passes before it is offered or run.

WHAT THIS IS AND IS NOT
-----------------------
It is a static AST check. It stops a drifting model, a corrupted
download and an edit made between generation and execution. It is NOT a
sandbox and it does not defend against someone who controls the prompt
and wants to run code on this machine: `ast` cannot see through
`getattr(os, "sys" + "tem")` and does not try to.

The deployment answer to that is the one the server takes: bind to
localhost, and treat /run as a convenience for the machine SolidWorks
is already on rather than as a service.

WHY THE MESH CHECK FAILS CLOSED
-------------------------------
An earlier version of this check inspected only literal arguments, so
`CreateMesh(0, 50.0, 2.5)` was checked and
`CreateMesh(0, spec.max, spec.min)` was skipped -- which is the form
every script copying the template actually used. A generated script
slipped through and hung SolidWorks during meshing. Now a size argument
that cannot be resolved to a number BLOCKS, because "I could not check
this" and "this is fine" are not the same answer.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

from .. import config

BLOCKED_IMPORTS = {
    "subprocess", "shutil", "socket", "requests", "urllib", "urllib2",
    "httplib", "http", "ftplib", "telnetlib", "smtplib", "pickle",
    "ctypes", "importlib", "pty", "multiprocessing", "asyncio",
    "webbrowser", "tempfile",
}

BLOCKED_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint",
                 "input", "globals", "locals", "vars"}

BLOCKED_OS_ATTRS = {"system", "remove", "unlink", "rmdir", "removedirs",
                    "popen", "execv", "execve", "spawnv", "kill",
                    "startfile", "chmod", "rename", "replace", "truncate"}

# COM properties the model reliably calls as methods. Doing so raises a
# TypeError about a non-callable int, which reads like a bug in the
# calling code rather than two characters that should not be there.
COM_PROPERTIES = {
    "RunAnalysis": "study.RunAnalysis",
    "ForceEndEdit": "force.ForceEndEdit",
    "PressureEndEdit": "pressure.PressureEndEdit",
    "TemperatureEndEdit": "temp.TemperatureEndEdit",
    "ConvectionEndEdit": "conv.ConvectionEndEdit",
    "HeatPowerEndEdit": "hp.HeatPowerEndEdit",
    "HeatFluxEndEdit": "hf.HeatFluxEndEdit",
    "RadiationEndEdit": "rad.RadiationEndEdit",
    "GravityEndEdit": "grav.GravityEndEdit",
}


@dataclass
class Verdict:
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking

    def report(self) -> str:
        lines: list[str] = []
        if self.blocking:
            lines.append("BLOCKED:")
            lines += [f"  - {m}" for m in self.blocking]
        if self.warnings:
            lines.append("WARNINGS:")
            lines += [f"  - {m}" for m in self.warnings]
        if not lines:
            lines.append(f"Passed {len(self.checked)} checks.")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "blocking": self.blocking,
                "warnings": self.warnings, "report": self.report()}


def _collect_constants(tree: ast.AST) -> dict[str, float]:
    """Every name bound to a numeric constant, unqualified.

    Covers plain assignment, annotated assignment (including dataclass
    field defaults) and keyword defaults. Deliberately simple: it
    resolves the patterns the emitter and its imitations produce, and
    anything it cannot resolve is treated as unsafe by the caller rather
    than assumed fine."""
    consts: dict[str, float] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            if isinstance(node.value, ast.Constant) and \
                    isinstance(node.value.value, (int, float)) and \
                    not isinstance(node.value.value, bool):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        consts[t.id] = float(node.value.value)
        elif isinstance(node, ast.AnnAssign):
            if (node.value is not None
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, (int, float))
                    and isinstance(node.target, ast.Name)):
                consts[node.target.id] = float(node.value.value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            if args.defaults:
                for name, default in zip(args.args[-len(args.defaults):],
                                         args.defaults):
                    if isinstance(default, ast.Constant) and \
                            isinstance(default.value, (int, float)):
                        consts[name.arg] = float(default.value)
    return consts


def _resolve_number(arg: ast.AST, consts: dict[str, float]) -> tuple[float | None, str]:
    if isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)):
        return float(arg.value), "literal"
    if isinstance(arg, ast.UnaryOp) and isinstance(arg.op, ast.USub):
        inner, how = _resolve_number(arg.operand, consts)
        return (-inner if inner is not None else None), how
    if isinstance(arg, ast.Attribute):
        if arg.attr in consts:
            return consts[arg.attr], f"resolved from {arg.attr}"
        return None, f"attribute '{arg.attr}' has no constant default"
    if isinstance(arg, ast.Name):
        if arg.id in consts:
            return consts[arg.id], f"resolved from {arg.id}"
        return None, f"variable '{arg.id}' is not bound to a constant"
    return None, f"{type(arg).__name__} cannot be evaluated statically"


_MESH_CALLS = {"CreateMesh", "create_mesh"}
_FLOOR_NAMES = {"MESH_MAX_FLOOR_MM", "MESH_MIN_FLOOR_MM",
                "MESH_MAX_ELEMENT_FLOOR_MM", "MESH_MIN_ELEMENT_FLOOR_MM"}


def _guarded_wrappers(tree: ast.AST) -> dict[str, set[str]]:
    """Functions that re-check the mesh floor on their own arguments.

    The runtime prelude wraps CreateMesh in a helper that compares its
    arguments against the floor and raises before meshing. A static
    check cannot resolve that helper's parameters to numbers -- and
    should not have to, because the numbers arrive at the CALL SITE,
    which is checkable, and the helper itself refuses anything below the
    floor at run time.

    So the rule is: a call whose size arguments are the enclosing
    function's own parameters is accepted only if that function contains
    a comparison against the floor constants. Accepting it because it
    looks like a wrapper would let any function named create_mesh
    through; this verifies the guard is actually there."""
    guarded: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        has_guard = any(
            isinstance(sub, ast.Name) and sub.id in _FLOOR_NAMES
            for sub in ast.walk(node))
        raises = any(isinstance(sub, ast.Raise) for sub in ast.walk(node))
        if has_guard and raises:
            params = {a.arg for a in node.args.args}
            params |= {a.arg for a in node.args.kwonlyargs}
            guarded[node.name] = params
    return guarded


def _check_mesh(tree: ast.AST, consts: dict[str, float], v: Verdict) -> bool:
    """Every mesh call, checked against the floor. Fails closed.

    An earlier version inspected only literal arguments, so
    `CreateMesh(0, 50.0, 2.5)` was checked and
    `CreateMesh(0, spec.max, spec.min)` was skipped -- which is the form
    every script copying the template actually used. One slipped through
    and hung SolidWorks during meshing. A size that cannot be resolved
    now BLOCKS, because "I could not check this" and "this is fine" are
    different answers."""
    guarded = _guarded_wrappers(tree)
    floors = (config.MESH_MAX_ELEMENT_FLOOR_MM,
              config.MESH_MIN_ELEMENT_FLOOR_MM)
    labels = ("max element", "min element")
    found = False

    for fn_node in [None] + [n for n in ast.walk(tree)
                             if isinstance(n, (ast.FunctionDef,
                                               ast.AsyncFunctionDef))]:
        scope = fn_node if fn_node is not None else tree
        own_params = guarded.get(getattr(fn_node, "name", ""), set())

        for node in ast.walk(scope):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else \
                getattr(fn, "id", "")
            if name not in _MESH_CALLS:
                continue
            # Walking each function separately means a nested call is
            # seen twice; the outer pass is the one without parameters
            # in scope, so it is skipped rather than double-reported.
            if fn_node is None and _inside_any_function(tree, node):
                continue
            found = True

            for arg, label, floor in zip(node.args[1:3], labels, floors):
                if isinstance(arg, ast.Name) and arg.id in own_params:
                    continue        # checked at run time by the wrapper
                value, how = _resolve_number(arg, consts)
                if value is None:
                    v.blocking.append(
                        f"Cannot verify the {label} size ({how}). Mesh "
                        f"sizes must be checkable -- use a literal or a "
                        f"module-level constant.")
                elif value < floor:
                    v.blocking.append(
                        f"Mesh too fine: {label} {value} < {floor} mm. A "
                        f"fine mesh on a shared machine takes it out of "
                        f"service for everyone.")
    return found


def _inside_any_function(tree: ast.AST, target: ast.Call) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(sub is target for sub in ast.walk(node)):
                return True
    return False


def validate(code: str, domain: str = "static") -> Verdict:
    v = Verdict()

    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        v.blocking.append(f"Not valid Python: line {e.lineno}: {e.msg}")
        return v
    v.checked.append("parses")

    # ---------------- imports and calls ----------------
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BLOCKED_IMPORTS:
                    v.blocking.append(f"Blocked import: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            if root in BLOCKED_IMPORTS:
                v.blocking.append(f"Blocked import from: {node.module}")
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in BLOCKED_CALLS:
                v.blocking.append(f"Blocked call: {fn.id}()")
            elif isinstance(fn, ast.Attribute):
                owner = getattr(fn.value, "id", None)
                if owner == "os" and fn.attr in BLOCKED_OS_ATTRS:
                    v.blocking.append(f"Blocked call: os.{fn.attr}()")
                if fn.attr in {"rmtree", "move", "unlink"}:
                    v.blocking.append(f"Blocked filesystem call: .{fn.attr}()")
                # AST rather than regex: the knowledge base quotes
                # `study.RunAnalysis()` as a counter-example of what NOT
                # to write, and a regex over raw text flags that prose.
                if fn.attr in COM_PROPERTIES:
                    v.blocking.append(
                        f"{fn.attr} is called with parentheses. It is a COM "
                        f"property -- write `{COM_PROPERTIES[fn.attr]}` with "
                        f"no parens or pywin32 raises a TypeError.")
                if fn.attr == "SelectByID2":
                    if any(isinstance(a, ast.Constant) and a.value is None
                           for a in node.args):
                        v.blocking.append(
                            "SelectByID2 passes Python None. COM needs "
                            "VARIANT(pythoncom.VT_DISPATCH, None).")
    v.checked.append("imports and calls")

    # ---------------- mesh floor ----------------
    consts = _collect_constants(tree)
    found_mesh = _check_mesh(tree, consts, v)
    v.checked.append("mesh floor")

    # ---------------- structural completeness ----------------
    if domain in ("static", "nonlinear", "buckling", "fatigue"):
        if "add_restraint" not in code and "AddRestraint" not in code:
            v.blocking.append(
                "No restraint in a structural script. With nothing held the "
                "stiffness matrix is singular and the solve fails -- or "
                "worse, a near-singular restraint converges to nonsense.")
        if not found_mesh:
            v.warnings.append("No meshing step found; the solve will use "
                              "whatever mesh already exists on the study.")
    if domain == "thermal":
        sinks = ("add_convection", "add_radiation", "add_temperature")
        if not any(s in code for s in sinks):
            v.blocking.append(
                "A steady-state thermal script with no convection, "
                "radiation or fixed temperature has no way for heat to "
                "leave, and therefore no steady solution.")
    v.checked.append("domain completeness")

    # ---------------- study type verification is not removed ------------
    if "create_study(" in code and "AnalysisType" not in code:
        v.warnings.append(
            "The study-type read-back appears to have been removed. That "
            "check is what stops a wrong study-type constant from "
            "answering a different question without saying so.")

    # ---------------- writes ----------------
    if re.search(r"\bopen\s*\([^)]*['\"][wax]", code):
        v.warnings.append("The script opens a file for writing. Expected "
                          "for the run receipt; confirm nothing else.")
    v.checked.append("file writes")

    if "win32com" not in code:
        v.warnings.append("No win32com import -- is this script complete?")

    return v


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "generated.py"
    dom = sys.argv[2] if len(sys.argv) > 2 else "static"
    verdict = validate(open(path, encoding="utf-8").read(), dom)
    print(verdict.report())
    sys.exit(0 if verdict.ok else 1)
