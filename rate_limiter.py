"""
Rate Limiting and Abuse Prevention for DataRefine.

Provides sliding-window in-memory IP rate limiting for:
1. General API traffic (DoS mitigation)
2. AI Planning endpoints (/api/plan)
3. Server-side Gemini API quota protection
"""

import os
import re
import time
from collections import defaultdict
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException, Request


class IPRateLimiter:
    """
    Thread-safe in-memory sliding-window rate limiter keyed by client IP.
    """

    def __init__(self):
        self._lock = Lock()
        # Storage format: { (client_ip, category): [timestamp1, timestamp2, ...] }
        self._records: Dict[Tuple[str, str], List[float]] = defaultdict(list)

    def get_client_ip(self, request: Request) -> str:
        """
        Extract client IP address, safely inspecting reverse proxy headers.
        Prefers the first IP in X-Forwarded-For when deployed behind trusted proxies.
        """
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # First IP in the comma-separated chain is the original client IP
            client_ip = forwarded.split(",")[0].strip()
            if client_ip:
                return client_ip

        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()

        if request.client and request.client.host:
            return request.client.host

        return "127.0.0.1"

    def is_allowed(
        self, client_ip: str, category: str, max_requests: int, window_seconds: int
    ) -> Tuple[bool, int]:
        """
        Check if request is permitted under sliding window.
        Returns: (is_allowed, retry_after_seconds)
        """
        if max_requests <= 0:
            # Non-positive limit implies disabled or forbidden
            return False, window_seconds

        now = time.time()
        window_start = now - window_seconds
        key = (client_ip, category)

        with self._lock:
            # Filter out timestamps outside the active window
            timestamps = [ts for ts in self._records[key] if ts > window_start]
            self._records[key] = timestamps

            if len(timestamps) >= max_requests:
                # Oldest timestamp determines when the earliest slot frees up
                oldest_in_window = timestamps[0]
                retry_after = max(1, int(oldest_in_window + window_seconds - now))
                return False, retry_after

            # Record this valid request
            timestamps.append(now)
            return True, 0

    def reset(self) -> None:
        """Clear all rate limiting records (useful for automated test teardowns)."""
        with self._lock:
            self._records.clear()


# Global Singleton Rate Limiter
limiter = IPRateLimiter()


def get_rate_limit_configs() -> Dict[str, Any]:
    """Load configurable rate limits from environment variables."""
    env = os.getenv("ENVIRONMENT", "development").strip().lower()
    default_fallback = "false" if env in ("production", "prod") else "true"
    return {
        "global_per_minute": int(os.getenv("GLOBAL_RATE_LIMIT_PER_MINUTE", "120")),
        "ai_per_minute": int(os.getenv("AI_RATE_LIMIT_PER_MINUTE", "10")),
        "ai_server_key_per_hour": int(os.getenv("AI_SERVER_KEY_LIMIT_PER_HOUR", "20")),
        "allow_server_gemini_fallback": os.getenv("ALLOW_SERVER_GEMINI_FALLBACK", default_fallback).lower() in ("true", "1", "yes"),
    }


def enforce_ai_rate_limits(request: Request, has_custom_key: bool = False) -> None:
    """
    Enforce multi-tier rate limiting on AI planning requests.
    - If user supplies their own API key: subject to AI per-minute burst limit only.
    - If user relies on server key: subject to per-minute limit AND hourly server-key quota.
    - If server fallback is disabled and no key is provided: 403 Forbidden.
    """
    configs = get_rate_limit_configs()
    client_ip = limiter.get_client_ip(request)

    # 1. Check if server fallback is prohibited when using server key
    if not has_custom_key:
        if not configs["allow_server_gemini_fallback"]:
            raise HTTPException(
                status_code=403,
                detail=(
                    "Server-side AI key fallback is disabled in this environment. "
                    "Please provide your own personal Gemini API key in the Settings drawer."
                ),
            )

        # 2. Check server-key hourly quota
        allowed_hour, retry_after_hour = limiter.is_allowed(
            client_ip=client_ip,
            category="ai_server_key_hourly",
            max_requests=configs["ai_server_key_per_hour"],
            window_seconds=3600,
        )
        if not allowed_hour:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Server-side AI quota exceeded ({configs['ai_server_key_per_hour']} plans/hour). "
                    "Please provide your own Gemini API key in Settings or wait before retrying."
                ),
                headers={"Retry-After": str(retry_after_hour)},
            )

    # 3. Check general per-minute AI burst limit (applies to both custom and server keys)
    allowed_min, retry_after_min = limiter.is_allowed(
        client_ip=client_ip,
        category="ai_per_minute",
        max_requests=configs["ai_per_minute"],
        window_seconds=60,
    )
    if not allowed_min:
        raise HTTPException(
            status_code=429,
            detail=f"AI request rate limit exceeded ({configs['ai_per_minute']} requests/minute). Please slow down.",
            headers={"Retry-After": str(retry_after_min)},
        )


def enforce_global_rate_limit(request: Request) -> None:
    """Enforce general API request rate limit across all endpoints."""
    configs = get_rate_limit_configs()
    client_ip = limiter.get_client_ip(request)

    allowed, retry_after = limiter.is_allowed(
        client_ip=client_ip,
        category="global_traffic",
        max_requests=configs["global_per_minute"],
        window_seconds=60,
    )
    if not allowed:
        raise HTTPException(
            status_code=429,
            detail="Too many requests. Please slow down.",
            headers={"Retry-After": str(retry_after)},
        )
