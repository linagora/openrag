import math

import nltk
from loguru import logger
from nltk.translate.bleu_score import SmoothingFunction, sentence_bleu
from nltk.translate.meteor_score import meteor_score
from rouge_score import rouge_scorer

for _pkg in ("punkt", "punkt_tab", "wordnet", "omw-1.4"):
    try:
        nltk.data.find(f"tokenizers/{_pkg}" if _pkg.startswith("punkt") else f"corpora/{_pkg}")
    except LookupError:
        nltk.download(_pkg, quiet=True)

_rouge = rouge_scorer.RougeScorer(["rouge1", "rouge2", "rougeL"], use_stemmer=True)
_bleu_smoothing = SmoothingFunction().method1


def compute_generation_metrics(reference: str, hypothesis: str) -> dict:
    """ROUGE-1/2/L (F1), BLEU-4, METEOR for a single (reference, hypothesis) pair."""
    if not reference or not hypothesis:
        return {"rouge1": 0.0, "rouge2": 0.0, "rougeL": 0.0, "bleu": 0.0, "meteor": 0.0}

    rouge = _rouge.score(reference, hypothesis)
    ref_tokens = nltk.word_tokenize(reference.lower())
    hyp_tokens = nltk.word_tokenize(hypothesis.lower())
    bleu = sentence_bleu([ref_tokens], hyp_tokens, smoothing_function=_bleu_smoothing)
    meteor = meteor_score([ref_tokens], hyp_tokens)

    return {
        "rouge1": rouge["rouge1"].fmeasure,
        "rouge2": rouge["rouge2"].fmeasure,
        "rougeL": rouge["rougeL"].fmeasure,
        "bleu": bleu,
        "meteor": meteor,
    }


def compute_hits(true_chunk_id, all_retrieved_chunks):
    return true_chunk_id in all_retrieved_chunks


def compute_hit_at_k(true_ids, retrieved_ids, k=None):
    if k is None:
        k = len(retrieved_ids)
    return int(len(set(true_ids) & set(retrieved_ids[:k])) > 0)


def compute_mrr(true_ids, retrieved_ids):
    for rank, doc_id in enumerate(retrieved_ids, start=1):
        if doc_id in true_ids:
            return 1 / rank
    return 0


def precision_at_k(true_ids, retrieved_ids, k):
    retrieved_k = retrieved_ids[:k]
    if k == 0:
        return 0
    return len(set(true_ids) & set(retrieved_k)) / k


def average_precision(true_ids, retrieved_ids):
    hits = 0
    sum_precisions = 0

    for i, doc_id in enumerate(retrieved_ids, start=1):
        if doc_id in true_ids:
            hits += 1
            sum_precisions += hits / i

    if len(true_ids) == 0:
        return 0

    return sum_precisions / len(true_ids)


def r_precision(true_ids, retrieved_ids):
    R = len(true_ids)
    return precision_at_k(true_ids, retrieved_ids, R)


def recall_at_k(true_ids, retrieved_ids, k):
    if len(true_ids) == 0:
        return 0
    retrieved_k = retrieved_ids[:k]
    return len(set(true_ids) & set(retrieved_k)) / len(true_ids)


def ndcg_at_k(true_ids, retrieved_ids, k):
    retrieved_k = retrieved_ids[:k]

    # DCG@k
    dcg = 0
    for i, doc_id in enumerate(retrieved_k):
        rel = 1 if doc_id in true_ids else 0
        dcg += rel / math.log2(i + 2)

    # iDCG@k (ideal ranking)
    ideal_rels = [1] * min(len(true_ids), k)
    idcg = sum(rel / math.log2(i + 2) for i, rel in enumerate(ideal_rels))

    return dcg / idcg if idcg > 0 else 0


def compute_inverted_ranks(true_chunk_id, all_retrieved_chunks):

    key = False
    try:
        rank = all_retrieved_chunks.index(true_chunk_id) + 1
        key = True
    except ValueError:
        logger.debug(f"ValueError: {true_chunk_id} not found in retrieved_ids")

    if key:
        return 1 / rank
    else:
        return 0
