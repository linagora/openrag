# Sample evaluation dataset

`rag_dataset_sample.csv` is a ready-made test set for the admin **System →
Evaluation** tab: 11 questions over 6 documents, every answer checked against
the source PDF rather than generated.

The corpus itself is not committed — the documents are third-party PDFs
totalling ~36 MB. `corpus.txt` lists the 24 filenames, drawn from the internal
`rag_dataset` collection (French public-sector, agricultural, medical and AI
documents).

## Why 24 documents for 11 questions

Only 6 documents are the subject of a question. The other 18 are deliberate
distractors, each topically adjacent to a question's source — other AI-policy
papers, other gut/neuro medical papers, other agricultural press releases. A
corpus where every document is on a different subject makes retrieval look
better than it is: any half-working retriever scores a perfect hit rate when
there is only one candidate per topic.

## Assembling it

```bash
mkdir -p /tmp/eval-corpus
while IFS= read -r f; do cp "<path-to>/rag_dataset/$f" /tmp/eval-corpus/; done \
  < tests/evaluation/corpus.txt
```

Then upload it, either through the Evaluation tab or the API:

```bash
args=(); for f in /tmp/eval-corpus/*; do args+=(-F "corpus=@${f}"); done
curl -X POST "$OPENRAG_URL/evaluation/datasets" \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -F "name=rag_dataset sample" \
  -F "testset=@tests/evaluation/rag_dataset_sample.csv" \
  "${args[@]}"
```

## Test set format

`question,expected_answer,expected_file_ids` — `expected_file_ids` is optional
and semicolon-separated. Name the files as they appear on disk; the indexer
sanitises the id it stores (spaces are not valid in a `file_id`), and the
ranking metrics match against the original filename in the chunk metadata.

Rows without `expected_file_ids` still count toward answer quality, but are
reported as `skipped_cases` in hit rate / MRR / recall rather than scored as
misses.
