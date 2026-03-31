"""FileReducer — iterative map-then-merge summarization."""

from components.prompts.prompts import FILE_REDUCER_PROMPT
from components.utils import get_llm_semaphore
from langchain_core.documents.base import Document
from langchain_openai import ChatOpenAI
from tqdm.asyncio import tqdm
from utils.logger import get_logger

logger = get_logger()

_IRRELEVANT = "IRRELEVANT"


class FileReducer:
    """Summarizes a file's chunks by repeatedly grouping and summarizing
    until the result fits within `max_tokens`."""

    def __init__(self, config):
        self._llm = ChatOpenAI(
            base_url=config.llm.get("base_url"),
            api_key=config.llm.get("api_key"),
            model=config.llm.get("model"),
            temperature=config.llm.get("temperature", 0.3),
            timeout=config.llm.get("timeout", 60),
        )
        self._max_group_tokens: int = config.file_reducer.get("max_group_tokens", 4096)
        self._min_group_tokens: int = config.file_reducer.get("min_group_tokens", 2048)
        self._max_rounds: int = config.file_reducer.get("max_rounds", 3)
        self._min_shrink_ratio: float = config.file_reducer.get("min_shrink_ratio", 0.1)
        self._target_size_tokens: int = config.file_reducer.get("target_size_tokens", 1024)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """Fast ~4 chars-per-token estimate."""
        return len(text) // 4

    def _fits(self, texts: list[str]) -> bool:
        """True when the joined texts are already within the output budget."""
        return self._estimate_tokens("\n\n".join(texts)) <= self._target_size_tokens

    def _group(self, texts: list[str]) -> list[list[str]]:
        """Bin texts into groups that each stay under `_max_group_tokens`."""
        groups: list[list[str]] = []
        current: list[str] = []
        current_tokens = 0
        for text in texts:
            tokens = self._estimate_tokens(text)
            if current and current_tokens + tokens > self._max_group_tokens:
                groups.append(current)
                current = [text]
                current_tokens = tokens
            else:
                current.append(text)
                current_tokens += tokens

        if current:
            groups.append(current)

        return groups

    async def _summarize(self, query: str, texts: list[str]) -> str:
        """Summarize a group of texts; skip the LLM if the group is already small."""

        async with get_llm_semaphore():
            try:
                joined = "\n\n".join(texts)
                if self._estimate_tokens(joined) <= self._min_group_tokens:
                    return joined

                response = await self._llm.ainvoke(
                    [
                        {"role": "system", "content": FILE_REDUCER_PROMPT},
                        {"role": "user", "content": f"user query: {query}\n\ncontent to compress:\n{joined}"},
                    ]
                )
                return response.content
            except Exception as e:
                logger.error("Error during summarization", error=str(e))
                return "\n\n".join(texts)  # fall back to original to avoid None in texts

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self, query: str, chunks: list[Document]) -> Document:
        """Summarize *chunks* by grouping and merging until the result fits."""

        # Normalise to plain strings, preserve first chunk's metadata
        first_metadata = chunks[0].metadata if isinstance(chunks[0], Document) else {}
        filename = first_metadata.get("filename")
        log = logger.bind(filename=filename)

        texts: list[str] = [c.page_content if isinstance(c, Document) else c for c in chunks]
        tag = f"[{filename}] " if filename else ""
        rounds = 0

        while not self._fits(texts):
            if rounds >= self._max_rounds:
                log.warning("FileReducer hit max_rounds cap — stopping early", rounds=rounds)
                break

            tokens_before = self._estimate_tokens("\n\n".join(texts))
            groups = self._group(texts)
            texts = list(
                await tqdm.gather(
                    *[self._summarize(query, g) for g in groups],
                    desc=f"{tag}merge (round {rounds + 1})",
                )
            )

            # Filter chunks the LLM deemed irrelevant (keep at least one to avoid empty output)
            relevant = [t for t in texts if t.strip() != _IRRELEVANT]
            if relevant:
                texts = relevant

            tokens_after = self._estimate_tokens("\n\n".join(texts))
            shrink = (tokens_before - tokens_after) / max(tokens_before, 1)

            rounds += 1
            log.debug("Merge round complete", round=rounds, shrink_pct=round(shrink * 100, 1))

            if shrink < self._min_shrink_ratio:
                log.warning(
                    "FileReducer not converging (shrink below threshold) — stopping early",
                    rounds=rounds,
                    shrink_pct=round(shrink * 100, 1),
                )
                break

        content = texts[0] if len(texts) == 1 else "\n\n".join(texts)
        metadata = {
            **first_metadata,
            "_summarized": True,
            "_original_chunk_count": len(chunks),
            "_rounds": rounds,
        }
        log.debug("FileReducer done", estimated_tokens=self._estimate_tokens(content), rounds=rounds)
        return Document(page_content=f"{filename}\n\n{content}", metadata=metadata)

    async def reduce_all(self, query: str, docs_l: list[Document]) -> list[Document]:
        tasks = [self.run(query, chunks) for chunks in docs_l]
        return await tqdm.gather(*tasks, desc="Reducing files")
