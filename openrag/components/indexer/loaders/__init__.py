"""
Loader registry and initialization module.

This module handles the dynamic loading and registration of all document loaders.
"""

import importlib
import pkgutil
from pathlib import Path
from typing import Dict, Set, Type

from utils.logger import get_logger

from .base import BaseLoader

logger = get_logger()

def get_loader_classes(config: dict) -> Dict[str, Type[BaseLoader]]:
    # 1. Discover all subclasses
    root_pkg = "components.indexer.loaders"
    root_path = Path(__file__).parent

    discovered: Dict[str, Type[BaseLoader]] = {}

    for finder, module_name, is_pkg in pkgutil.walk_packages(
        path=[str(root_path)], prefix=f"{root_pkg}."
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError as e:
            logger.warning(f"Could not import module {module_name}: {e}")
            continue

        for attr in vars(module).values():
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseLoader)
                and attr is not BaseLoader
            ):
                discovered[attr.__name__] = attr

    # logger.debug(f"Discovered loaders: {discovered}")

    # 2. Read your config map of extensions → class names
    loader_classes: Dict[str, Type[BaseLoader]] = {}
    file_loaders = config.get("loader", {}).get("file_loaders", {})

    for ext, cls_name in file_loaders.items():
        cls = discovered.get(cls_name)
        if cls is None:
            logger.error(f"Configured loader '{cls_name}' for '.{ext}' not found")
            continue
        loader_classes[f".{ext}"] = cls
        logger.debug(f"Registered {cls_name} for .{ext}")

    logger.debug(f"Final loader map: {loader_classes.keys()}")
    return loader_classes


def get_supported_extensions(loader_classes: Dict[str, Type[BaseLoader]]) -> Set[str]:
    """
    Get the set of supported file extensions from the loaded classes.

    Args:
        loader_classes: Dictionary mapping file extensions to loader classes

    Returns:
        Set of supported file extensions
    """
    return set(loader_classes.keys())
