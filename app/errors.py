"""One error type for every rejection, rendered as an OpenAI-style error body.

Each case has a distinct `type`, HTTP status, and request-log `status` so a rejection can be
told apart in the response, in the log, and in the console.
"""

import math

from fastapi.responses import JSONResponse

from app.headers import prism_headers
from app.money import format_usd


class GatewayError(Exception):
    def __init__(self, status: int, type_: str, message: str, log_status: str, headers: dict[str, str] | None = None):
        super().__init__(message)
        self.headers = headers or {}
        self.retries = 0  # upstream attempts made before giving up (only set for upstream failures)
        self.status = status
        self.type = type_
        self.message = message
        self.log_status = log_status


def _clip(text: str) -> str:
    return text[:80]


def authentication_error() -> GatewayError:
    return GatewayError(401, "authentication_error", "Missing or invalid virtual key", "rejected_auth")


def invalid_request(message: str) -> GatewayError:
    return GatewayError(400, "invalid_request_error", message, "rejected_invalid")


def not_found(model: str) -> GatewayError:
    return GatewayError(404, "not_found_error", f"Unknown model or alias '{_clip(model)}'", "rejected_not_found")


def model_not_allowed(model: str) -> GatewayError:
    return GatewayError(
        403, "model_not_allowed", f"Model '{_clip(model)}' is not allowed for this key", "rejected_allowlist"
    )


def rate_limited(limit: int, retry_after_s: float) -> GatewayError:
    wait = max(1, math.ceil(retry_after_s))
    return GatewayError(
        429,
        "rate_limit_exceeded",
        f"Rate limit of {limit} requests per minute exceeded for this key; retry in {wait}s",
        "rejected_rate_limit",
        headers={"Retry-After": str(wait)},
    )


def budget_exhausted(budget_nano: int) -> GatewayError:
    # 402, not 429: SDKs retry 429 automatically, and an exhausted budget will not recover by retrying.
    return GatewayError(
        402,
        "budget_exceeded",
        f"Monthly budget of ${format_usd(budget_nano)} exhausted for this key",
        "rejected_budget",
    )


def upstream_failed(retries: int = 0) -> GatewayError:
    # Deliberately generic: upstream messages can describe our credentials or infrastructure.
    err = GatewayError(502, "upstream_error", "The upstream provider failed to serve this request", "upstream_error")
    err.retries = retries
    return err


def error_response(err: GatewayError, request_id: str | None = None) -> JSONResponse:
    body = {"error": {"message": err.message, "type": err.type, "code": err.type}}
    headers = prism_headers(request_id=request_id)
    headers.update(err.headers)
    return JSONResponse(body, status_code=err.status, headers=headers)