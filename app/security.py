import logging

from fastapi import Header, HTTPException, Request, status

from app.config import get_settings


logger = logging.getLogger("chm_api")


def require_api_key(request: Request, x_api_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not settings.require_api_key:
        return
    configured_key = settings.api_key.strip()
    provided_key = (x_api_key or "").strip()

    if not configured_key:
        logger.warning(
            "api_key_missing method=%s path=%s has_x_api_key=%s",
            request.method,
            request.url.path,
            bool(provided_key),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Server API key is not configured. Set API_KEY or CANOPY_API_KEY and restart FastAPI.",
        )
    if provided_key != configured_key:
        logger.warning("api_key_mismatch method=%s path=%s", request.method, request.url.path)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")
