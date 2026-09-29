"""
Central configuration for the engineering assistant.

One place for every path, model name and tunable, because the previous
generation of this project resolved paths relative to the current
working directory and broke the moment the server was started from
anywhere but the repo root.

Nothing here reads a secret from source. GEMINI_API_KEY comes from the
environment and only from the environment -- a key written into a file
that gets committed is a key that has to be rotated.
"""

from __future__ import annotations

import os
from pathlib import Path

# --------------------------------------------------------------------------
# Paths -- all anchored to THIS file, never to os.getcwd()
# --------------------------------------------------------------------------
PKG_ROOT = Path(__file__).resolve().parent
KNOWLEDGE_DIR = PKG_ROOT / "knowledge"
TEMPLATES_DIR = PKG_ROOT / "templates"
INDEX_DIR = PKG_ROOT / ".index"
RUNS_DIR = PKG_ROOT / "runs"
SESSIONS_DIR = PKG_ROOT / ".sessions"
EXAMPLES_PATH = INDEX_DIR / "verified_examples.jsonl"

for _d in (INDEX_DIR, RUNS_DIR, SESSIONS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------
_RAW_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

# run.ps1 ships with a placeholder in it. Someone who starts the server
# before editing it should be told the key is missing, rather than get
# an authentication error from Google, which reads like a problem with
# their account.
_PLACEHOLDERS = {"paste-your-key-here", "your-key", "your-key-here",
                 "changeme", "xxx", "todo"}
API_KEY = "" if _RAW_KEY.lower() in _PLACEHOLDERS else _RAW_KEY

# There is no offline mode: every plan comes from the model, so the
# server stops at startup with this message instead of running without
# one.
NO_KEY_HELP = (
    "GEMINI_API_KEY is not set. The assistant plans every simulation with "
    "Gemini and has no offline mode.\n"
    "Put your key in run.ps1 and start the server again."
)

# Model names move faster than this project will. Overridable, and probed
# once at startup so a wrong name fails on the console instead of thirty
# seconds into a student's first message.
PLANNER_MODEL = os.environ.get("GEMINI_PLANNER_MODEL", "gemini-3.6-flash").strip()

# The planner turns free text into a typed plan and needs to be good at
# structured output. The explainer only writes prose. They are separate
# settings so the expensive model can be used for exactly one of them.
EXPLAINER_MODEL = os.environ.get("GEMINI_EXPLAINER_MODEL", PLANNER_MODEL).strip()

# Only used for the rare operation the deterministic emitter cannot cover.
CODEGEN_MODEL = os.environ.get("GEMINI_CODEGEN_MODEL", PLANNER_MODEL).strip()

EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001").strip()

FALLBACK_MODELS = (
    "gemini-3.6-flash",
    "gemini-flash-latest",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
)

# --------------------------------------------------------------------------
# Retrieval
# --------------------------------------------------------------------------
# Dense + sparse are fused; these are the per-arm depths before fusion.
DENSE_K = int(os.environ.get("RAG_DENSE_K", "12"))
SPARSE_K = int(os.environ.get("RAG_SPARSE_K", "12"))
# How many chunks actually reach the prompt after fusion and MMR.
FINAL_K = int(os.environ.get("RAG_FINAL_K", "8"))
# 0 = pure relevance, 1 = pure diversity. 0.3 keeps the top hit and stops
# the next five slots from being near-duplicates of it.
MMR_LAMBDA = float(os.environ.get("RAG_MMR_LAMBDA", "0.3"))

# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------
# A bad mesh can hang SolidWorks indefinitely. An unbounded wait strands
# whatever machine this is running on.
RUN_TIMEOUT_S = int(os.environ.get("RUN_TIMEOUT_S", "900"))

# The mesh floor is a project rule, not a suggestion: coarse meshes keep
# lab PCs usable. The validator enforces these and fails closed.
MESH_MAX_ELEMENT_FLOOR_MM = float(os.environ.get("MESH_MAX_FLOOR_MM", "25.0"))
MESH_MIN_ELEMENT_FLOOR_MM = float(os.environ.get("MESH_MIN_FLOOR_MM", "1.0"))

# Flow Simulation meshes are counted in cells, not millimetres.
FLOW_MAX_CELLS = int(os.environ.get("FLOW_MAX_CELLS", "300000"))

ALLOWED_ORIGINS = [
    "http://localhost:8000", "http://127.0.0.1:8000",
    "http://localhost:8010", "http://127.0.0.1:8010",
    "http://localhost:5500", "http://127.0.0.1:5500",
    "null",
]

# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------
# The Gemini Developer API is not available in every country, and the
# same key works through a VPN and fails without it. Retrying cannot
# help, so the raw 400 is translated into something actionable.
LOCATION_MARKERS = ("User location is not supported", "FAILED_PRECONDITION")

LOCATION_HELP = (
    "Gemini refused the request because of the server's location: the "
    "Gemini Developer API is not offered in every country. Nothing is "
    "wrong with your key or with this project.\n"
    "Options: run this server behind a VPN terminating in a supported "
    "country, or switch to Vertex AI, which has its own regional rules."
)

TRANSIENT_MARKERS = ("503", "UNAVAILABLE", "429", "RESOURCE_EXHAUSTED",
                     "high demand", "overloaded")
API_RETRIES = 3


def summary() -> dict:
    """What the /health endpoint reports. Never includes the key itself."""
    return {
        "planner_model": PLANNER_MODEL,
        "embed_model": EMBED_MODEL,
        "api_key_set": bool(API_KEY),
        "knowledge_dir": str(KNOWLEDGE_DIR),
        "mesh_floor_mm": [MESH_MAX_ELEMENT_FLOOR_MM, MESH_MIN_ELEMENT_FLOOR_MM],
        "run_timeout_s": RUN_TIMEOUT_S,
    }
