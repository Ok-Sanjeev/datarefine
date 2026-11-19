"""
Security Utilities and Middleware for DataRefine.

Provides:
1. Secret masking and sanitization to prevent API keys from leaking in logs and error responses.
2. Production-grade CORS configuration and validation.
3. Security response headers middleware (CSP, nosniff, DENY, HSTS).
"""

import os
import re
from typing import Any, List, Optional, Tuple
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

# Patterns matching sensitive API keys and tokens
SECRET_PATTERNS = [
    # Google Gemini / Cloud API keys
    (re.compile(r"AIza[0-9A-Za-z\-_]{35}"), "[REDACTED_GEMINI_KEY]"),
    # Anthropic Claude API keys (must precede generic sk- pattern)
    (re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}"), "[REDACTED_ANTHROPIC_KEY]"),
    # OpenAI standard API keys
    (re.compile(r"sk-[A-Za-z0-9\-_]{20,}"), "[REDACTED_OPENAI_KEY]"),
    # Bearer tokens in headers or strings
    (re.compile(r"Bearer\s+[A-Za-z0-9\-_\.]{16,}", re.IGNORECASE), "Bearer [REDACTED_TOKEN]"),
    # DataRefine authentication tokens
    (re.compile(r"dref_[A-Za-z0-9\-_]{20,}"), "[REDACTED_AUTH_TOKEN]"),
    # Fernet encrypted ciphertext
    (re.compile(r"gAAAAA[A-Za-z0-9\-_=]{30,}"), "[REDACTED_ENCRYPTED_SECRET]"),
    # Query parameters containing keys: ?key=... or &api_key=...
    (re.compile(r"(?:[?&](?:api_)?key=)[A-Za-z0-9\-_\.]{10,}", re.IGNORECASE), "?key=[REDACTED]"),
]


def sanitize_secret_strings(text: Any) -> str:
    """
    Sanitize text by redacting any discovered API keys, tokens, or credential signatures.
    """
    if text is None:
        return ""
    sanitized = str(text)
    for pattern, replacement in SECRET_PATTERNS:
        sanitized = pattern.sub(replacement, sanitized)
    return sanitized


def validate_cors_origins(
    origins_str: Optional[str] = None,
    environment: Optional[str] = None,
) -> Tuple[List[str], bool]:
    """
    Validate and return safe CORS origins based on the operational environment.

    In production:
    - Wildcard '*' is strictly prohibited.
    - Origins must be explicit, valid URLs with http:// or https:// schemes.
    - Credentials are permitted for explicitly verified origins.

    In development:
    - Permissive origins are allowed for rapid testing.
    """
    env = (environment or os.getenv("ENVIRONMENT", "development")).strip().lower()
    raw_origins = origins_str if origins_str is not None else os.getenv("ALLOWED_ORIGINS", "")

    if env in ("production", "prod"):
        if not raw_origins or not raw_origins.strip():
            raise ValueError(
                "In production (ENVIRONMENT=production), ALLOWED_ORIGINS must be explicitly configured "
                "with trusted domain URLs (e.g. 'https://datarefine.example.com')."
            )

        candidate_list = [orig.strip() for orig in raw_origins.split(",") if orig.strip()]
        if "*" in candidate_list:
            raise ValueError(
                "Security violation: Wildcard origin '*' is strictly prohibited in production. "
                "Specify explicit domain origins in ALLOWED_ORIGINS."
            )

        validated_origins: List[str] = []
        for origin in candidate_list:
            parsed = urlparse(origin)
            if not parsed.scheme or parsed.scheme not in ("http", "https"):
                raise ValueError(
                    f"Invalid origin '{origin}' in ALLOWED_ORIGINS: Must specify http:// or https:// scheme."
                )
            if not parsed.netloc:
                raise ValueError(
                    f"Invalid origin '{origin}' in ALLOWED_ORIGINS: Missing valid domain / host."
                )
            # Remove trailing slash for standard CORS matching
            clean_origin = f"{parsed.scheme}://{parsed.netloc}"
            validated_origins.append(clean_origin)

        return validated_origins, True

    else:
        # Development / Testing environment
        if not raw_origins or raw_origins.strip() == "*":
            # Permissive default for local development
            return ["*"], False

        candidate_list = [orig.strip() for orig in raw_origins.split(",") if orig.strip()]
        allow_creds = "*" not in candidate_list
        return candidate_list, allow_creds


# Standard Content-Security-Policy preserving frontend UI features
DEFAULT_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.tailwindcss.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob:; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self';"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    FastAPI / Starlette middleware that adds essential security headers to every response.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)

        # 1. Content Security Policy
        response.headers["Content-Security-Policy"] = DEFAULT_CSP

        # 2. Prevent MIME sniffing
        response.headers["X-Content-Type-Options"] = "nosniff"

        # 3. Prevent Clickjacking
        response.headers["X-Frame-Options"] = "DENY"

        # 4. Strict Referrer Policy
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # 5. Restrict dangerous browser features
        response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"

        # 6. HTTP Strict Transport Security (HSTS) when on HTTPS
        is_https = (
            request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").lower() == "https"
        )
        if is_https:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        # 7. Strip server fingerprinting headers (L2)
        for fingerprint_header in (
            "server",
            "Server",
            "x-render-origin-server",
            "X-Render-Origin-Server",
            "x-powered-by",
            "X-Powered-By",
        ):
            if fingerprint_header in response.headers:
                del response.headers[fingerprint_header]

        return response
