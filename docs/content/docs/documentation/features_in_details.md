---
title: ✨ Key Features
---

### 📁 Rich File Format Support
[OpenRag](https://open-rag.ai/) supports a comprehensive range of file formats for seamless document ingestion:

* **Text Files**: `txt`, `md`
* **Document Files**: `pdf`, `docx`, `doc`, `pptx` - Advanced PDF parsing with OCR support and Office document processing
* **Audio Files**: `wav`, `mp3`, `mp4`, `ogg`, `flv`, `wma`, `aac` - Audio transcription and content extraction
* **Images**: `png`, `jpeg`, `jpg`, `svg` - Vision Language Model (VLM) powered image captioning and analysis

All files are intelligently converted to **Markdown format** with images replaced by AI-generated captions, ensuring consistent processing across all document types.

### 🎛️ Native Web-Based Indexer UI
Experience intuitive document management through our built-in web interface.

<details>

<summary>Indexer UI Features</summary>

* **Drag-and-drop file upload** with batch processing capabilities
* **Real-time indexing progress** monitoring and status updates
* **Admin Dashboard** to monitor RAG components (Indexer, VectorDB, TaskStateManager, etc)
* **Partition management** - organize documents into logical collections
* **Visual document preview** and metadata inspection
* **Search and filtering** capabilities for indexed content

</details>

### 🗂️ Partition-Based Architecture
Organize your knowledge base with flexible partition management:
* **Multi-tenant support** - isolate different document collections

### 💬 Interactive Chat UI with Source Attribution
Engage with your documents through our sophisticated chat interface:

<details>

<summary>Chat UI Features</summary>

* **Chainlit-powered UI** - modern, responsive chat experience
* **Source transparency** - every response includes relevant document references
</details>


### 🔌 OpenAI API Compatibility
[OpenRag](https://open-rag.ai/) API is tailored to be compatible with the OpenAI format (see the [openai-compatibility section](/documentation/api/#-openai-compatible-chat) for more details), enabling seamless integration of your deployed RAG into popular frontends and workflows such as OpenWebUI, LangChain, N8N, and more. This ensures flexibility and ease of adoption without requiring custom adapters.

<details>

<summary>Summary of features</summary>

* **Drop-in replacement** for OpenAI API endpoints
* **Compatible with popular frontends** like OpenWebUI, LangChain, N8N, and more
* **Authentication support** - secure your API with token-based auth

</details>


### ⚡ Distributed Ray Deployment
Scale your RAG pipeline across multiple machines and GPUs.
<details>

<summary>Distributed Ray Deployment</summary>

* **Horizontal scaling** - distribute processing across worker nodes
* **GPU acceleration** - optimize inference across available hardware
* **Resource management** - intelligent allocation of compute resources
* **Monitoring dashboard** - real-time cluster health and performance metrics

See the section on [distributed deployment in a ray cluster](#5-distributed-deployment-in-a-ray-cluster) for more details

</details>

### 🔍 Advanced Retrieval & Reranking
[OpenRag](https://open-rag.ai/) Leverages state-of-the-art retrieval techniques for superior accuracy.

<details>

<summary>Implemented advanced retrieval techniques</summary>

* **Hybrid search** - combines semantic similarity with **`BM25` keyword** matching
* **Contextual retrieval** - Anthropic's technique for enhanced chunk relevance
* **Multilingual reranking** - using `Alibaba-NLP/gte-multilingual-reranker-base`

</details>

### 🕐 Temporal Awareness
OpenRAG includes intelligent temporal understanding to deliver more relevant, time-aware responses.

<details>

<summary>Temporal Features</summary>

* **Automatic date extraction** - Detects temporal expressions in queries across multiple languages
  * ISO dates: `2024-01-15`, `2024-01-15T10:30:00`
  * Numeric formats: `15/01/2024`, `01/15/2024`, `15.01.2024`
  * Month-year: `01/2024`, `2024/01`
  * Year only: `2024`, `2023`
  * Relative time: "last 30 days", "últimos 7 días", "derniers 30 jours"
  * Keywords: "today", "yesterday", "recent" (and multilingual equivalents)

* **Document timestamp metadata** - Tracks temporal information for each document
  * `datetime` - Primary timestamp from document content (user-provided)
  * `modified_at` - Document modification timestamp
  * `created_at` - Document creation timestamp  
  * `indexed_at` - When the document was indexed into OpenRAG

* **Temporal filtering** - Automatically filters search results based on detected time ranges
  * Queries like "documents from 2024" only retrieve relevant documents
  * "Last week's updates" focuses on recent content
  * Works across all retrieval methods (base, multi-query, HyDE)

* **Temporal scoring in reranking** - Balances relevance with recency
  * Combines semantic relevance score with temporal score
  * Configurable temporal weight (default: 30% temporal, 70% relevance)
  * Linear decay formula favors more recent documents
  * Configurable decay period (default: 365 days)
  * Priority hierarchy: `datetime` > `modified_at` > `created_at` > `indexed_at`

* **Temporal-aware prompts** - LLM receives temporal context
  * Current date/time injected into system prompt
  * Document timestamps included in retrieved chunks
  * LLM instructed to consider recency when answering
  * Prioritizes newer information for time-sensitive queries

* **Configuration options** via environment variables:
  * `RERANKER_TEMPORAL_WEIGHT` - Weight for temporal scoring (0.0-1.0, default: 0.3)
  * `RERANKER_TEMPORAL_DECAY_DAYS` - Days for temporal score decay (default: 365)

</details>