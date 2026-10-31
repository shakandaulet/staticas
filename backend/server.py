"""
Statica backend, as Render starts it from this folder:

    python server.py            or            uvicorn server:app

Everything happens in the engineering package: Gemini fills a typed plan,
the plan is checked (units, missing data, physics), and a tested emitter
writes the SolidWorks script. The model never writes the script itself.
This file only starts that server under the name Render already uses.

Settings, all from the environment:
    GEMINI_API_KEY    required
    ALLOWED_ORIGINS   where the site is served from (comma separated)
    ACCESS_CODE       optional; when set, requests must carry it
"""

import os

# The site's own address, unless Render says otherwise.
os.environ.setdefault("ALLOWED_ORIGINS", "https://shakandaulet.github.io")

from engineering.server import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
