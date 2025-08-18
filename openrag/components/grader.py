import asyncio
from typing import Literal

from langchain_core.documents.base import Document
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field

from .utils import llmSemaphore

sys_prompt = """You are a seasoned expert in assessing document relevance. Your task is to evaluate documents carefully against a user's query by considering their semantics, context, and keyword significance. Your expert judgment ensures that only truly pertinent documents are flagged as relevant."""


class DocumentGrade(BaseModel):
    """
    Evaluates a document's relevance with respect to a user's query.

    This model guides you to assess whether a document is pertinent by analyzing:
      - Semantic alignment with the query
      - Contextual relevance
      - Presence and significance of key terms

    The evaluation assigns one of two scores:
      - "highly_relevant": The document meaningfully addresses the query.
      - "irrelevant": The document does not adequately address the query.

    Use this framework to ensure that only documents with strong relevance pass the evaluation.
    """

    relevance_score: Literal["highly_relevant", "irrelevant"] = Field(
        description="Classification of document relevance based on semantic and contextual analysis."
    )


class Grader:
    def __init__(self, config, logger=None):
        llm: ChatOpenAI = ChatOpenAI(**config.llm)
        self.sllm = llm.with_structured_output(DocumentGrade)
        self.logger = logger

    async def _grade_doc(self, user_input, doc: Document, semaphore=llmSemaphore):
        async with semaphore:
            try:
                query_template = (
                    """User query: {user_input}\n"""
                    """Retrieved Document: {content}"""
                )

                template = ChatPromptTemplate.from_messages(
                    [("system", sys_prompt), ("user", query_template)]
                )
                # Create a PromptValue from the template
                prompt_value = template.invoke(
                    {"user_input": user_input, "content": doc.page_content}
                )
                result: DocumentGrade = await self.sllm.ainvoke(prompt_value)

                return result.relevance_score
            except Exception as e:
                self.logger.debug(
                    f"An Exception occured. Couldn't grade this document: {e}"
                )

    async def grade_docs(self, user_input: str, docs: list[Document], batch_size=6):
        """
        Grades a list of documents based on their relevancy to the user input.

        Args:
            user_input (str): The input string provided by the user.
            docs (list[Document]): A list of Document objects to be graded.
            batch_size (int, optional): The number of documents to process in a batch. Defaults to 6.

        Returns:
            list[Document]: A list of relevant Document objects.
        """
        batch_size = min(batch_size, len(docs))
        self.logger.debug("Documents to assess relevancy", document_count=len(docs))

        tasks = [self._grade_doc(user_input=user_input, doc=d) for d in docs]
        grades: list[DocumentGrade] = await asyncio.gather(*tasks)

        # Filter out irrelevant documents
        relevant_docs = list(
            filter(lambda doc_grade: doc_grade[1] != "irrelevant", zip(docs, grades))
        )
        relevant_docs = [doc for doc, _ in relevant_docs]
        self.logger.debug("Relevant documents found", document_count=len(relevant_docs))
        return relevant_docs
