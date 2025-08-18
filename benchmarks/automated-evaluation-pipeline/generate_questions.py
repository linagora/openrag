import random
import asyncio
import os
import httpx
import json
import time
import numpy as np
import ast

from dotenv import load_dotenv
from loguru import logger
from langchain_openai import ChatOpenAI
from tqdm.asyncio import tqdm
import umap.umap_ as umap
import hdbscan
import pickle

load_dotenv()  # Charge les variables du .env

BASE_URL = os.environ["BASE_URL"]
API_KEY = os.environ["API_KEY"]
MODEL = os.environ["MODEL"]

settings = {
    "temperature": 0.2,
    "max_retries": 3,
    "timeout": 60,
    "base_url": BASE_URL,
    "model": MODEL,
    "api_key": API_KEY,
    "max_tokens": 2048,
}

llm = ChatOpenAI(**settings).with_retry(stop_after_attempt=2)


# question_tmpl = """You are a question generation assistant. I will give you multiple related paragraphs.
# Your task is to generate a **single meaningful question** that reflects the **combined information and logical flow** of all the paragraphs.

# Make sure not to focus only on one paragraph. Capture the connection between them.
# """

# answer_tmpl = """You are an expert in answering questions based on given documents.
# Given a question and a set of text documents, your task is to provide a comprehensive answer that utilizes all of the provided documents.
# The answer should be clear, and directly address the question using the information from the documents.
# The output should only be the answer, without any additional text or explanation."""

question_tmpl = """Vous êtes un assistant de génération de questions. Je vous fournirai plusieurs paragraphes liés.
Votre tâche consiste à générer une **question unique et pertinente** reflétant **l'ensemble des informations et le flux logique** de tous les paragraphes.

Veillez à ne pas vous concentrer sur un seul paragraphe. Saisissez le lien entre eux."""

answer_tmpl = """Vous êtes expert dans la réponse aux questions basées sur des documents.
À partir d'une question et d'un ensemble de documents, votre tâche consiste à fournir une réponse complète en utilisant tous les documents fournis.
La réponse doit être claire et répondre directement à la question en utilisant les informations des documents.
Le résultat doit être la réponse seule, sans texte ni explication supplémentaire."""


async def summarize(
    chunk: str, semaphore: asyncio.Semaphore = asyncio.Semaphore(10)
) -> str:
    async with semaphore:
        message = [
            {
                "role": "user",
                "content": f"Voici le document:\n{chunk}. Donnez-moi un résumé qui précise quel type d'informations et de contenu contient le passage, mais sans entrer dans des détails trop précis",
            }
        ]

        output = await llm.ainvoke(message)
        return output.content.strip()


async def format_chunks(chunks: list[str]):
    chunks_str = ""
    for i, chunk in enumerate(chunks, start=1):
        # chunk = await summarize(chunk)
        chunks_str += f"Document {i}:\n{chunk}\n"
        chunks_str += "-" * 40 + "\n"
    return chunks_str.strip()  # Remove trailing newline and spaces


async def question_answer(chunks: list[dict], semaphore=asyncio.Semaphore(10)):
    async with semaphore:
        chunks_str = await format_chunks([c["text"] for c in chunks])

        # generate a question based on the chunks
        messages = [
            {"role": "system", "content": question_tmpl},
            {
                "role": "user",
                "content": f"Voici les documents:\n{chunks_str}. Créez maintenant une question cohérente, pertinente. Cette question ne doit pas dépasser 25 mots de longueur.",
            },
        ]
        output = await llm.ainvoke(messages)
        llm_question = output.content.strip()

        # generate an answer based on the question and chunks
        messages = [
            {"role": "system", "content": answer_tmpl},
            {
                "role": "user",
                "content": f"Voici les documents:\n{chunks_str}\n\nQuestion: {llm_question}.\n Générez la réponse dans la même langue que les documents.",
            },
        ]
        output = await llm.ainvoke(messages)
        llm_answer = output.content.strip()
        return {"question": llm_question, "chunks": chunks, "llm_answer": llm_answer}


