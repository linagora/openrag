"""VLM ABC + registry."""

from openrag.core.vlm.registry import vlm_registry
from openrag.core.vlm.vlm import VLM

__all__ = ["VLM", "vlm_registry"]
