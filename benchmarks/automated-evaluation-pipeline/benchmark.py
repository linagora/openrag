import os
import asyncio
import json
import math
from typing import Literal, TypedDict
from loguru import logger
from openai import AsyncOpenAI
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from dotenv import load_dotenv
from tqdm.asyncio import tqdm
import pandas as pd

load_dotenv()


# Retrieving responses and document sources fom OpenRAG
async def retrieve_response_and_docs_openrag(
    query: str, partition: str, _base_url: str, semaphore: asyncio.Semaphore
):
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
            list_source_chunk_ids = [
                item["_id"] for item in json.loads(res.extra)["sources"]
            ]

            return response_llm, list_source_chunk_ids
        except Exception as e:
            logger.debug(f"Error fetching chunks and response: {e}")
            return None, []


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
    "model": os.environ.get("JUDGE_MODEL"),
    "base_url": os.environ.get("JUDGE_BASE_URL"),
    "api_key": os.environ.get("JUDGE_API_KEY"),
    "temperature": 0.2,
    "max_tokens": 1000,
    "top_p": 1.0,
    "frequency_penalty": 0.0,
    "presence_penalty": 0.0,
}


class CompletionEvaluationResponse(BaseModel):
    output: Literal[
        # "complete", "mostly_complete", "partially_complete", "incomplete"
        "complet", "presque complet", "partiellement complet", "incomplet"
    ] = Field(
        ...,
        description="Le résultat du juge LLM. Il peut être l'un des suivants : "
        "« complet », « presque complet », « partiellement complet », « incomplet »."
        "Cela indique dans quelle mesure la réponse générée correspond à la vraie réponse.",
        # "The output of the LLM judge. It can be one of the following: "
        # "'complete', 'mostly_complete', 'partially_complete', 'incomplete'. "
        # "This indicates how well the generated answer matches the true answer.",
    )


class PrecisionEvaluationResponse(BaseModel):
    output: Literal[
        # "Highly_precise", "mostly_precise", "low_precision", "imprecise"
        "Très précis", "plutôt précis", "faible précision", "imprécis"
    ] = Field(
        ...,
        description="Le résultat du juge LLM. Il peut être : "
        "« très précis », « plutôt précis », « faible précision », « imprécis ». "
        "Cela indique dans quelle mesure la réponse générée correspond à la vraie réponse.",
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

llm_completion_judge = ChatOpenAI(**llm_judge_settings).with_structured_output(
    CompletionEvaluationResponse
)
llm_precision_judge = ChatOpenAI(**llm_judge_settings).with_structured_output(
    PrecisionEvaluationResponse
)


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
            return response_for_completion.output, response_for_precision.output
        except Exception as e:
            logger.debug(f"Error evaluating response: {e}")
            return "error", "error"


class Element(TypedDict):
    question: str
    llm_answer: str
    chunks: list[dict]


async def main():
    with open("./dataset.json", "r", encoding="utf-8") as f:
        eval_dataset: list[Element] = json.load(f)

    list_response_answer_reference = eval_dataset  # [:10]

    num_port = os.environ.get("APP_PORT")
    num_host = os.environ["APP_URL"]
    openrag_api_base_url = f"http://{num_host}:{num_port}"
    partition = "terresunivia"

    # Create shared semaphores for rate limiting
    openrag_semaphore = asyncio.Semaphore(4)  # Limit concurrent OpenRAG requests
    judge_semaphore = asyncio.Semaphore(10)  # Limit concurrent judge requests

    # Create tasks for OpenRAG API calls
    tasks = [
        retrieve_response_and_docs_openrag(
            query=resp_ans_reference["question"],
            partition=partition,
            openrag_base_url=openrag_api_base_url,
            semaphore=openrag_semaphore,
        )
        for resp_ans_reference in list_response_answer_reference
    ]

    openrag_answer_chunk_ids_l = await tqdm.gather(*tasks, desc="Fetching")
    scores = []
    response_judge_tasks = []

    for (openrag_response, openrag_chunk_ids), input_reference in zip(
        openrag_answer_chunk_ids_l, list_response_answer_reference
    ):
        if openrag_response is None:
            continue
        chunk_id_reference = [c["id"] for c in input_reference["chunks"]]
        score = source_score_per_question(
            chunk_id_reference=chunk_id_reference, chunk_id_llm=openrag_chunk_ids
        )
        scores.append(score)

        # Create task with proper semaphore passing
        resp_eval_task = response_judgment_per_question(
            query=input_reference["question"],
            llm_answer=input_reference["llm_answer"],
            openrag_answer=openrag_response,
            semaphore=judge_semaphore,
        )
        response_judge_tasks.append(resp_eval_task)

    llm_judge_scores = await tqdm.gather(
        *response_judge_tasks, desc="Evaluating responses"
    )

    # Filter out error responses
    valid_scores = [(comp, prec) for comp, prec in llm_judge_scores if comp != "error"]
    valid_ndcg_scores = scores[: len(valid_scores)]  # Match the filtered scores

    eval_results = pd.DataFrame(
        valid_scores,
        columns=["completion_evaluation", "precision_evaluation"],
    )
    eval_results["nDCG"] = valid_ndcg_scores
    chunks_count = [
        len(input_reference["chunks"])
        for input_reference in list_response_answer_reference[: len(valid_scores)]
    ]
    eval_results["n_chunks"] = chunks_count

    # Calculate average nDCG for each n_chunks and round values to 3 decimal places
    avg_ndcg_per_chunk = (
        eval_results.groupby("n_chunks")["nDCG"].mean().round(3).to_dict()
    )
    print(f"Average nDCG per chunk count: {avg_ndcg_per_chunk}\n")
    print(
        f"Average nDCG: {round(eval_results['nDCG'].mean(), 3)} +/- {eval_results['nDCG'].std():.3f}"
    )

    # Print evaluation distributions
    print("\n", "-" * 50, "\n")
    print("\nCompletion evaluation distribution:")
    print(eval_results["completion_evaluation"].value_counts())
    print("\n", "-" * 50, "\n")
    print("\nPrecision evaluation distribution:")
    print(eval_results["precision_evaluation"].value_counts())


if __name__ == "__main__":
    asyncio.run(main())
