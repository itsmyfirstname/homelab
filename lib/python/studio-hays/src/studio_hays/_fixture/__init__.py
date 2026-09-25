"""Fixture module: the toy client that exercises the core kernel (M1).

It models a single resource type, "widget", against an in-memory fake
remote, mirroring the shape a real module (home_assistant, proxmox) has.
The contract test suite in ``tests/`` runs against it.
"""

from .client import Client, FixtureRemote
from .operations import OPERATIONS

__all__ = ["Client", "FixtureRemote", "OPERATIONS"]
