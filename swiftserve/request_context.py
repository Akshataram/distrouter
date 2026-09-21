"""Request correlation ID, propagated via a contextvar rather than threaded
through every function signature: it's set once per request by a
middleware (in `app.py` for the router, `replica_sidecar.py` for the
sidecar) and read anywhere in that request's async call chain -- including
`proxy.py`'s retry/failure log lines, which run several `await`s deep from
the route handler with no other reference to the current request.

A contextvar is correctly request-scoped here because the whole chain from
the middleware's `call_next` down through `forward_chat_completion` runs as
one asyncio task per request; it would NOT be safe to rely on for state
shared across a manually-spawned `asyncio.create_task` (e.g. the background
metrics-scrape loop), which is exactly why those keep using the default
"-" instead of picking up whatever request happened to be in flight when
the task was created.
"""

from __future__ import annotations

import contextvars
import logging

_current_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


def get_request_id() -> str:
    return _current_request_id.get()


def set_request_id(request_id: str) -> contextvars.Token:
    return _current_request_id.set(request_id)


def reset_request_id(token: contextvars.Token) -> None:
    _current_request_id.reset(token)


class RequestIdLogFilter(logging.Filter):
    """Attach to a logging.Handler (not just a Logger) so it applies to
    every record that reaches it regardless of which module's logger
    originated the record -- one filter on the root handler covers
    swiftserve.app, swiftserve.proxy, swiftserve.metrics_scraper, etc. all
    at once, since they all propagate up to it by default."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s [rid=%(request_id)s] %(message)s"


def configure_logging(level: int = logging.INFO) -> None:
    """Shared basicConfig for both the router and the sidecar processes,
    so `%(request_id)s` is always a valid format field and INFO-level logs
    are never silently swallowed by Python's handler-of-last-resort (which
    only prints WARNING and above) when a process is run standalone."""
    logging.basicConfig(level=level, format=LOG_FORMAT)
    logging.getLogger().handlers[0].addFilter(RequestIdLogFilter())