async def get_all_chunks(url: str) -> dict:
    retries = 3
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                all_chunks_list = resp.json()["chunks"]
            if not all_chunks_list:
                raise ValueError("No chunks found.")
            return all_chunks_list
        except Exception as e:
            logger.debug(f"Attempt {attempt + 1} failed: {e}")
            if attempt < retries - 1:
                await asyncio.sleep(1)  # Wait before retrying
            else:
                logger.debug(f"Error fetching chunks after {retries} attempts: {e}")
                return None


async def generate_questions_from_clusters(
    clusters: dict, n_min=1, n_max=2, n_questions_per_cluster=10
):
    tasks = []
    for cluster_label, chunks in clusters.items():  # cluster loop
        for _ in range(n_questions_per_cluster):
            n = random.randint(n_min, min(n_max, len(chunks)))
            sampled_chunks = random.sample(chunks, n)
            task = question_answer(chunks=sampled_chunks)
            tasks.append(task)

    questions_and_answers = await tqdm.gather(
        *tasks, desc="Question and Answer Generation..."
    )
    return questions_and_answers


async def main():
    num_port = os.environ.get("APP_PORT")
    num_host = os.environ["APP_URL"]
    openrag_api_base_url = f"http://{num_host}:{num_port}"
    partition = "terresunivia"
    url = f"{openrag_api_base_url}/partition/{partition}/chunks"

    start = time.time()
    all_chunks_list = await get_all_chunks(url)
    pause = time.time()
    logger.info(f"Clusters retrieval time: {pause - start} seconds")

    ids, chunk_contents, chunk_embeddings, file_ids = map(
        list,
        zip(
            *[
                (
                    chunk["metadata"]["_id"],
                    chunk["content"],
                    chunk["metadata"]["vector"],
                    chunk["metadata"]["file_id"],
                )
                for chunk in all_chunks_list
            ]
        ),
    )

    embeddings = np.array(list(map(ast.literal_eval, chunk_embeddings)))

    reducer = umap.UMAP(n_neighbors=15, n_components=5, min_dist=0.1, metric="cosine")
    embeddings_reduced = reducer.fit_transform(embeddings)

    hdb = hdbscan.HDBSCAN(min_cluster_size=5, metric="euclidean")
    labels = hdb.fit_predict(embeddings_reduced)

    clusters = {}
    for idx, label in enumerate(labels):
        if label == -1:
            continue  # -1 == bruit
        clusters.setdefault(int(label), []).append(
            {
                "id": ids[idx],
                "text": chunk_contents[idx],
                "file_id": file_ids[idx],
            }
        )

    for label, items in clusters.items():
        logger.info(f"Cluster {label}: {[item['id'] for item in items]}")

    # # save data
    # os.makedirs("./data", exist_ok=True)
    # pickle.dump(
    #     clusters, open("./data/chunks_cluster.pkl", "wb"), protocol=pickle.HIGHEST_PROTOCOL
    # )
    questions = await generate_questions_from_clusters(
        clusters, n_questions_per_cluster=1
    )

    logger.info(f"Questions generated time: ({time.time() - pause}) seconds")

    with open("./dataset.json", "w", encoding="utf-8") as f:
        json.dump(questions, f, ensure_ascii=False, indent=4)


if __name__ == "__main__":
    asyncio.run(main())


# d = {}
# for i, chunk in enumerate(all_chunks_list):
#     content = chunk["content"]
#     metadata = chunk["metadata"]
#     metadata.pop("vector", None)  # Remove vector from metadata

#     d[i] = {"content": content, "metadata": metadata}


# # save data
# os.makedirs("./data", exist_ok=True)
# pickle.dump(
#     d, open("./data/chunks_data.pkl", "wb"), protocol=pickle.HIGHEST_PROTOCOL
# )
# pickle.dump(
#     embeddings,
#     open("./data/chunks_embeddings.pkl", "wb"),
#     protocol=pickle.HIGHEST_PROTOCOL,
# )
