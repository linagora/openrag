"""Shared helpers for the automatic evaluation pipeline.

Anything used by more than one of generate_questions.py / benchmark.py /
context_ablation.py / dashboard.py belongs here rather than being duplicated —
as does any general-purpose, self-contained helper that doesn't depend on a
specific script's module-level state (CONFIG-derived LLM clients, etc.), even
if only one script currently calls it.

Sections
--------
- Chunk text helpers      : parse OpenRAG chunk payloads (filename marker, body text)
- Document categories     : content-domain classification with a runtime-discovered
                            taxonomy (research_paper, hr_policy, support_documentation…)
- OpenAI response parsing : pull sources / logprobs out of a ChatCompletion
- Judge JSON parsing      : lenient JSON parsing/repair for LLM judge output
- Context formatting      : numbered "[Source N]" blocks for judge prompts
- Retrieval trace         : combine gold chunk ids with a raw ranked retrieval list
- Numeric grounding       : exact-value scoring for value-answer question types
- File facets             : source filename/format/type facets for a dataset row
- Dataset loading         : load + validate a (golden) eval dataset JSON file
- Network preflight       : fail fast if a server is unreachable
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from typing import TypedDict

import httpx
from langchain_core.utils.json import parse_partial_json
from loguru import logger

# ===========================================================================
# Chunk text helpers
# ===========================================================================

# Filename marker written by the contextual-retrieval preamble ("* filename: X.pdf").
FILENAME_RE = re.compile(r"^\*\s*filename:\s*(.+?)\s*$", re.MULTILINE)


def chunk_body(text: str) -> str:
    """Chunk content with the ``[CONTEXT]`` preamble / ``* filename:`` line and page
    markers stripped, so callers see document text rather than generated summary."""
    if "[CHUNK_START]" in text:
        text = text.split("[CHUNK_START]", 1)[1]
    return re.sub(r"\[PAGE_\d+\]|\[CHUNK_END\]", " ", text).strip()


def filename_of(chunk: dict) -> str:
    """Filename for a chunk, falling back to its opaque file_id."""
    match = FILENAME_RE.search(chunk.get("text") or "")
    return match.group(1) if match else str(chunk.get("file_id", "") or "")


# ===========================================================================
# Document categories (content domain)
# ===========================================================================
# The taxonomy is **discovered from the corpus at runtime**, never hardcoded, so new
# domains — hr_policy, support_documentation, legal_contract, … — appear on their own
# rather than being forced into a fixed enum.
#
# Resolution order per document, cheapest first:
#     cache  >  unambiguous filename rule (free)  >  LLM (batched)
#
# Primary user is generate_questions.py, which writes the label into each dataset
# item's metadata.source.document_category. benchmark.py calls this only as a
# *fallback* for legacy datasets predating that field.

# Unambiguous filename patterns — a free fast path, NOT a taxonomy.
_ARXIV_RE = re.compile(r"^\d{4}[._]\d{4,5}")
_SEC_RE = re.compile(r"_(10K|10Q|8K|DEF14A|S1|20F|6K)[_.\-]", re.I)
_CHAPTER_RE = re.compile(r"^chapter[_\-\s]?\d+", re.I)
_HASH_PREFIX_RE = re.compile(r"^[0-9a-f]{16,}-", re.I)


def category_cache_path(partition: str) -> str:
    """Stable, corpus-scoped cache path (``.doc_categories/<partition>.json``).

    Deliberately NOT beside the dataset: the orchestrator writes a fresh
    ``reports/<uuid>/dataset.json`` per run, so a dataset-relative cache would miss
    every time — re-discovering a different taxonomy per run and making version
    comparisons incomparable.
    """
    directory = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".doc_categories")
    os.makedirs(directory, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(partition or "default"))
    return os.path.join(directory, f"{safe}.json")


def slug(label) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(label).strip().lower()).strip("_") or "other"


def heuristic_category(filename: str) -> str | None:
    """Free label for unambiguous filename patterns; None means 'ask the LLM'."""
    base = _HASH_PREFIX_RE.sub("", os.path.basename(str(filename)))
    if _SEC_RE.search(base):
        return "financial_filing"
    if _ARXIV_RE.match(base):
        return "research_paper"
    if _CHAPTER_RE.match(base):
        return "educational_material"
    return None


def strip_code_fences(text: str) -> str:
    """Strip a leading/trailing ```lang ... ``` markdown fence, if present."""
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    return text


def loose_json(raw: str):
    """Parse a JSON object/array out of a possibly fenced or chatty LLM reply."""
    text = strip_code_fences((raw or "").strip())
    try:
        return json.loads(text, strict=False)
    except (json.JSONDecodeError, ValueError):
        pass
    for pattern in (r"\[.*\]", r"\{.*\}"):
        match = re.search(pattern, text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0), strict=False)
            except (json.JSONDecodeError, ValueError):
                continue
    return None


def _spread(items: list, k: int) -> list:
    """Evenly-spaced pick of ``k`` items across ``items`` (order-preserving)."""
    if k <= 0 or not items:
        return []
    if k >= len(items):
        return list(items)
    step = len(items) / k
    return [items[int(i * step)] for i in range(k)]


def _diverse_sample(remaining: list[str], already_labelled: list[str], sample_docs: int) -> list[str]:
    """Documents to show the taxonomy-discovery call.

    Sampled by striding across the *sorted* names rather than taking the first N:
    corpora group related files together (all the .pptx decks adjacent, say), so a
    head slice shows the model one narrow slice and it proposes a degenerate
    taxonomy. A fifth of the budget comes from already-labelled documents so the
    model also sees the corpus's established shape.
    """
    n_known = min(len(already_labelled), max(0, sample_docs // 5))
    return _spread(sorted(remaining), sample_docs - n_known) + _spread(already_labelled, n_known)


async def _discover_taxonomy(samples, known, ainvoke, hints, max_labels) -> list[str]:
    """One LLM call: propose a content-domain taxonomy covering this corpus."""
    listing = "\n\n".join(f"FILENAME: {n}\nEXCERPT: {t}" for n, t in samples)
    prompt = (
        "You are designing a taxonomy of DOCUMENT CONTENT DOMAINS for a corpus.\n"
        f"Below are {len(samples)} sample documents.\n\n"
        f"Labels already in use (you MUST keep these): {known or 'none'}\n"
        + (f"Example labels for style (NOT exhaustive): {list(hints)}\n" if hints else "")
        + f"\nThe corpus is HETEROGENEOUS. Propose the FULL set of labels — at least "
        f"{min(5, max_labels)} and at most {max_labels} — covering these documents by "
        "SUBJECT DOMAIN, e.g. research_paper, financial_filing, hr_policy, "
        "support_documentation, legal_contract, technical_documentation, "
        "business_presentation, marketing_material, product_documentation, "
        "government_public_sector. Judge by CONTENT, not file format.\n"
        "Rules: lowercase snake_case; specific but reusable; never per-document labels.\n"
        "Do NOT collapse everything into one or two broad labels — distinguish the "
        "genuinely different subject domains you can see in the sample.\n"
        'Return ONLY a JSON array of strings, e.g. ["research_paper","hr_policy"].\n\n'
        f"DOCUMENTS:\n{listing}"
    )
    try:
        parsed = loose_json(await ainvoke(prompt))
    except Exception as e:  # discovery must never abort the caller
        logger.warning(f"Document-taxonomy discovery failed ({e}); using known labels only.")
        parsed = None
    labels = [slug(x) for x in parsed] if isinstance(parsed, list) else []
    for known_label in known:  # never drop labels the filename rules already assigned
        if known_label not in labels:
            labels.append(known_label)
    if "other" not in labels:
        labels.append("other")
    return labels


async def _classify_batch(batch, taxonomy, ainvoke, semaphore) -> dict[str, str]:
    """Assign one taxonomy label to each document in the batch (one LLM call)."""
    listing = "\n\n".join(f"[{i}] FILENAME: {n}\nEXCERPT: {t}" for i, (n, t) in enumerate(batch))
    prompt = (
        "Classify each document into exactly ONE label from this list, by SUBJECT DOMAIN "
        "(not file format).\n"
        f"LABELS: {taxonomy}\n"
        "Return ONLY a JSON object mapping each index to its label, e.g. "
        '{"0":"hr_policy","1":"research_paper"}.\n\n'
        f"DOCUMENTS:\n{listing}"
    )
    parsed = None
    try:
        if semaphore is not None:
            async with semaphore:
                parsed = loose_json(await ainvoke(prompt))
        else:
            parsed = loose_json(await ainvoke(prompt))
    except Exception as e:
        logger.debug(f"Document classification batch failed: {e}")
    valid = set(taxonomy)
    out = {}
    for i, (name, _) in enumerate(batch):
        label = slug((parsed or {}).get(str(i), "")) if isinstance(parsed, dict) else ""
        out[name] = label if label in valid else "other"
    return out


async def resolve_document_categories(
    doc_texts: dict[str, str],
    cache_path: str,
    ainvoke,
    *,
    semaphore=None,
    enabled: bool = True,
    hints=(),
    max_labels: int = 12,
    sample_docs: int = 40,
    sample_chars: int = 1200,
    batch_size: int = 10,
) -> dict[str, str]:
    """Map ``{filename: text}`` -> ``{filename: category}``.

    ``ainvoke`` is an async ``(prompt: str) -> str`` callable, so each caller supplies
    its own LLM (the generator's critic model, or benchmark's judge model). Falls back
    to "other" — never raises — if the LLM is unavailable.
    """
    cache = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                cache = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Ignoring unreadable document-category cache {cache_path}: {e}")
    taxonomy = [slug(x) for x in (cache.get("taxonomy") or [])]
    cached_docs = {k: slug(v) for k, v in (cache.get("documents") or {}).items()}
    assigned = {k: v for k, v in cached_docs.items() if k in doc_texts}
    n_cached = len(assigned)

    for name in doc_texts:  # free filename rules
        if name not in assigned:
            hit = heuristic_category(name)
            if hit:
                assigned[name] = hit
    n_free = len(assigned) - n_cached
    remaining = [n for n in doc_texts if n not in assigned]

    if remaining and enabled:
        if not taxonomy:
            picks = _diverse_sample(remaining, sorted(set(doc_texts) - set(remaining)), sample_docs)
            sample = [(n, doc_texts[n][:sample_chars]) for n in picks]
            taxonomy = await _discover_taxonomy(
                sample, sorted(set(assigned.values())), ainvoke, hints, max_labels
            )
            logger.info(f"Discovered document taxonomy ({len(taxonomy)}): {taxonomy}")
        size = max(1, batch_size)
        batches = [
            [(n, doc_texts[n][:sample_chars]) for n in remaining[i : i + size]]
            for i in range(0, len(remaining), size)
        ]
        for res in await asyncio.gather(
            *[_classify_batch(b, taxonomy, ainvoke, semaphore) for b in batches]
        ):
            assigned.update(res)
        logger.info(
            f"Document categories: {n_cached} cached, {n_free} by filename rule, "
            f"{len(remaining)} classified in {len(batches)} LLM call(s)."
        )
        merged = dict(cached_docs)
        merged.update(assigned)
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"taxonomy": taxonomy, "documents": merged}, f, ensure_ascii=False, indent=2
                )
        except OSError as e:
            logger.warning(f"Could not write document-category cache: {e}")
    elif not remaining:
        logger.info(
            f"Document categories: {n_cached} cached, {n_free} by filename rule (no LLM calls)."
        )
    return assigned


# ===========================================================================
# OpenAI response parsing
# ===========================================================================


def extract_sources(res) -> list[dict]:
    """Pull the source list out of a ChatCompletion's non-standard ``extra`` field.

    OpenRAG returns the cited sources as a JSON string in ``res.extra``
    (``{"sources": [...]}``). This is not part of the OpenAI schema, so guard every
    step: a missing attribute, a non-JSON payload, or an unexpected shape all degrade
    to an empty list rather than raising and dropping the whole row."""
    raw = getattr(res, "extra", None)
    if not raw:
        return []
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        sources = parsed.get("sources", []) if isinstance(parsed, dict) else []
    except (ValueError, TypeError) as e:
        logger.debug(f"Could not parse sources from res.extra: {e}")
        return []
    return [s for s in sources if isinstance(s, dict)]


def extract_logprob_metrics(res) -> tuple[float, float, list[str], list[float]]:
    """Pull per-token logprobs from a ChatCompletion.

    Returns (mean_logprob, perplexity, tokens, token_logprobs). The latter two are
    parallel arrays in generation order, used downstream for per-question logprob
    visualisations. Returns (nan, nan, [], []) if logprobs are absent."""
    try:
        token_lps = res.choices[0].logprobs.content
    except AttributeError:
        return float("nan"), float("nan"), [], []
    if not token_lps:
        return float("nan"), float("nan"), [], []
    tokens: list[str] = []
    lps: list[float] = []
    for t in token_lps:
        if t.logprob is None:
            continue
        tokens.append(t.token)
        lps.append(t.logprob)
    if not lps:
        return float("nan"), float("nan"), [], []
    mean_lp = sum(lps) / len(lps)
    return mean_lp, math.exp(-mean_lp), tokens, lps


# ===========================================================================
# Judge JSON parsing
# ===========================================================================


def escape_json_string_control_chars(s: str) -> str:
    out = []
    in_string = False
    escaped = False
    for ch in s:
        if escaped:
            if ord(ch) < 0x20:
                # Mangled escape like "\<newline>" — the backslash is already
                # emitted, so complete it into a legal escape sequence.
                if ch == "\n":
                    out.append("n")
                elif ch == "\r":
                    out.append("r")
                elif ch == "\t":
                    out.append("t")
                else:
                    out.append(f"u{ord(ch):04x}")
            else:
                out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ord(ch) < 0x20:
            if ch == "\n":
                out.append("\\n")
            elif ch == "\r":
                out.append("\\r")
            elif ch == "\t":
                out.append("\\t")
            else:
                out.append(f"\\u{ord(ch):04x}")
            continue
        if not in_string and ord(ch) < 0x20:
            continue
        out.append(ch)
    return "".join(out)


def drop_incomplete_tail(obj):
    """Drop trailing dict items from list fields when they lack a key their peers
    have — the signature of a max_tokens-truncated judge response (e.g. a final
    statement emitted without its verdict)."""
    if isinstance(obj, dict):
        for key, val in obj.items():
            if isinstance(val, list) and val and all(isinstance(x, dict) for x in val):
                full_keys = max((set(x.keys()) for x in val), key=len)
                while val and set(val[-1].keys()) < full_keys:
                    val.pop()
                obj[key] = val
    return obj


def parse_judge_json(raw: str, schema):
    """Parse possibly-dirty judge JSON into ``schema``.

    Judge models frequently emit JSON that is illegal under strict parsers:
    wrapped in markdown ``` fences, or with raw control characters (newlines,
    tabs, \\u0000-\\u001F) inside string values. Pydantic's
    ``model_validate_json`` has no lenient mode and rejects these outright, so
    we parse with the stdlib ``json.loads(strict=False)`` (which *permits*
    control characters inside strings) and validate the resulting object. Only
    if that still fails do we fall back to the manual control-char escaper.
    """
    # Strip ```json ... ``` (or bare ``` ... ```) fences if the model added them.
    text = strip_code_fences(raw.strip())
    try:
        return schema.model_validate(json.loads(text, strict=False))
    except (json.JSONDecodeError, ValueError):
        pass
    # Harder breakage (e.g. a stray backslash before a control char): repair
    # escapes/control chars, then lenient-parse again.
    repaired = escape_json_string_control_chars(text)
    try:
        return schema.model_validate(json.loads(repaired, strict=False))
    except (json.JSONDecodeError, ValueError):
        # Truncated output (hit max_tokens): complete missing closers and parse.
        obj = parse_partial_json(repaired)
        try:
            return schema.model_validate(obj)
        except ValueError:
            # Trailing list item truncated mid-object (e.g. a statement with no
            # verdict yet) — drop incomplete tail items, then validate.
            return schema.model_validate(drop_incomplete_tail(obj))


# ===========================================================================
# Context formatting
# ===========================================================================


def format_context(chunks: list[str]) -> str:
    """Join non-empty chunks into numbered '[Source N]' blocks separated by '---'."""
    return "\n\n---\n\n".join(
        f"[Source {i + 1}]\n{c}" for i, c in enumerate(chunks) if c
    )


# ===========================================================================
# Retrieval trace
# ===========================================================================


def build_retrieval_trace(question_id, question: str, gold_ids: list[str], ranked: list[dict] | None) -> dict:
    """Combine ground-truth chunk ids with the raw ranked retrieval list into one
    inspectable record: which rank (if any) each gold chunk landed at, and which
    gold chunks never showed up anywhere in the fetched top-k at all."""
    fetch_failed = ranked is None
    ranked = [dict(r) for r in (ranked or [])]
    retrieved_ids = [r["chunk_id"] for r in ranked]
    gold_set = set(gold_ids)
    for r in ranked:
        r["is_gold"] = r["chunk_id"] in gold_set
    hit_ranks = [r["rank"] for r in ranked if r["is_gold"]]
    missed_gold_ids = [] if fetch_failed else [g for g in gold_ids if g not in retrieved_ids]
    return {
        "question_id": question_id,
        "question": question,
        "gold_chunk_ids": gold_ids,
        "retrieved": ranked,
        "n_gold": len(gold_ids),
        "n_gold_hit": len(hit_ranks),
        "first_gold_rank": min(hit_ranks) if hit_ranks else None,
        "missed_gold_ids": missed_gold_ids,
        "fetch_failed": fetch_failed,
    }


# ===========================================================================
# Numeric grounding (value-answer exact-value scoring)
# ===========================================================================
# table_lookup / numerical_reasoning question types. The LLM completion/precision
# judges give partial credit for a wrong number; this adds a strict signal on top:
# did the response actually contain the golden's value(s)?

NUMBER_RE = re.compile(r"[-+]?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?")


def extract_numbers(text: str) -> list[float]:
    """All numeric literals in ``text`` as floats (handles 1,000 / 3.14 / 1.5e-3 / %)."""
    nums = []
    for tok in NUMBER_RE.findall(text or ""):
        tok = tok.replace(",", "").rstrip(".")
        if not tok or tok in ("+", "-"):
            continue
        try:
            nums.append(float(tok))
        except ValueError:
            continue
    return nums


def numeric_grounding(golden: str, response: str, rel_tol: float, abs_tol: float) -> float:
    """Strict numeric-grounding score for a value-answer row.

    Returns 1.0 if EVERY number in the golden answer appears in the response within
    tolerance, 0.0 if any is missing, or NaN when it doesn't apply (golden has no
    number, or the response is empty/None). Conservative: a golden that shows its
    working ("10 - 7.5 = 2.5") requires all three numbers, so this is a strict lower
    bound that complements — not replaces — the LLM judge.
    """
    if not response:
        return float("nan")
    golden_nums = extract_numbers(golden)
    if not golden_nums:
        return float("nan")
    resp_nums = extract_numbers(response)
    if not resp_nums:
        return 0.0
    for gv in golden_nums:
        if not any(math.isclose(gv, rv, rel_tol=rel_tol, abs_tol=abs_tol) for rv in resp_nums):
            return 0.0
    return 1.0


# ===========================================================================
# File facets (source filename/format/type for a dataset row)
# ===========================================================================
# Filenames are parsed out of the chunk text ("* filename: X.pdf", written by the
# contextual-retrieval preamble) rather than metadata, so these work on existing
# datasets without regenerating them. (Free + deterministic, so unlike
# document_category these deliberately stay computed at eval time.)

# Extension -> category, covering every format OpenRAG actually ingests: the loader
# registry in openrag/core/config/indexation.py (FileLoadersConfig) plus DocumentType.
# Categories mirror how the file was PARSED, which is the useful axis to slice on
# (e.g. Whisper-transcribed audio vs natively-extracted text).
FORMAT_TO_TYPE = {
    # PyMuPDFLoader / DocxLoader / DocLoader
    "pdf": "document", "docx": "document", "doc": "document",
    # PPTXLoader
    "pptx": "presentation",
    # TextLoader / MarkdownLoader
    "txt": "text", "md": "text",
    # EmlLoader
    "eml": "email",
    # DocumentType.HTML
    "html": "web",
    # ImageLoader (VLM captioning)
    "png": "image", "jpeg": "image", "jpg": "image", "svg": "image",
    # LocalWhisperLoader (transcription)
    "wav": "audio", "mp3": "audio", "flac": "audio", "ogg": "audio",
    "aac": "audio", "wma": "audio",
    # video containers — also transcribed via LocalWhisperLoader
    "mp4": "video", "flv": "video",
}


def file_facets(item) -> tuple[str, str, str]:
    """Return (source_files, file_format, file_type) for a dataset row.

    ``source_files`` is a "|"-joined list of the distinct documents the question was
    built from. ``file_format`` is the extension (pdf/md/pptx/…) and ``file_type`` its
    OpenRAG parse category (document/presentation/text/image/audio/…); both are
    "mixed" when the question spans documents that disagree, and "unknown" for a
    format OpenRAG doesn't ingest. Falls back to the opaque file_id when a chunk
    carries no filename marker.
    """
    names, formats = [], []
    for c in item.get("chunks") or []:
        name = filename_of(c)
        if not name:
            continue
        names.append(name)
        formats.append(os.path.splitext(name)[1].lower().lstrip(".") or "unknown")
    if not names:
        return "", "unknown", "unknown"
    uniq_formats = sorted(set(formats))
    fmt = uniq_formats[0] if len(uniq_formats) == 1 else "mixed"
    uniq_types = sorted({FORMAT_TO_TYPE.get(f, "unknown") for f in formats})
    ftype = uniq_types[0] if len(uniq_types) == 1 else "mixed"
    return "|".join(sorted(set(names))), fmt, ftype


# ===========================================================================
# Dataset loading
# ===========================================================================


class Element(TypedDict):
    question: str
    llm_answer: str
    chunks: list[dict]


def load_and_validate_dataset(path: str) -> list[Element]:
    """Load a (golden) dataset JSON file and validate its schema.

    Required per-row: ``question`` (non-empty str), ``llm_answer`` (str).
    Optional per-row: ``answerable`` (bool, default True), ``chunks`` (list of
    objects each with an ``id`` field, default []).
    Rows without ``chunks`` are still scored on generation/judges; retrieval
    metrics are skipped for those rows.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError(f"Dataset {path!r} must be a JSON list of entries.")

    normalized: list[Element] = []
    n_with_chunks = 0
    n_answerable = 0
    n_unanswerable = 0
    for i, row in enumerate(data):
        if not isinstance(row, dict):
            raise ValueError(f"Dataset entry #{i} is not an object.")
        question = row.get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError(f"Dataset entry #{i} is missing a non-empty 'question' string.")
        llm_answer = row.get("llm_answer")
        if not isinstance(llm_answer, str):
            raise ValueError(f"Dataset entry #{i} is missing a 'llm_answer' string.")
        answerable = bool(row.get("answerable", True))
        chunks = row.get("chunks") or []
        if not isinstance(chunks, list):
            raise ValueError(f"Dataset entry #{i} 'chunks' must be a list.")
        for j, c in enumerate(chunks):
            if not isinstance(c, dict) or "id" not in c:
                raise ValueError(
                    f"Dataset entry #{i} chunk #{j} must be an object with an 'id' field."
                )
        if chunks:
            n_with_chunks += 1
        if answerable:
            n_answerable += 1
        else:
            n_unanswerable += 1
        normalized.append({**row, "answerable": answerable, "chunks": chunks})

    logger.info(
        f"Loaded {len(normalized)} entries from {path} "
        f"(answerable={n_answerable}, unanswerable={n_unanswerable}, "
        f"with_ground_truth_chunks={n_with_chunks})"
    )
    if n_with_chunks == 0:
        logger.warning(
            "No rows contain ground-truth chunks — retrieval metrics will be skipped."
        )
    return normalized


# ===========================================================================
# Network preflight
# ===========================================================================


async def preflight_openrag(base_url: str, timeout: float = 5.0) -> None:
    """Fail fast if the OpenRAG server is unreachable, before running the full dataset.

    Hits the auth-free ``/health_check`` endpoint with a short timeout. Any HTTP
    response (even 404/401/500) means the server is *up* and only that matters here;
    only a transport-level failure (connection refused, DNS, timeout) means it's
    down. Raising here turns a slow, all-failed run into a ~1s clear error.
    """
    url = f"{base_url.rstrip('/')}/health_check"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url)
        logger.info(f"Pre-flight: OpenRAG reachable at {base_url} (HTTP {resp.status_code}).")
    except httpx.RequestError as e:
        raise RuntimeError(
            f"OpenRAG server at {base_url} is not reachable ({type(e).__name__}: {e}). "
            "Start the instance (e.g. `docker compose up -d`) and retry — aborting now "
            "instead of running the whole dataset against a down server."
        ) from e
