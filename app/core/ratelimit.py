"""Fixed-window rate limiter backed by Redis, with an in-process fallback when Redis is unavailable."""

from __future__ import annotations

import threading
import time
from functools import lru_cache

from app.core.config import get_settings
from app.core.errors import RateLimited
from app.core.logging import get_logger

log = get_logger(__name__)
_local: dict[str, tuple[int, int]] = {}
_lock = threading.Lock()


@lru_cache
def _redis():
    try:
        import redis

        r = redis.Redis.from_url(get_settings().redis_url, socket_timeout=0.3, socket_connect_timeout=0.3)
        r.ping()
        return r
    except Exception:
        log.warning("ratelimit.redis_unavailable_using_local")
        return None


def hit(key: str, limit: int, window_s: int = 60) -> int:
    """Count a hit; raise RateLimited when over the limit. Returns the current count."""
    bucket = int(time.time() // window_s)
    k = f"rl:{key}:{bucket}"
    r = _redis()
    if r is not None:
        try:
            pipe = r.pipeline()
            pipe.incr(k)
            pipe.expire(k, window_s + 5)
            count = int(pipe.execute()[0])
        except Exception:
            count = _local_hit(k)
    else:
        count = _local_hit(k)
    if count > limit:
        raise RateLimited("Too many requests. Please slow down.", details={"retry_after_s": window_s})
    return count


def _local_hit(k: str) -> int:
    with _lock:
        n = _local.get(k, (0, 0))[0] + 1
        _local[k] = (n, int(time.time()))
        if len(_local) > 50_000:
            _local.clear()
        return n
