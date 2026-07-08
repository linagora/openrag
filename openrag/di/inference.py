"""Convenience wrapper — registers all inference adapters at once.

Delegates to the per-domain registration modules.
"""

from di.embedders import register_embedders
from di.llms import register_llms
from di.rerankers import register_rerankers
from di.vlms import register_vlms


def register_inference() -> None:
    register_embedders()
    register_llms()
    register_rerankers()
    register_vlms()
