"""VLM registry."""

from core.utils.registry import Registry

from .vlm import VLM

vlm_registry: Registry[VLM] = Registry("vlm")
