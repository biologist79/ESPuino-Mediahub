"""A tiny in-process TTL cache, used by the podcast sources.

Deliberately not shared between gunicorn workers — a duplicate feed fetch
costs nothing, and a cache server would be absurd overhead for a household
hub (same reasoning as the JSON file store).
"""

import threading
import time


class TtlCache:
    def __init__(self):
        self._lock = threading.Lock()
        self._entries = {}

    def get(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if expires_at < time.monotonic():
                del self._entries[key]
                return None
            return value

    def set(self, key, value, ttl):
        with self._lock:
            self._entries[key] = (time.monotonic() + ttl, value)
