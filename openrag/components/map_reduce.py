from langchain_core.documents.base import Document
from openai import AsyncOpenAI
from tqdm.asyncio import tqdm
from utils.logger import get_logger

from .utils import llmSemaphore

logger = get_logger()

system_prompt_map = """
Vous êtes un modèle de langage spécialisé dans l’analyse et la synthèse d’informations. Ton rôle est d’examiner un texte fourni et d’en extraire les éléments nécessaires pour répondre à une question utilisateur.
Analyse le texte en profondeur.
Synthétise les informations essentielles qui peuvent aider à répondre à la requête.
Si le texte ne contient aucune donnée pertinente pour répondre à la question, réponds simplement : "Not pertinent" et n'ajoute pas de commentaires.
"""


system_prompt_reduce = """
Vous êtes un assistant conversationnel IA spécialisé dans la recherche et la synthèse d'informations. Votre objectif est de fournir des réponses précises, fiables et bien structurées en utilisant exclusivement les documents récupérés (Contexte). Priorisez la clarté et l'exactitude dans vos réponses.
Voici les règles à suivre :
- Répondez dans la langue de la requête de l'utilisateur.
- Utilisez uniquement les informations contenues dans le Contexte. Ne faites jamais d'inférences, de suppositions ou ne vous basez pas sur des connaissances externes.
- Si le contexte est insuffisant, invitez l'utilisateur à préciser sa requête ou à fournir des mots-clés supplémentaires.
- Soyez concis mais complet, en veillant à ne pas omettre d'informations importantes.
"""

user_prompt_reduce = """
Requête utilisateur : 
{query}

Informations récupérées : 
{context}
"""


class RAGMapReduce:
    def __init__(self, config):
        self.config = config
        self.client = AsyncOpenAI(
            base_url=self.config.llm["base_url"], api_key=self.config.llm["api_key"]
        )
        self.model = self.config.llm["model"]

    async def infer_llm_map(self, query, chunk: Document):
        async with llmSemaphore:
            user_prompt_map = (
                "Voici un texte :\n" + chunk.page_content + "\n"
                "À partir de ce document, identifie et résume de manière complète les informations utiles pour répondre à la question suivante :\n"
                + query
            )
            response = await self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt_map},
                    {"role": "user", "content": user_prompt_map},
                ],
                stream=False,
                max_tokens=512,
                temperature=0.3,
            )
            resp = response.choices[0].message.content.strip()
            relevancy = "Not pertinent" not in resp
            return relevancy, resp

    async def map(self, query: str, chunks: list[Document]):
        logger.debug("Running map reduce", chunk_count=len(chunks), query=query)
        tasks = [self.infer_llm_map(query, chunk) for chunk in chunks]
        output = await tqdm.gather(
            *tasks, desc="MAP_REDUCE Processing chunks", total=len(chunks)
        )
        relevant_chunks_syntheses = [
            (synthesis, chunk)
            for chunk, (relevancy, synthesis) in zip(chunks, output)
            if relevancy
        ]
        logger.debug(
            "Map reduce completed",
            relevant_chunk_count=len(relevant_chunks_syntheses),
            query=query,
        )
        # final_response = await infer_llm_reduce("\n".join(syntheses))
        return relevant_chunks_syntheses


# async def infer_llm_reduce(text):
#     user_prompt_reduce = "Requête utilisateur :\n" + query
#     user_prompt_reduce += "\nInformations récupérées :\n" + text + "\n"
#     response = await client.chat.completions.create(
#         model=model,
#         messages=[
#             {"role": "system", "content": system_prompt_reduce},
#             {"role": "user", "content": user_prompt_reduce},
#         ],
#         stream=False,
#         max_tokens=1000,
#     )
#     return response


# async def queue_api_calls():
#     responses = await asyncio.gather(*[infer_llm_map(chunk) for chunk in chunks])
#     results = []
#     for _, response in enumerate(responses):
#         r = response.choices[0].message.content.strip()
#         if "Not pertinent" not in r:
#             results.append(r)
#             print(_)
#             print("synthèse:")
#             print(r)
#             print(10 * "----")
#     final_response = await infer_llm_reduce("\n".join(results))
#     print(10 * "----")
#     print("Résumé final:")
#     print(final_response.choices[0].message.content.strip())
#     # print(10 * '----')
#     # return final_response


# asyncio.run(queue_api_calls())
# end = time.time()
# print(end - start)
# # print(final_response.choices[0].message.content.strip())
