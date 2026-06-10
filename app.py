"""Top-level entrypoint for hosting the dashboard (e.g. Posit Connect Cloud).

Connect Cloud asks for a "primary file"; point it at this `app.py`, which
exposes the FastAPI application object `app`.

Locally you can run the same thing with:
    python -m uvicorn app:app --port 8000
"""
from web.app import app  # noqa: F401  (re-exported for the ASGI server)
