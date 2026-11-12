"""The ops console: one static page, no build step. It loads without a token and then calls the
admin API with the token you type in (kept in sessionStorage, never in the page or the URL)."""

import secrets
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()
TEMPLATE = (Path(__file__).resolve().parent.parent / "static" / "console.html").read_text(encoding="utf-8")


@router.get("/console", include_in_schema=False)
async def console() -> HTMLResponse:
    # A fresh nonce per response: the page's own inline script and style may run, nothing else can.
    nonce = secrets.token_urlsafe(16)
    csp = (
        f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )
    headers = {"Content-Security-Policy": csp, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    return HTMLResponse(TEMPLATE.replace("__NONCE__", nonce), headers=headers)