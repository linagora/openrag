import json
import numpy as np
import os
from loguru import logger
from tqdm import tqdm
from reranker import Reranker
import math
import time

# load the model
evaluate_with_reranking = True

# model_name = "jinaai/jina-colbert-v2"
# reranker_type = "colbert"

# model_name = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
# reranker_type = "crossencoder"

# model_name = "jinaai/jina-reranker-v2-base-multilingual"
# reranker_type = "crossencoder"

model_name = "Alibaba-NLP/gte-multilingual-reranker-base"
reranker_type = "crossencoder"

reranker = Reranker(reranker_type=reranker_type, model_name=model_name)

def relevance(val, true_chunk_ids):
    return 1 if val in true_chunk_ids else 0

def compute_nDCG(true_chunk_ids: list[str], all_retrieved_chunks: list[dict]):
    val_DCG = 0
    for i, val in enumerate(all_retrieved_chunks):
        val_DCG += relevance(val["corpus_id"], true_chunk_ids) / math.log2(i + 2)
    iDCG = 0
    for i in range(min(len(true_chunk_ids), len(all_retrieved_chunks))):
        iDCG += 1 / math.log2(i + 2)
    return [val_DCG / iDCG]

# load json file
embedder = os.environ.get('EMBEDDER_MODEL_NAME').split("/")[-1]
path = f"./data/retrieved_chunks_{embedder}.json"
with open(path, "r", encoding="utf-8") as json_file:
    question_relevant_chunks = json.load(json_file)

RERANKING_TIME = []
NDCG = []


start = time.time()

for row in tqdm(question_relevant_chunks, desc="Computing metrics"):
    question = row["text"]  # get the question
    true_chunk_ids = row["response_id"]  # get the relevant chunks.
    all_retrieved_chunks = row["all_retrieved_chunks"]  # get the all retrieved chunks

    if evaluate_with_reranking:
        s_rerank_time = time.time()
        all_retrieved_chunks = reranker.rerank(
            query=question, chunks=all_retrieved_chunks, top_k=5
        )  # permute the chunks' positions
        e_rerank_time = time.time()
        RERANKING_TIME.append(e_rerank_time - s_rerank_time)

    NDCG.extend(compute_nDCG(true_chunk_ids, all_retrieved_chunks))
end = time.time()

nDCG = np.array(NDCG).mean()
reranking_time = np.array(RERANKING_TIME).mean() if RERANKING_TIME else 0

print(f"nDCG: {nDCG}")
print(f"Total time taken: {end - start} seconds")
print(f"Reranking time: {reranking_time} seconds")