import json
import numpy as np
from loguru import logger
from tqdm import tqdm
from reranker import Reranker
import time

# load the model
evaluate_with_reranking = False

model_name = "jinaai/jina-colbert-v2"
reranker_type = "colbert"

# model_name = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
# reranker_type = "crossencoder"

# model_name = "jinaai/jina-reranker-v2-base-multilingual"
# reranker_type = "crossencoder"

# model_name = "Alibaba-NLP/gte-multilingual-reranker-base"
# reranker_type = "crossencoder"

reranker = Reranker(reranker_type=reranker_type, model_name=model_name)


def compute_hits(true_chunk_id, all_retrieved_chunks):
    retrieved_ids = [c["corpus_id"] for c in all_retrieved_chunks]
    return [true_chunk_id in retrieved_ids]


def compute_inverted_ranks(true_chunk_id, all_retrieved_chunks):
    # see link: https://chatgpt.com/share/6813f998-2e88-8002-a472-6af2e9a64b61
    inverted_ranks = []
    retrieved_ids = [c["corpus_id"] for c in all_retrieved_chunks]
    key = False
    try:
        rank = retrieved_ids.index(true_chunk_id) + 1
        key = True
    except ValueError:
        logger.debug(f"ValueError: {true_chunk_id} not found in retrieved_ids")

    if key:
        inverted_ranks.append(1 / rank)
    else:
        inverted_ranks.append(0)

    return inverted_ranks


# load json file
path = "./data/retrieved_chunks_jinaemb_v3.json"
with open(path, "r", encoding="utf-8") as json_file:
    question_relevant_chunks = json.load(json_file)

HITS = []
INVERTED_RANKS = []
RERANKING_TIME = []


start = time.time()

for row in tqdm(question_relevant_chunks, desc="Computing metrics"):
    question = row["instruction"] + row["query"]  # get the question
    true_chunk_id = row["response_id"]  # get the relevant chunks.
    all_retrieved_chunks = row["all_retrieved_chunks"]  # get the all retrieved chunks

    if evaluate_with_reranking:
        s_rerank_time = time.time()
        all_retrieved_chunks = reranker.rerank(
            query=question, chunks=all_retrieved_chunks, top_k=5
        )  # permute the chunks' positions
        e_rerank_time = time.time()
        RERANKING_TIME.append(e_rerank_time - s_rerank_time)

    HITS.extend(compute_hits(true_chunk_id, all_retrieved_chunks[:5]))
    INVERTED_RANKS.extend(
        compute_inverted_ranks(true_chunk_id, all_retrieved_chunks[:5])
    )

end = time.time()

hit_rate = np.array(HITS).mean()
Mrr = np.array(INVERTED_RANKS).mean()
reranking_time = np.array(RERANKING_TIME).mean() if RERANKING_TIME else 0

print(f"Hit Rate: {hit_rate}")
print(f"MRR: {Mrr}")
print(f"Total time taken: {end - start} seconds")
print(f"Reranking time: {reranking_time} seconds")
