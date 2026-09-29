# Copy to run.ps1 and put your key in it. run.ps1 is git-ignored;
# this example is not, so do not put a key in THIS file.

$env:GEMINI_API_KEY = "AQ.Ab8RN6JN-ZvTufS036NaUDW6k_cZsSEf4-_Xpfx8mchKsrHXzg"

# Optional overrides.
# $env:GEMINI_PLANNER_MODEL = "gemini-3.6-flash"
# $env:GEMINI_EMBED_MODEL   = "gemini-embedding-001"
# $env:RUN_TIMEOUT_S        = "900"

# Run as a module from the PARENT directory so the package imports
# resolve. Starting it as "python server.py" from inside engineering/
# breaks every "from .. import config".
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    python -m engineering.cli @args
} finally {
    Pop-Location
}
