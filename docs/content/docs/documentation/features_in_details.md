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