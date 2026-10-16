# Retrieval comparison summary

These measurements are exploratory. They do not establish a retrieval-quality or capacity improvement for the current PR head.

| Run | Result | Limitation |
|---|---|---|
| October 12 API comparison | Median TTFT was 3.82 s on develop and 4.17 s on the candidate (+9.2%). On three new questions, source recall was 6/6 on develop and 5/6 on the candidate. | Three questions and nine samples per revision; this was an earlier candidate revision. |
| October 13 chunk comparison | On 12 SciFact questions, full chunk recall was 10/12 on develop and 12/12 on the candidate. For questions requiring three chunks, it was 3/5 and 5/5; both found all seven two-chunk cases. | One run per question. The candidate artifact came from dirty revision `a2493ecf`; its recorded source hashes do not match that revision. |

The chunk run used 5,183 title-and-abstract chunks, the same frozen embeddings, 50 dense candidates, GTE reranking before RRF, and a five-chunk final budget. It used exact dense cosine search, not Milvus hybrid search or the full API answer path. Measured contextualizer input rose from 2,758 to 3,917 tokens (+42%); median retrieval plus generation rose from 1.40 s to 2.25 s. These small-sample figures are not capacity estimates.

Several 512-token output examples retained their requested items and date range, but they do not guarantee that every long request fits that budget.

**Conclusion:** keep these numbers as exploratory context only. Before making a merge or rollout quality claim, rerun from a clean final PR revision with matching corpus, questions, settings, and embeddings; record generated sub-queries and chunk-level results, and measure the production retrieval path.
