---
title: Environment Variable Configuration
---

# Overview
OpenRAG provides a large range of environment variables that allow you to customize and configure various aspects of the application. This page serves as a comprehensive reference for all available environment variables, providing their types, default values, and descriptions. As new variables are introduced, this page will be updated to reflect the growing configuration options.

:::note
This page is up-to-date with OpenRAG release version v.1.1.2 but is still a work in progress to later include more accurate descriptions, listing out options available for environment variables, defaults, and improving descriptions.
:::

# Backend
## Indexer Pipeline
### Loaders
Openrag loads all files into a pivot markdown file format before proceeding to chunking. Some environment variables can be configured to customized this pipeline

#### General variables

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `IMAGE_CAPTIONING` | `bool` | `true` | If `true`, an LLM is used to describe images and convert them into text using a [specific prompt](https://github.com/linagora/openrag/blob/main/prompts/example1/image_captioning_tmpl.txt). The image in files are replaced by their descriptions |
| `SAVE_MARKDOWN` | `bool` | `false` | If `true`, the pivot-format markdown produced during parsing is saved. Useful for debugging and verifying the correctness of the generated markdown. |
|`SAVE_UPLOADED_FILES`|`bool`|`false`| When `true`, uploaded files are stored on disk. You must enable this option if you want Chainlit to show sources while chatting.|
| `PDFLoader` | `str` | `MarkerLoader` | Specifies the PDF parsing engine to use. Available options: `PyMuPDFLoader`, `PyMuPDF4LLMLoader`, `MarkerLoader` and `DotsOCRLoader`.|

:::caution
`PyMuPDFLoader` and `PyMuPDF4LLMLoader` are lightweight pdf loaders that cannot process non-searchable (image-based) PDFs and do not extract or handle embedded images.
:::

#### PDF Loader
##### Marker Loader Configuration
The `MarkerLoader` is the default PDF parsing engine. It can be configured using the following environment variables:

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `MARKER_POOL_SIZE` | int | 1 | Number of workers (typically 1 worker per cluster node) |
| `MARKER_MAX_PROCESSES` | int | 2 | Number of subprocesses <-> Number of concurrent PDFs per worker (to increase depending on your available GPU resources)|
| `MARKER_MAX_TASKS_PER_CHILD` | int | 10 | Number of tasks a child (PDF worker) has to process before it gets restarted to clean up memory leaks |
| `MARKER_MIN_PROCESSES` | int | 1 | Minimum number of subprocesses available before triggering a process pool reset |
| `MARKER_TIMEOUT` | int | 3600 | Timeout in seconds for marker processes |



##### OpenAI-Compatible OCR Loader Configuration

Modern OCR pipelines increasingly rely on VLM-based OCR models (such as *DeepSeek OCR*, *DotsOCR*, or *LightOn OCR*) that convert PDF pages into images and feed them into vision-language models with specialized prompts.  
This loader integrates that workflow by exposing an OpenAI-compatible API that accepts PDF image pages and returns structured text produced by the OCR-VLM model in Markdown.

The parameters below configure how the OCR loader communicates with the model server, handles retries, manages concurrency, and controls model sampling behavior.

| Variable | Type | Default | Description |
|----------|--------|---------|-------------|
| `OPENAI_LOADER_BASE_URL` | `string` | `http://openai:8000/v1` | Base URL of the OCR loader (OpenAI-compatible endpoint). |
| `OPENAI_LOADER_API_KEY` | `string` | `EMPTY` | API key used to authenticate with the OCR service. |
| `OPENAI_LOADER_MODEL` | `string` | `dotsocr-model` | OCR VLM model to use (e.g., DotsOCR, DeepSeek OCR, LightOn OCR). |
| `OPENAI_LOADER_TEMPERATURE` | `float` | `0.2` | Sampling temperature. Lower values produce more deterministic OCR results. |
| `OPENAI_LOADER_TIMEOUT` | `int` | `180` | Maximum request duration (in seconds) before timing out. |
| `OPENAI_LOADER_MAX_RETRIES` | `int` | `2` | Number of retry attempts for failed OCR requests. |
| `OPENAI_LOADER_TOP_P` | `float` | `0.9` | Nucleus sampling parameter that limits generation to the top-p probability mass. |
| `OPENAI_LOADER_CONCURRENCY_LIMIT` | `int` | `20` | Maximum number of OCR requests processed concurrently. Useful for multi-page PDF workloads. |

:::note[Information]
This feature is currently experimental. Docker server configurations are available in [extern/ocr_vlm_servers](https://github.com/linagora/openrag/tree/main/extern/ocr_vlm_servers) and can be deployed using standard Docker Compose commands.
:::


#### Audio Loader

The transcriber is an OpenAI-compatible audio transcription service powered by Whisper models deployed via VLLM. It processes audio input by automatically segmenting it into chunks using silence detection, then transcribes these chunks in parallel for optimal speed and accuracy. This loader includes a bundled VLLM service for users who prefer to run Whisper locally.

To enable this service, set the `TRANSCRIBER_COMPOSE` variable to `extern/transcriber.yaml`. By default, it's disabled !!!

The following environment variables configure its behavior, performance, and connectivity:

<div style="overflow-x: auto; max-height: 500px; overflow-y: auto;">

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `TRANSCRIBER_BASE_URL` | `str` | `http://transcriber:8000/v1` | Base URL for the transcriber API (OpenAI-compatible endpoint). |
| `TRANSCRIBER_API_KEY` | `str` | `EMPTY` | Authentication key for transcriber service requests. |
| `TRANSCRIBER_MODEL` | `str` | `openai/whisper-large-v3-turbo` | Whisper model identifier served by VLLM for speech-to-text conversion. |
| `TRANSCRIBER_MAX_CHUNK_MS` | `int` | `30000` | Maximum duration (milliseconds) for each processed audio segment. Defines the upper limit for chunk length. |
| `TRANSCRIBER_SILENCE_THRESH_DB` | `int` | `-40` | Silence detection threshold (decibels) for voice activity detection. Audio below this level is classified as silence. |
| `TRANSCRIBER_MIN_SILENCE_LEN_MS` | `int` | `500` | Minimum silence duration (milliseconds) needed to trigger audio splitting. Shorter pauses are disregarded. |
| `TRANSCRIBER_MAX_CONCURRENT_CHUNKS` | `int` | `20` | Maximum number of audio chunks processed simultaneously. Increasing this value improves throughput when sufficient GPU resources are available. |

</div>

:::danger[Information]
This feature was recently introduced to externalize the audio loader for improved scalability and to resolve a queue blocking issue that occurred when running the audio loader internally.

As noted in [this PR](https://github.com/linagora/openrag/pull/134), the current VLLM implementation of Whisper has known limitations, including language mismatches between the source audio and the generated transcription. This issue is related to VLLM ([See this issue](https://github.com/vllm-project/vllm/issues/14174))
:::

### Chunking

| Variable               | Type | Default              | Description |
|------------------------|------|----------------------|-------------|
| `CHUNKER`              | `str`  | recursive_splitter   | Defines the chunking strategy: `recursive_splitter`, `semantic_splitter`, or `markdown_splitter`. |
| `CONTEXTUAL_RETRIEVAL` | `bool` | true                 | Enables contextual retrieval to chunk context, a technique introduced by Anthropic to improve retrieval performance ([Contextual Retrieval](https://www.anthropic.com/news/contextual-retrieval)) |
| `CHUNK_SIZE`           | `int`  | 512                  | Maximum size (in characters) of each chunk. |
| `CHUNK_OVERLAP_RATE`   | `float`| 0.2                  | Percentage of overlap between consecutive chunks. |

After files are converted to Markdown, only the **text content** is chunked.
**Image descriptions and Markdown tables are not chunked.**

**Chunker strategies:**

* **`recursive_splitter`**: Uses hierarchical text structure (sections, paragraphs, sentences). Based on [RecursiveCharacterTextSplitter](https://docs.langchain.com/oss/python/integrations/splitters/index#text-structure-based), it preserves natural boundaries whenever possible while ensuring chunks never exceeding the `CHUNK_SIZE`.

* **`markdown_splitter`**: Splits text using Markdown headers, then subdivides sections that exceed `CHUNK_SIZE`.

* **`semantic_splitter`**: Uses embedding-based semantic similarity to create meaning-preserving chunks. Oversized chunks are chunked to be less than `CHUNK_SIZE`.

### Embedding
Our embedder is **OpenAI-compatible** and runs on a **VLLM** instance configured with the following variables:

| Variable | Type | Default | Description  |
|----------|------|---------|--------------|
| `EMBEDDER_MODEL_NAME` | `str` | jinaai/jina-embeddings-v3 | HuggingFace Embedding model served by VLLM .i.e `Qwen/Qwen3-Embedding-0.6B` or `jinaai/jina-embeddings-v3`|
| `EMBEDDER_BASE_URL` | `str` | http://vllm:8000/v1 | Base URL of the embedder (OpenAI-style).|
| `EMBEDDER_API_KEY`  | `str` | EMPTY | API key for authenticating embedder calls.|

If you prefer to use an **external embedding service**, simply comment out the embedder service in the [docker-compose.yaml](https://github.com/linagora/openrag/blob/dev/docker-compose.yaml#L117-L153) and provide the variables above in your environment.


### Database Configuration

Our system uses two databases that work together:

* **`Vector Database (VDB)`**

The vector database stores embeddings and is configured using the following environment variables:

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `VDB_HOST` | str | milvus | Hostname of the vector database service |
| `VDB_PORT` | int | 19530 | Port on which the vector database listens |
| `VDB_CONNECTOR_NAME` | str | milvus | Connector/driver to use for the vector DB. Currently only `milvus` is implemented |
| `VDB_COLLECTION_NAME` | str | vdb_test | Name of the collection storing embeddings |
|`VDB_HYBRID_SEARCH`| `bool` | true |To activate hybrid search (semantic similarity + Keyword search)|

These variables can be overridden when using an external vector database service.

* **`Relational Database (RDB)`**

The vector database implementation relies on an underlying PostgreSQL database that stores metadata about partitions and their owners (users). For more information about the data structure, see the [data model](/openrag/documentation/data_model).

The PostgreSQL database is configured using the following environment variables:

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `POSTGRES_HOST` | str | rdb | Hostname of the PostgreSQL database service |
| `POSTGRES_PORT` | int | 5432 | Port on which the PostgreSQL database listens |
| `POSTGRES_USER` | str | root | Username for database authentication |
| `POSTGRES_PASSWORD` | str | root_password | Password for database authentication |


## Chat Pipeline
### LLM & VLM Configuration
The system uses two types of language models:

- **LLM (Large Language Model)**: The primary model for text generation and chat interactions
- **VLM (Vision Language Model)**: Used for describing images (see **`IMAGE_CAPTIONING`**) and, to reduce load on the primary LLM, also handles contextualization tasks (see **`CONTEXTUAL_RETRIEVAL`**)

These are external services to provide !!!

#### LLM Configuration
| Variable | Type | Description |
|----------|------|-------------|
| `BASE_URL` | str | Base URL of the LLM API endpoint |
| `MODEL` | str | Model identifier for the LLM |
| `API_KEY` | str | API key for authenticating with the LLM service |
| `LLM_SEMAPHORE` | int | 10 | Maximum number of concurrent requests to allow for the LLM service |


#### VLM Configuration
| Variable | Type | Description |
|----------|------|-------------|
| `VLM_BASE_URL` | str | Base URL of the VLM API endpoint |
| `VLM_MODEL` | str | Model identifier for the VLM |
| `VLM_API_KEY` | str | API key for authenticating with the VLM service |
| `VLM_SEMAPHORE` | int | 10 | Maximum number of concurrent requests to allow for the VLM service |

### Retriever Configuration

The retriever fetches relevant documents from the vector database based on query similarity. Retrieved documents are then [optionally reranked](/openrag/documentation/env_vars/#reranker-configuration) to improve relevance.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `RETRIEVER_TOP_K` | int | 50 | Number of documents to retrieve before reranking.|
| `SIMILARITY_THRESHOLD` | float | 0.6 | Minimum similarity score (0.0-1.0) for document retrieval. Documents below this threshold are filtered out |
| `RETRIEVER_TYPE` | str | single | Retrieval strategy to use. Options: `single`, `multiQuery`, `hyde` |

#### Retrieval Strategies

| Strategy | Description |
|----------|-------------|
| **single** | Standard semantic search using the original query. Fast and efficient for most queries |
| **multiQuery** | Generates multiple query variations to improve recall. Better coverage for ambiguous or complex questions |
| **hyde** | [Hypothetical Document Embeddings](https://arxiv.org/pdf/2212.10496) - generates a hypothetical answer then searches for similar documents|

### Reranker Configuration

The reranker enhances search quality by re-scoring and reordering retrieved documents according to their relevance to the user's query. Currently, the system uses [Infinity server](https://github.com/michaelfeil/infinity) for reranking functionality.

:::info[Future Improvements]
The current Infinity server interface is not OpenAI-compatible, which limits integration flexibility. We plan to improve this by supporting OpenAI-compatible reranker interfaces in future releases.
:::

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `RERANKER_ENABLED` | `bool` | true | Enable or disable the reranking mechanism |
| `RERANKER_MODEL` | `str` | Alibaba-NLP/gte-multilingual-reranker-base | Model used for reranking documents.|
| `RERANKER_TOP_K` | `int` | 5 | Number of top documents to return after reranking. Increase to 8 for better results if your LLM has a wider context window |
| `RERANKER_BASE_URL` | `str` | http://reranker:7997 | Base URL of the reranker service |
| `RERANKER_PORT` | `int` | 7997 | Port on which the reranker service listens |

## Extra
### Prompts

The RAG pipeline comes with preconfigured prompts **`./prompts/example1`**. Here are available Prompt Templates in that folder.

| Template File | Purpose |
|---------------|---------|
| `sys_prompt_tmpl.txt` | System prompt that defines the assistant's behavior and role |
| `query_contextualizer_tmpl.txt` | Template for adding context to user queries |
| `chunk_contextualizer_tmpl.txt` | Template for contextualizing document chunks during indexing |
| `image_captioning_tmpl.txt` | Template for generating image descriptions using the VLM |
| `hyde.txt` | Hypothetical Document Embeddings (HyDE) query expansion template |
| `multi_query_pmpt_tmpl.txt` | Template for generating multiple query variations |

To customize prompt:
1. **Duplicate the example folder**: Copy the `example1` folder from `./prompts/`
2. **Create your custom folder**: Rename it to something meaningful, e.g., `my_prompt`
3. **Modify the prompts**: Edit any prompt templates within your new folder
4. **Update configuration**: Point to your custom prompts directory
  ```bash
  //.env
  # Use custom prompts
  export PROMPTS_DIR=../prompts/my_prompt
  ```

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `PROMPTS_DIR` | str | ../prompts/example1 | Path to the directory containing your prompt templates |

### Logging
Our application uses Loguru with custom formatting. Log messages appear in two places:
- **Terminal (stderr)**: Human-readable formatted output
- **Log file** (`logs/app.json`): JSON format for monitoring tools like Grafana. This file resides at the mounted folder `./logs` 

#### Log Message Format
Terminal output follows this format:
```bash title="Logging message in the terminal..."
LEVEL    | module:function:line - message [context_key=value]
```
#### Logging Levels & What They Mean
There are several logging levels available (TRACE, DEBUG, INFO, SUCCESS, WARNING, ERROR, CRITICAL). Only the levels intended for use in this project are documented here.

| Level | What You'll See in Logs |
|-------|-------------------------|
| **WARNING** | Potential issues that don't stop execution: approaching rate limits, deprecated features used, retryable failures, configuration concerns. Review these periodically. |
| **DEBUG** | Detailed diagnostic information including variable states, intermediate processing steps, and function entry/exit points. Useful during development and troubleshooting. |
| **INFO** | Standard operational messages showing normal application behavior: server startup, request handling, major workflow stages. This is the typical production level. |

#### Configuration
Set the logging level via environment variable:

```bash
// .env
# Show only warnings and errors
LOG_LEVEL=WARNING

# Show detailed debug information (use in dev and pre-prod)
LOG_LEVEL=DEBUG

# Production default (informational messages)
LOG_LEVEL=INFO
```

#### Log File Features

- **Rotation**: Files rotate automatically at 10 MB
- **Retention**: Logs kept for 10 days
- **Format**: JSON for easy parsing and ingestion into monitoring systems
- **Async**: Queued writing (`enqueue=True`) prevents blocking operations

:::tip[Reading Logs]
- **In development**: Watch terminal output with DEBUG level
- **In production**: Use INFO level and monitor the JSON log file
- **For troubleshooting**: Temporarily switch to DEBUG or TRACE
- **For monitoring**: Parse `logs/app.json` with your observability stack
:::

### RAY
Ray is used for distributed task processing and parallel execution in the RAG pipeline. This configuration controls **`resource allocation`**, **`concurrency limits`**, and **`serving options`**.

#### General Ray Settings

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `RAY_POOL_SIZE` | `int` | 1 | Number of serializer actor instances (typically 1 actor per cluster node) |
| `RAY_MAX_TASKS_PER_WORKER` | `int` | 8 | Maximum number of concurrent tasks (serialization tasks) per serializer actor instance |
| `RAY_DASHBOARD_PORT` | `int` | 8265 | Ray Dashboard port used for monitoring. In production, [comment out this line](https://github.com/linagora/openrag/blob/ee732ea8e080dcde0107d62d12703a7525f810cd/docker-compose.yaml#L21C1-L22C1) to avoid exposing the port, as it may introduce security vulnerabilities. |

:::danger[Attention]
The following environment variables control Ray's logging behavior, task retry settings. These are not set by default and must be supplied [as suggested in the .env](/openrag/getting_started/quickstart#2-create-a-env-file)
:::

| Variable | Type | value | Description |
|----------|------|---------|-------------|
| `RAY_DEDUP_LOGS` | `number` | `0` | Turns off Ray log deduplication that appears across multiple processes. Set to `0` to see all logs from each process. |
| `RAY_ENABLE_RECORD_ACTOR_TASK_LOGGING` | `number` | `1` | Enables logs at task level in the Ray dashboard for better debugging and monitoring. |
| `RAY_task_retry_delay_ms` | `number` | `3000` | Delay (in milliseconds) before retrying a failed task. Controls the wait time between retry attempts. |
| `RAY_ENABLE_UV_RUN_RUNTIME_ENV` | `number` | `0` | Controls UV runtime environment integration. **Critical**: Must be set to `0` when using the newest version of UV to avoid compatibility issues. |

#### Indexer Configuration

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `RAY_MAX_TASK_RETRIES` | int | 2 | Number of retry attempts for failed tasks |
| `INDEXER_SERIALIZE_TIMEOUT` | int | 36000 | Timeout in seconds for serialization operations (10 hours) |

#### Indexer Concurrency Groups

Controls the maximum number of concurrent operations for different indexer tasks:

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `INDEXER_DEFAULT_CONCURRENCY` | int | 1000 | Default concurrency limit for general operations |
| `INDEXER_UPDATE_CONCURRENCY` | int | 100 | Maximum concurrent document update operations |
| `INDEXER_SEARCH_CONCURRENCY` | int | 100 | Maximum concurrent search/retrieval operations |
| `INDEXER_DELETE_CONCURRENCY` | int | 100 | Maximum concurrent document deletion operations |
| `INDEXER_CHUNK_CONCURRENCY` | int | 1000 | Maximum concurrent document chunking operations |
| `INDEXER_INSERT_CONCURRENCY` | int | 10 | Maximum concurrent document insertion operations |

#### Semaphore Configuration

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `RAY_SEMAPHORE_CONCURRENCY` | int | 100000 | Global concurrency limit for Ray semaphore operations |

#### Ray Serve Configuration
Ray Serve enables deployment of the FastAPI as a scalable service. For simple deployment, without the intend to scale, one can usage the [uvicorn deployment mode](/openrag/documentation/env_vars/#ray-serve-configuration)

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `ENABLE_RAY_SERVE` | bool | false | Enable Ray Serve deployment mode |
| `RAY_SERVE_NUM_REPLICAS` | int | 1 | Number of service replicas for load balancing |
| `RAY_SERVE_HOST` | str | 0.0.0.0 | Host address for the Ray Serve deployment |
| `RAY_SERVE_PORT` | int | 8080 | Port for the Ray Serve FastAPI endpoint |
| `CHAINLIT_PORT` | int | 8090 | Port for the Chainlit UI interface if ray serve is enable `ENABLE_RAY_SERVE`. If not chainlit UI is simply a subroute (`/chainlit` [see this](/openrag/getting_started/usage/#default-ports)) of the FastAPI **`base_url`**|


### Map & Reduce Configuration
The map & reduce mechanism processes documents by fetching chunks (map phase), filtering out irrelevant ones and summarizing relevant content (reduce phase) with respect to the user's query. The algorithm works as follows:

1. Initially fetches a batch of documents for processing
2. Evaluates relevance and continues expanding the search if needed
3. Stops expansion when the last `MAP_REDUCE_EXPANSION_BATCH_SIZE` chunks are all irrelevant
4. Otherwise, continues fetching additional documents up to `MAP_REDUCE_MAX_TOTAL_DOCUMENTS`

When `MAP_REDUCE_DEBUG` is enabled, the mechanism logs detailed information to `./logs/map_reduce.md`.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `MAP_REDUCE_INITIAL_BATCH_SIZE` | `int` | 10 | Number of documents to process in the initial mapping phase |
| `MAP_REDUCE_EXPANSION_BATCH_SIZE` | `int` | 5 | Number of additional documents to fetch when expanding the search (also used as the threshold for stopping) |
| `MAP_REDUCE_MAX_TOTAL_DOCUMENTS` | `int` | 20 | Maximum total number of documents (chunks) to process across all iterations |
| `MAP_REDUCE_DEBUG` | `bool` | true | Enable debug logging for map & reduce operations. Logs are written to `./logs/map_reduce.md` |

:::danger[Caution]
While the map & reduce mechanism enables processing more documents for LLMs with limited context lengths (by summarizing relevant documents to free up context space), it comes with trade-offs:

- **Time-consuming**: The intermediate summarization steps significantly increase response time
- **User experience**: Due to performance considerations, this is not the default mechanism
Use this feature when thoroughness is more important than response speed.
:::

:::tip[How to Enable Map & Reduce?]
When chatting with a partition, you can enable the map & reduce mechanism through the OpenAI-compatible API by setting the `use_map_reduce` metadata variable (disabled by default).

```bash frame="none" title="Enabling map & reduce via OpenAI chat completions endpoint"
curl -X 'POST' 'http://localhost:8080/v1/chat/completions' \
  -H 'accept: application/json' \
  -H 'Authorization: Bearer YOUR_AUTH_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{
  "model": "openrag-{partition_name}",
  "messages": [
    {
      "role": "user",
      "content": "your_query"
    }
  ],
  "temperature": 0.3,
  "stream": false,
  "metadata": {
    "use_map_reduce": true
  }
}'
```
:::

### FastAPI & Access Control
:::info
By default, our API (FastAPI) uses **`uvicorn`** for deployment. One can opt in to use `Ray Serve` for scalability (see the [ray serve configuration](/openrag/documentation/env_vars/#ray-serve-configuration))
:::

The following environment variables configure the FastAPI server and control access permissions:

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `APP_PORT` | `number` | `8000` | Port number on which the FastAPI application listens for incoming requests. |
| `AUTH_TOKEN` | `string` | `EMPTY` | An authentication token is required to access protected API endpoints. By default, this token corresponds to the API key of the created admin (see [Admin Bootstrapping](/openrag/documentation/user_auth/#2-admin-bootstrapping)). If left empty, authentication is disabled. |
| `SUPER_ADMIN_MODE` | `boolean` | `false` | Enables super admin privileges when set to `true`, [granting unrestricted access](/openrag/documentation/data_model/#access-control) to all operations and bypassing standard access controls. This is for debugging |
|`API_NUM_WORKERS`|`int`|1|Number of uvicorn workers|



:::caution[Security Notice]
Always set a strong **`AUTH_TOKEN`** in production environments. Never leave it empty or use default values in production deployments.
:::

### Indexer-UI

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `INCLUDE_CREDENTIALS` | `boolean` | `false` | If authentification is  |
| `INDEXERUI_PORT` | `number` | `8060` | Port number on which the Indexer UI application runs. Default is `8060` (documentation mentions `3042` as another common default). |
| `INDEXERUI_URL` | `string` | `http://X.X.X.X:INDEXERUI_PORT` | Base URL of the Indexer UI. Required to prevent CORS issues. Replace `X.X.X.X` with `localhost` (local) or your server IP, and `INDEXERUI_PORT` with the actual port. |
| `API_BASE_URL` | `string` | `http://X.X.X.X:APP_PORT` | Base URL of your FastAPI backend, used by the frontend to communicate with the API. Replace `X.X.X.X` with `localhost` (local) or your server IP, and `APP_PORT` with your FastAPI port. |

### Chainlit
[See this](/openrag/documentation/setup_chainlit_ui_auth/) for chainlit authentification

[See this](/openrag/documentation/chainlit_data_persistency/) for chainlit data persistency
