import asyncio
import json
import math
import os
from typing import TypedDict

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from loguru import logger
from openai import AsyncOpenAI
from pydantic import BaseModel, Field
from tqdm.asyncio import tqdm

load_dotenv()


# Retrieving responses and document sources fom OpenRAG
async def retrieve_response_and_docs_openrag(query: str, partition: str, _base_url: str, semaphore: asyncio.Semaphore):
    async with semaphore:
        base_url = f"{_base_url}/v1"
        auth_key = "sk-1234"
        client = AsyncOpenAI(api_key=auth_key, base_url=base_url)

        settings = {
            "model": f"openrag-{partition}",
            "messages": [
                {
                    "role": "user",
                    "content": query,
                }
            ],
            "temperature": 0.2,
            "stream": False,
            "timeout": 120,
        }

        try:
            res = await client.chat.completions.create(**settings)
            response_llm = res.choices[0].message.content
            list_source_chunk_ids = [item["_id"] for item in json.loads(res.extra)["sources"]]

            return response_llm, list_source_chunk_ids
        except Exception as e:
            logger.debug(f"Error fetching chunks and response: {e}")
            return None, []


def compute_hits(true_chunk_id, all_retrieved_chunks):
    return true_chunk_id in all_retrieved_chunks


def compute_inverted_ranks(true_chunk_id, all_retrieved_chunks):
    # see link: https://chatgpt.com/share/6813f998-2e88-8002-a472-6af2e9a64b61
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


# Sources retrieval evaluation
def relevance(val, true_chunk_ids):
    return 1 if val in true_chunk_ids else 0


def source_score_per_question(
    chunk_id_reference: list[int],
    chunk_id_llm: list[int],
):
    val_DCG = 0
    for i, val in enumerate(chunk_id_llm):
        val_DCG += relevance(val, chunk_id_reference) / math.log2(i + 2)
    iDCG = 0.0000001
    for i in range(min(len(chunk_id_reference), len(chunk_id_llm))):
        iDCG += 1 / math.log2(i + 2)
    return val_DCG / iDCG


# Response retrieval evaluation
llm_judge_settings = {
    "model": os.environ.get("MODEL"),
    "base_url": os.environ.get("BASE_URL"),
    "api_key": os.environ.get("API_KEY"),
    "temperature": 0.2,
    "max_tokens": 1000,
    "top_p": 1.0,
    "frequency_penalty": 0.0,
    "presence_penalty": 0.0,
}


class CompletionEvaluationResponse(BaseModel):
    score: int = Field(
        ...,
        ge=1,
        le=10,
        description="Le résultat du juge LLM."
        "Le résultat est compris entre 1 et 10, où 1 indique une réponse très incomplète et 10 indique une réponse très complète.",
        # "The output of the LLM judge. It can be one of the following: "
        # "'complete', 'mostly_complete', 'partially_complete', 'incomplete'. "
        # "This indicates how well the generated answer matches the true answer.",
    )


class PrecisionEvaluationResponse(BaseModel):
    score: int = Field(
        ...,
        ge=1,
        le=10,
        description="Le résultat du juge LLM."
        "Le résultat est compris entre 1 et 10, où 1 indique une réponse très imprécise et 10 indique une réponse très précise.",
        # "The output of the LLM judge. It can be one of the following: "
        # "'Highly_precise', 'mostly_precise', 'low_precision', 'imprecise'. "
        # "This indicates how well the generated answer matches the true answer.",
    )


# complettion_judge_prompt = """You are an expert judge evaluating the completeness of a language model's response to a question.
# Given:
#     A query (query)
#     The correct or reference answer (true_answer)
#     A generated response (generated_answer)
# Your task is to assess how complete the generated_answer is in relation to the true_answer. Focus on whether the generated response fully covers, partially covers, or omits important elements found in the true_answer.
# Consider:
#     Does the response address all key points in the true_answer?
#     Are there any significant omissions or gaps?
#     Is the response thorough or only partial?"""

# precision_judge_prompt = """You are an expert judge evaluating the precision of a language model's response to a question.
# Given:
#     A query (query)
#     The correct or reference answer (true_answer)
#     A generated response (generated_answer)
# Your task is to assess how precisely the generated_answer aligns with the true_answer. Focus on whether the generated response contains only accurate, relevant, and specific information, without unnecessary or incorrect additions.
# Consider:
#     Does the response stay focused on what was asked?
#     Does it avoid unrelated, vague, or incorrect information?
#     Is the content specific and factually aligned with the true_answer?
# """

complettion_judge_prompt = """Vous êtes un expert chargé d'évaluer l'exhaustivité de la réponse d'un modèle de langage à une question.
Étant donné :
Une requête (query)
La réponse correcte ou de référence (true_answer)
Une réponse générée (generated_answer)
Votre tâche consiste à évaluer l'exhaustivité de la generated_answer par rapport à la true_answer. Considérez si la réponse générée couvre entièrement, partiellement ou omet des éléments importants de la true_answer.
Considérez :
La réponse aborde-t-elle tous les points clés de la true_answer ?
Y a-t-il des omissions ou des lacunes importantes ?
La réponse est-elle complète ou seulement partielle ?"""

precision_judge_prompt = """Vous êtes un juge expert évaluant la précision de la réponse d'un modèle de langage à une question.
Étant donné :
Une requête (query)
La réponse correcte ou de référence (true_answer)
Une réponse générée (generated_answer)
Votre tâche consiste à évaluer la précision avec laquelle la réponse générée correspond à la vraie réponse. Vérifiez si la réponse générée contient uniquement des informations exactes, pertinentes et spécifiques, sans ajouts inutiles ou incorrects.
Considérez :
La réponse reste-t-elle centrée sur la question ?
Évite-t-elle les informations sans rapport, vagues ou incorrectes ?
Le contenu est-il précis et conforme aux faits à la vraie réponse ?"""

