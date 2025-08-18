# Extraction and generation benchmark

This folder is built to check OpenRAG's ability to extract the relevant documents and also the responses. The questions are generated based from the sources sharing the same topic.

Here are the steps to follow in order to reproduce the benchmark.

Relocate the relative path to this folder


```bash
cd benchmarks/automated-evaluation-pipeline
```

## Inserting files into the vectorize database
This will send all the files in your `pdf_files/` folder to your OpenRAG via API method (and they will be indexed automatically), so make sure that your OpenRAG is turned on first.

```bash
python upload_files.py
```

You can name the partition in the input.

## Organise all the chunks into clusters
We use the endpoint `chunks` to retrieve all the available chunks from a partition, then to organise them all into clusters (with the helps from `Umap` and `HDBSCAN`). 

For each cluster, we take a certain number of combinaisons of chunks (varies from 1 to 3) and generate questions with LLM's help (the model that you are also using for OpenRAG).

```bash
python generate_questions.py
```

All the questions are then stores in `dataset.json` with such format:

```bash
{
    "question": "Comment les conditions climatiques et les défis géopolitiques ont-ils influencé les récoltes de pois, de féveroles et d'autres protéagineux en France entre 2020 et 2022 ?",
    "chunks": [
        {
            "id": 458974149490248568,
            "text": ...,
            "file_id": "note-aux-operateurs-422.pdf"
        },
        ...
    ],
    "llm_answer": 
},
```

## Evaluation: Documents retrieval and responses

Using an OpenAI compatible client, we can retrieve the response and also the relevant documents.

Thus, by comparing the chunks' id with those stored in the .json file, we can get the nDCG score of the retrieval. For the reponse produced by the LLM, we use an LLM as a judge, to ask if the content generated has provided quality and sufficient amount of informaton or not.

/// Explain nDCG

Finally, we get the score by taking the mean value and also regroup the evaluations of the LLM. 

```bash
python benchmark.py
```
