"""Audio parser facades.

Each backend lives in its own module so its heavy dependencies (Ray
worker pools, cloud SDKs, …) are only pulled in by the concrete impl
in ``services/`` — the core facade just declares the parser type and
delegates ``parse()`` to an injected pool/client.
"""

from .client_based import ClientAudioParser
from .local_whisper import LocalWhisperParser

__all__ = ["ClientAudioParser", "LocalWhisperParser"]
