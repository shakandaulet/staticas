"""
The HTTP layer: the page sends a message, this sends back api.generate().

    uvicorn engineering.server:app --port 8000      (from the PARENT dir)

Deliberately thin. Every decision -- what to simulate, whether the plan
is complete, what the script says -- is made in api.py and the modules
behind it. This file only turns HTTP into a call and a failure into a
status code the page can explain.
"""

from __future__ import annotations

import hmac
import threading
import time
from collections import defaultdict, deque

from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from . import api, config

MAX_IMAGE_BYTES = 10 * 1024 * 1024

app = FastAPI(title="Statica", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Access-Code"],
)

_hits: dict[str, deque] = defaultdict(deque)


@app.on_event("startup")
def _warm_up() -> None:
    """Load the index while the page is still pinging /health, so the
    first plan does not also pay for embedding whatever changed."""
    def load():
        try:
            api.engine()
        except Exception as e:
            print(f"  [startup] {e}", flush=True)
    threading.Thread(target=load, daemon=True).start()


def _client(request: Request) -> str:
    # Render sits behind a proxy; the first forwarded address is the visitor.
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "?")


def _guard(request: Request, code: str) -> None:
    if config.ACCESS_CODE and not hmac.compare_digest(code, config.ACCESS_CODE):
        raise HTTPException(401, "Wrong or missing access code.")
    now = time.time()
    q = _hits[_client(request)]
    while q and now - q[0] > config.RATE_WINDOW_S:
        q.popleft()
    if len(q) >= config.RATE_LIMIT:
        raise HTTPException(429, f"Too many requests: {config.RATE_LIMIT} per "
                                 f"{config.RATE_WINDOW_S // 60} minutes. "
                                 f"Wait a little and try again.")
    q.append(now)


@app.get("/health")
def health() -> dict:
    return {**config.summary(), "access_code_required": bool(config.ACCESS_CODE)}


@app.get("/domains")
def domains() -> list[dict]:
    return api.domains()


@app.post("/generate")
async def generate(request: Request,
                   message: str = Form(""),
                   session_id: str = Form(""),
                   domain: str = Form(""),
                   force: bool = Form(False),
                   image: UploadFile | None = File(None),
                   x_access_code: str = Header("")) -> dict:
    _guard(request, x_access_code)

    data, mime = None, ""
    if image is not None and image.filename:
        mime = image.content_type or ""
        if not mime.startswith("image/"):
            raise HTTPException(400, "Only image files are accepted.")
        data = await image.read()
        if len(data) > MAX_IMAGE_BYTES:
            raise HTTPException(400, "Image is too large: 10 MB at most.")

    try:
        # Planning blocks on Gemini, including its own backoff; off the
        # event loop so one slow plan does not stall /health.
        return await run_in_threadpool(
            api.generate, message, session_id or None, data, mime,
            domain or None, force)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(503, str(e))
