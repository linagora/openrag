from __future__ import annotations

import sys
from importlib import import_module
from types import ModuleType
from unittest.mock import Mock

from core.config.root import Settings
from di.workers import ensure_worker_bootstrap


def test_ensure_worker_bootstrap_initializes_explicitly() -> None:
    """Startup calls the worker bootstrap function instead of relying on import side effects."""
    module = ModuleType("services.workers.bootstrap")
    module.initialize_worker_bootstrap = Mock()
    sys.modules["services.workers.bootstrap"] = module
    settings = Settings()

    try:
        ensure_worker_bootstrap(settings)
    finally:
        sys.modules.pop("services.workers.bootstrap", None)

    module.initialize_worker_bootstrap.assert_called_once_with(settings)


def test_worker_bootstrap_import_has_no_actor_side_effects() -> None:
    """Importing worker bootstrap does not create detached actors."""
    sys.modules.pop("services.workers.bootstrap", None)

    module = import_module("services.workers.bootstrap")

    assert module.actor_creation_map == {}
    assert not hasattr(module, "task_state_manager")
    assert not hasattr(module, "serializer")
