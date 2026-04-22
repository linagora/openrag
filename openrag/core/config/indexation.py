"""Indexation pipeline configuration — loaders, parsers, transcribers."""

from __future__ import annotations

from pydantic import Field

from .base import ConfigMixin

# ---------------------------------------------------------------------------
# Transcriber (nested under loader)
# ---------------------------------------------------------------------------


class TranscriberConfig(ConfigMixin):
    base_url: str = "http://transcriber:8000/v1"
    api_key: str = Field(default="EMPTY", repr=False)
    model_name: str = "openai/whisper-large-v3-turbo"
    timeout: int = 3600
    max_concurrent_chunks: int = 20
    use_whisper_lang_detector: bool = True


# ---------------------------------------------------------------------------
# OpenAI Loader (nested under loader)
# ---------------------------------------------------------------------------


class OpenAILoaderConfig(ConfigMixin):
    base_url: str = "http://openai:8000/v1"
    api_key: str = Field(default="EMPTY", repr=False)
    model: str = "dotsocr-model"
    temperature: float = 0.2
    timeout: int = 180
    max_retries: int = 2
    top_p: float = 0.9
    concurrency_limit: int = 20


# ---------------------------------------------------------------------------
# Local Whisper (nested under loader)
# ---------------------------------------------------------------------------


class LocalWhisperConfig(ConfigMixin):
    model: str = "base"
    whisper_n_workers: int = 3
    whisper_num_gpus: float = 0.01
    whisper_concurrency_per_worker: int = 2
    whisper_timeout: int = 1800
    whisper_max_task_retry: int = 1
    whisper_retry_base_delay: float = 2.0


# ---------------------------------------------------------------------------
# File loaders mapping (nested under loader)
# ---------------------------------------------------------------------------


class FileLoadersConfig(ConfigMixin):
    txt: str = "TextLoader"
    pdf: str = "MarkerLoader"
    eml: str = "EmlLoader"
    docx: str = "DocxLoader"
    pptx: str = "PPTXLoader"
    doc: str = "DocLoader"
    png: str = "ImageLoader"
    jpeg: str = "ImageLoader"
    jpg: str = "ImageLoader"
    svg: str = "ImageLoader"
    wav: str = "LocalWhisperLoader"
    mp3: str = "LocalWhisperLoader"
    flac: str = "LocalWhisperLoader"
    ogg: str = "LocalWhisperLoader"
    aac: str = "LocalWhisperLoader"
    flv: str = "LocalWhisperLoader"
    wma: str = "LocalWhisperLoader"
    mp4: str = "LocalWhisperLoader"
    md: str = "MarkdownLoader"


# ---------------------------------------------------------------------------
# Mimetypes mapping (nested under loader)
# ---------------------------------------------------------------------------


class MimetypesConfig(ConfigMixin):
    """Maps MIME type strings to file extensions.

    Access via .to_dict() for {mime_type: extension} mapping.
    """

    text_plain: str = Field(default=".txt", alias="text/plain")
    text_markdown: str = Field(default=".md", alias="text/markdown")
    application_pdf: str = Field(default=".pdf", alias="application/pdf")
    message_rfc822: str = Field(default=".eml", alias="message/rfc822")
    application_docx: str = Field(
        default=".docx",
        alias="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    application_pptx: str = Field(
        default=".pptx",
        alias="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )
    application_msword: str = Field(default=".doc", alias="application/msword")
    image_png: str = Field(default=".png", alias="image/png")
    image_jpeg: str = Field(default=".jpeg", alias="image/jpeg")
    audio_wav: str = Field(default=".wav", alias="audio/wav")
    audio_mpeg: str = Field(default=".mp3", alias="audio/mpeg")
    audio_flac: str = Field(default=".flac", alias="audio/flac")
    audio_ogg: str = Field(default=".ogg", alias="audio/ogg")
    audio_aac: str = Field(default=".aac", alias="audio/aac")
    video_x_flv: str = Field(default=".flv", alias="video/x-flv")
    audio_x_ms_wma: str = Field(default=".wma", alias="audio/x-ms-wma")
    video_mp4: str = Field(default=".mp4", alias="video/mp4")

    model_config = {"frozen": True, "extra": "allow", "populate_by_name": True}

    def to_dict(self) -> dict[str, str]:
        """Return {mime_type: extension} mapping using aliases as keys."""
        result = {}
        for field_name, field_info in type(self).model_fields.items():
            alias = field_info.alias or field_name
            result[alias] = getattr(self, field_name)
        if self.__pydantic_extra__:
            result.update(self.__pydantic_extra__)
        return result


# ---------------------------------------------------------------------------
# Loader (top-level indexation config)
# ---------------------------------------------------------------------------


class LoaderConfig(ConfigMixin):
    image_captioning: bool = True
    image_captioning_url: bool = True
    save_markdown: bool = False
    mimetypes: MimetypesConfig = Field(default_factory=MimetypesConfig)
    local_whisper: LocalWhisperConfig = Field(default_factory=LocalWhisperConfig)
    file_loaders: FileLoadersConfig = Field(default_factory=FileLoadersConfig)
    marker_max_tasks_per_child: int = 20
    marker_pool_size: int = 1
    marker_max_processes: int = 2
    marker_num_gpus: float = 0.01
    marker_timeout: int = 3600
    marker_pdftext_workers: int = 2
    marker_chunk_size: int = 10
    marker_max_task_retry: int = 3
    marker_retry_base_delay: float = 2.0
    docling_num_gpus: float = Field(default=0.01, ge=0)
    docling_pool_size: int = Field(default=1, ge=1)
    docling_max_tasks_per_worker: int = Field(default=2, ge=1)
    docling_timeout: int = 3600
    docling_max_task_retry: int = 3
    docling_retry_base_delay: float = 2.0
    transcriber: TranscriberConfig = Field(default_factory=TranscriberConfig)
    openai: OpenAILoaderConfig = Field(default_factory=OpenAILoaderConfig)