llm_completion_judge = ChatOpenAI(**llm_judge_settings).with_structured_output(CompletionEvaluationResponse)
llm_precision_judge = ChatOpenAI(**llm_judge_settings).with_structured_output(PrecisionEvaluationResponse)


async def response_judgment_per_question(
    query: str,
    llm_answer: str,
    openrag_answer: str,
    semaphore: asyncio.Semaphore,
):
    s = f"""Voici les détails nécessaires pour évaluer la réponse d'un modèle de langage (LLM) à une question :
    query: {query}
    true_answer: {openrag_answer}
    generated_answer: {llm_answer}
    """
    async with semaphore:
        try:
            response_for_completion = await llm_completion_judge.ainvoke(
                [
                    {"role": "system", "content": complettion_judge_prompt},
                    {"role": "user", "content": s},
                ]
            )

            response_for_precision = await llm_precision_judge.ainvoke(
                [
                    {"role": "system", "content": precision_judge_prompt},
                    {"role": "user", "content": s},
                ]
            )
            return response_for_completion.score, response_for_precision.score
        except Exception as e:
            logger.debug(f"Error evaluating response: {e}")
            return "error", "error"


class Element(TypedDict):
    question: str
    llm_answer: str
    chunks: list[dict]


async def main():
    with open("./dataset.json", encoding="utf-8") as f:
        eval_dataset: list[Element] = json.load(f)

    list_response_answer_reference = eval_dataset  # [:10]

    num_port = os.environ.get("APP_PORT")
    num_host = os.environ["APP_URL"]
    openrag_api_base_url = f"http://{num_host}:{num_port}"
    partition = "pdftest"  # To replace with your wanted partition's name

    # Create shared semaphores for rate limiting
    openrag_semaphore = asyncio.Semaphore(4)  # Limit concurrent OpenRAG requests
    judge_semaphore = asyncio.Semaphore(10)  # Limit concurrent judge requests

    # Create tasks for OpenRAG API calls
    tasks = [
        retrieve_response_and_docs_openrag(
            query=resp_ans_reference["question"],
            partition=partition,
            _base_url=openrag_api_base_url,
            semaphore=openrag_semaphore,
        )
        for resp_ans_reference in list_response_answer_reference
    ]

    openrag_answer_chunk_ids_l = await tqdm.gather(*tasks, desc="Fetching")
    hit_rates, MRRs, recalls, nDCG_scores = [], [], [], []
    response_judge_tasks = []

    for (openrag_response, openrag_chunk_ids), input_reference in zip(
        openrag_answer_chunk_ids_l, list_response_answer_reference
    ):
        if openrag_response is None:
            continue
        chunk_id_reference = [c["id"] for c in input_reference["chunks"]]  # The "true answer" ids list

        # Hit rate and MRR
        hit_rates.append(compute_hits(chunk_id_reference[0], openrag_chunk_ids))
        MRRs.append(compute_inverted_ranks(chunk_id_reference[0], openrag_chunk_ids))

        # Recall computaton
        recall = len(list(set(chunk_id_reference) & set(openrag_chunk_ids))) / len(chunk_id_reference)
        recalls.append(recall)

        # nDCG score calculation
        nDCG_score = source_score_per_question(chunk_id_reference=chunk_id_reference, chunk_id_llm=openrag_chunk_ids)
        nDCG_scores.append(nDCG_score)

        # Create task with proper semaphore passing
        resp_eval_task = response_judgment_per_question(
            query=input_reference["question"],
            llm_answer=input_reference["llm_answer"],
            openrag_answer=openrag_response,
            semaphore=judge_semaphore,
        )
        response_judge_tasks.append(resp_eval_task)

    llm_judge_scores = await tqdm.gather(*response_judge_tasks, desc="Evaluating responses")

    # Score display
    print(f"Average Hit Rate: {round(np.array(hit_rates).mean(), 3)}")
    print(f"Average MRR: {round(np.array(MRRs).mean(), 3)}")
    print(f"Average Recall: {round(np.array(recalls).mean(), 3)}")

    # Filter out error responses
    valid_scores = [(comp, prec) for comp, prec in llm_judge_scores if comp != "error"]
    valid_ndcg_scores = nDCG_scores[: len(valid_scores)]  # Match the filtered scores

    eval_results = pd.DataFrame(
        valid_scores,
        columns=["completion_evaluation", "precision_evaluation"],
    )
    eval_results["nDCG"] = valid_ndcg_scores
    chunks_count = [
        len(input_reference["chunks"]) for input_reference in list_response_answer_reference[: len(valid_scores)]
    ]
    eval_results["n_chunks"] = chunks_count

    # Calculate average nDCG for each n_chunks and round values to 3 decimal places
    avg_ndcg_per_chunk = eval_results.groupby("n_chunks")["nDCG"].mean().round(3).to_dict()
    print(f"Average nDCG per chunk count: {avg_ndcg_per_chunk}\n")
    print(f"Average nDCG: {round(eval_results['nDCG'].mean(), 3)} +/- {eval_results['nDCG'].std():.3f}")

    # Print evaluation distributions
    print("\n", "-" * 50, "\n")
    print("\nCompletion evaluation distribution:")
    print(eval_results["completion_evaluation"].value_counts())
    print(f"Completion evaluation average: {eval_results['completion_evaluation'].mean():.3f}")
    print("\n", "-" * 50, "\n")
    print("\nPrecision evaluation distribution:")
    print(eval_results["precision_evaluation"].value_counts())
    print(f"Precision evaluation average: {eval_results['precision_evaluation'].mean():.3f}")


if __name__ == "__main__":
    asyncio.run(main())
