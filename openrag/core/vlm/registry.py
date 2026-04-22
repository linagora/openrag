"""VLM registry."""

from openrag.core.utils.registry import Registry

from .vlm import VLM

vlm_registry: Registry[VLM] = Registry("vlm")
