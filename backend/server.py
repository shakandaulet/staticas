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
import shutil
from pathlib import Path

# The site's own address, unless Render says otherwise.
os.environ.setdefault("ALLOWED_ORIGINS", "https://shakandaulet.github.io")

# The prebuilt knowledge-base index. GitHub's web upload cannot create a
# folder whose name starts with a dot, so the files sit at the repository
# root and are copied into engineering/.index before the engine loads.
# Without them every start re-embeds the whole corpus: two minutes and a
# share of the free Gemini quota, after each time Render wakes up.
_here = Path(__file__).resolve().parent
_index = _here / "engineering" / ".index"
_index.mkdir(parents=True, exist_ok=True)
for _name in ("chunks.json", "vectors.npz", "verified_examples.jsonl"):
    _src = _here.parent / _name
    if _src.exists() and not (_index / _name).exists():
        shutil.copy2(_src, _index / _name)

from engineering.server import app  # noqa: E402

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
