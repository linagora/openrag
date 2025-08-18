* https://www.analyticsvidhya.com/blog/2024/07/hit-rate-mrr-and-mmr-metrics/
* https://www.evidentlyai.com/ranking-metrics/precision-recall-at-k

## Hit Rate
**`Definition`**: Le hit rate, c’est le pourcentage de fois où on obtient le bon chunk parmi toutes les chunks récupérés.

![alt text](./assets/image-1.png)
![alt text](./assets/image.png)

> Pb with Hit Rate: Cela ne prend pas en compte la position du chunk pertinent parmi ceux récupérés. Dans l'exemple suivant on a 2 retrievers qui ont le même hit rate et pourtant le retriever 2 classe mieux les documents pertinents  (à la première position) et clairement ce retriever serait plus preferable: C'est là que le MRR (Mean Reciprocal Rank) est considéré
![alt text](./assets/image-2.png)

## MRR
**`Definition`**: Le MRR, indique à quelle position moyenne se trouve la première bonne réponse.

> **`Interprétation`** :
* MRR proche de 1: le bon résultat est souvent en tête.
* MRR proche de 0: le bon résultat est rarement trouvé ou très bas dans la liste.

![alt text](./assets/image-3.png)
![alt text](./assets/image-4.png)

# Benchmark Pipeline

## 1. Create the folder for the benchmark task

```bash
mkdir your_benchmark_folder
cd your_benchmark_folder
```

## 2. Data preparation

Create a sub folder for your data files
```bash
mkdir data
```

Look for a text retrieval task dataset online (like [HuggingFace](https://huggingface.co/datasets?task_categories=task_categories:text-retrieval&sort=trending)), then download and store them in form of .csv in ./data/ like in the 2 examples folders.

Copy all the necessary .py files
```bash
cp ../benchmark-with-reference/*.py .
```

## 3. Run the benchmark

First, make sure your OpenRAG is running with docker compose.
Then, by running the index_data.py file, you will index all the data source that you downloaded online into OpenRAG.
```bash 
python index_data.py
```

Next, create a json file with complete informations, such as questions, the id of the responses and the metadata of all the reponses found by the model. (take a look at /home/ubuntu/an/OpenRAG-an/evaluation-embedder-reranker/benchmark-with-reference/data/retrieved_chunks_paraphase_MiniLM_L12.json as an example). Make sure that your .csv files have appropriate structure.

```bash
python generate_dataset.py
```

Lastly, run the compute_metrics.py file to have the metric scores of the dataset (here we have hit rate and MRR).