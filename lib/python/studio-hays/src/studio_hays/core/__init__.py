"""The studio-hays core kernel (doc 01).

Small, stdlib + httpx only, and deliberately free of infrastructure
knowledge: nothing here may know about entities, VMs, or any other remote
concept. Modules import from here; modules never import each other.
"""

from .base import BaseClient, StateNamespace
from .config import (
    Config,
    Key,
    ModuleSpec,
    load_env_file,
    resolve,
)
from .errors import (
    ConfigError,
    DriftError,
    NotFoundError,
    RemoteError,
    StateError,
    StudioHaysError,
)
from .operations import Operation, Param
from .state import StateStore
from .testing import FakeRemote, contract_suite

__all__ = [
    "BaseClient",
    "Config",
    "ConfigError",
    "DriftError",
    "Key",
    "ModuleSpec",
    "NotFoundError",
    "Operation",
    "Param",
    "RemoteError",
    "StateError",
    "StateNamespace",
    "StateStore",
    "StudioHaysError",
    "FakeRemote",
    "contract_suite",
    "load_env_file",
    "resolve",
]
