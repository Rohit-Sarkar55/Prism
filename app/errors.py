"""One error type for every rejection, rendered as an OpenAI-style error body.

Each case has a distinct `type`, HTTP status, and request-log `status` so a rejection can be
told apart in the response, in the log, and in the console.
"""

from fastapi.responses import JSONResponse

from app.headers import prism_headers


class GatewayError(Exception):
    def __init__(self, status: int, type_: str, message: str, log_status: str):
        super().__init__(message)
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


def upstream_failed() -> GatewayError:
    # Deliberately generic: upstream messages can describe our credentials or infrastructure.
    return GatewayError(502, "upstream_error", "The upstream provider failed to serve this request", "upstream_error")


def error_response(err: GatewayError, request_id: str | None = None) -> JSONResponse:
    body = {"error": {"message": err.message, "type": err.type, "code": err.type}}
    return JSONResponse(body, status_code=err.status, headers=prism_headers(request_id=request_id))