"""One cached result per read-only page view, refreshed in the background.

The same pattern as clusters.Clusters and account_identity.AccountIdentity:
the first request computes the result; later requests get the cached one at
once, and a stale one starts a single background refresh. A failed refresh
keeps serving the previous result (still marked stale) so a busy database
never turns a page into an error; the next request retries.
"""
from __future__ import annotations

import logging
import threading
import time

LOGGER = logging.getLogger(__name__)


class CachedView:
    def __init__(self, build, *, seconds, name):
        self._build, self.seconds, self.name = build, seconds, name
        self._lock = threading.Lock()
        self._refreshing = False
        self._cached = None

    def _refresh(self):
        try:
            self._cached = (time.monotonic(), self._build())
        except Exception:
            LOGGER.exception('%s refresh failed', self.name)
        finally:
            with self._lock:
                self._refreshing = False

    def get(self):
        cached = self._cached
        if cached is None:
            with self._lock:
                if self._cached is None:
                    self._cached = (time.monotonic(), self._build())
                return {**self._cached[1], 'stale': False}
        stale = time.monotonic() - cached[0] >= self.seconds
        if stale:
            with self._lock:
                if not self._refreshing:
                    self._refreshing = True
                    threading.Thread(target=self._refresh, name='ctn-view-' + self.name, daemon=True).start()
        return {**cached[1], 'stale': stale}
