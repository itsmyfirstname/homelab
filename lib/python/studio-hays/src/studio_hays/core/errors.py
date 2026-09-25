"""Exception hierarchy for studio-hays (doc 01, ``errors.py``).

All errors raised by studio-hays code derive from :class:`StudioHaysError`
so callers can catch one base type.
"""


class StudioHaysError(Exception):
    """Base class for all studio-hays errors."""


class ConfigError(StudioHaysError):
    """Missing or invalid configuration.

    Raised at first use (the lazy guarantee), never at import time.
    Messages name the env var or kwarg to fix.
    """


class RemoteError(StudioHaysError):
    """Network or remote-API failure. Carries URL / status context."""


class NotFoundError(StudioHaysError):
    """Resource not on the remote and not in local state."""


class DriftError(StudioHaysError):
    """Raised by strict operations when drift was not expected."""


class StateError(StudioHaysError):
    """Local state problem: schema mismatch, corrupt or locked DB."""
