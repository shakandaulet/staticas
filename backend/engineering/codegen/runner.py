"""
Running a generated script, and picking up what it left behind.

THE RISK, STATED PLAINLY
------------------------
This executes a Python file on the machine the server is running on.
The file is re-validated here, in this process, immediately before it
runs -- the caller's copy is never trusted, because it arrived over
HTTP and could have been edited in between. But `validator` is a static
check, not a sandbox.

So the deployment rule is not optional: the server binds to 127.0.0.1,
and /run exists because the machine with SolidWorks on it is the same
machine. Anyone who can reach this endpoint can already run code on
this box by easier means; anyone who cannot reach it is not this
endpoint's problem. Exposing it to a network changes both of those
sentences.

THE TIMEOUT IS NOT A FORMALITY
------------------------------
A bad mesh hangs SolidWorks indefinitely. Without a deadline the first
student to submit an awkward part takes the lab PC out of service until
someone walks over to it.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .. import config
from .validator import validate


@dataclass
class RunResult:
    ok: bool
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""
    receipt: dict | None = None
    error: str = ""
    receipt_path: str = ""
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok, "returncode": self.returncode,
            "stdout": self.stdout[-6000:], "stderr": self.stderr[-4000:],
            "receipt": self.receipt, "error": self.error,
            "receipt_path": self.receipt_path, "warnings": self.warnings,
        }


def run_script(code: str, domain: str = "static") -> RunResult:
    """Validate, write, execute, collect."""
    verdict = validate(code, domain)
    if not verdict.ok:
        return RunResult(
            ok=False,
            error="This script did not pass validation here, so it was not "
                  "run.\n" + verdict.report())

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    script = config.PKG_ROOT / f"_run_{stamp}.py"
    script.write_text(code, encoding="utf-8")

    before = {p.name for p in config.RUNS_DIR.glob("run_*.json")}

    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=config.RUN_TIMEOUT_S, cwd=str(config.PKG_ROOT))
    except subprocess.TimeoutExpired as e:
        return RunResult(
            ok=False,
            error=(f"The run did not finish within {config.RUN_TIMEOUT_S}s "
                   f"and was stopped. SolidWorks may still be meshing -- "
                   f"check it, and try a coarser mesh or a simpler part."),
            stdout=(e.stdout or b"").decode("utf-8", "replace")
            if isinstance(e.stdout, bytes) else (e.stdout or ""))
    finally:
        script.unlink(missing_ok=True)

    receipt, receipt_path = _latest_receipt(before)

    if proc.returncode != 0:
        return RunResult(
            ok=False, returncode=proc.returncode,
            stdout=proc.stdout, stderr=proc.stderr, receipt=receipt,
            receipt_path=receipt_path,
            error=_explain_failure(proc.stderr, receipt))

    return RunResult(ok=True, returncode=0, stdout=proc.stdout,
                     stderr=proc.stderr, receipt=receipt,
                     receipt_path=receipt_path,
                     warnings=(receipt or {}).get("warnings", []))


def _latest_receipt(before: set[str]) -> tuple[dict | None, str]:
    """The receipt this run wrote, if it wrote one.

    A failed run writes one too, and that is the more informative case:
    it carries how far the script got and what was selected before it
    stopped."""
    fresh = [p for p in config.RUNS_DIR.glob("run_*.json")
             if p.name not in before]
    if not fresh:
        return None, ""
    newest = max(fresh, key=lambda p: p.stat().st_mtime)
    try:
        return json.loads(newest.read_text(encoding="utf-8")), str(newest)
    except json.JSONDecodeError:
        return None, str(newest)


def _explain_failure(stderr: str, receipt: dict | None) -> str:
    """Turn a traceback into the sentence the user needs.

    The script already raises with readable messages; this catches the
    cases where the failure happened outside them -- a missing package,
    a COM error from a call the runtime did not wrap."""
    if receipt and receipt.get("error"):
        return receipt["error"]

    tail = (stderr or "").strip().splitlines()
    last = tail[-1] if tail else ""

    if "No module named 'win32com'" in stderr or "No module named 'pythoncom'" in stderr:
        return ("pywin32 is not installed for this Python. Install it with "
                "`pip install pywin32` on the machine running SolidWorks.")
    if "CO_E_CLASSSTRING" in stderr or "Invalid class string" in stderr:
        return ("SolidWorks is not installed or not registered on this "
                "machine, so the COM class could not be found.")
    if "-2147221021" in stderr or "Operation unavailable" in stderr:
        return ("SolidWorks is not running. Start it, open the part, and "
                "try again.")
    if "RPC_E_" in stderr or "call was rejected" in stderr.lower():
        return ("SolidWorks rejected the call because it was busy -- usually "
                "a dialog is open in the SolidWorks window waiting for an "
                "answer. Dismiss it and re-run.")
    return last or "The script failed without a readable message."


def find_receipts(limit: int = 20) -> list[dict]:
    """Recent runs, newest first -- what the panel's history shows."""
    out = []
    paths = sorted(config.RUNS_DIR.glob("run_*.json"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    for p in paths:
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        out.append({
            "file": p.name,
            "run_at": data.get("run_at", ""),
            "domain": data.get("domain", ""),
            "summary": data.get("summary", ""),
            "results": data.get("results", {}),
            "error": data.get("error", ""),
            "warnings": data.get("warnings", []),
        })
    return out


def load_receipt(name: str) -> dict | None:
    """Read one receipt by file name, refusing anything outside runs/.

    The name arrives from an HTTP request, so it is resolved and checked
    against the runs directory rather than trusted -- `../../` in a
    filename is the oldest trick there is."""
    candidate = (config.RUNS_DIR / Path(name).name).resolve()
    if candidate.parent != config.RUNS_DIR.resolve() or not candidate.exists():
        return None
    try:
        return json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
