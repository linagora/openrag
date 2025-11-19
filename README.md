# 🦫 OpenRag — The Open RAG Experimentation Playground

![RAG Architecture](./RAG_architecture.png)

[OpenRag](https://open-rag.ai/) is a lightweight, modular and extensible Retrieval-Augmented Generation (RAG) framework designed to explore and test advanced RAG techniques — 100% open source and focused on experimentation, not lock-in.

> Built by the Linagora, OpenRag offers a sovereign-by-design alternative to mainstream RAG stacks.

## Table of Contents
- [🦫 OpenRag — The Open RAG Experimentation Playground](#-openrag--the-open-rag-experimentation-playground)
- [Table of Contents](#table-of-contents)
- [🎯 Goals](#-goals)
- [✨ Key Features](#-key-features)
- [🚧 Coming Soon](#-coming-soon)
- [🚀 Installation](#-installation)
  - [Prerequisites](#prerequisites)
  - [Installation and Configuration](#installation-and-configuration)
- [🔧 Troubleshooting](#-troubleshooting)
- [🤝 Support and Contributing](#-support-and-contributions)
- [📜 License](#-license)


## 🎯 Goals
- Experiment with advanced RAG techniques
- Develop evaluation metrics for RAG applications
- Collaborate with the community to innovate and push the boundaries of RAG applications

## ✨ Key Features
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
* **Admin Dashbord** to monitore RAG components (Indexer, VectorDB, TaskStateManager, etc)
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
[OpenRag](https://open-rag.ai/) API is tailored to be compatible with the OpenAI format (see the [openai-compatibility section](docs/api_documentation.md#-openai-compatible-chat) for more details), enabling seamless integration of your deployed RAG into popular frontends and workflows such as OpenWebUI, LangChain, N8N, and more. This ensures flexibility and ease of adoption without requiring custom adapters.

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

* **Hybrid search** - combines semantic similarity with BM25 keyword matching
* **Contextual retrieval** - Anthropic's technique for enhanced chunk relevance
* **Multilingual reranking** - using `Alibaba-NLP/gte-multilingual-reranker-base`

For more details, [see this file](docs/features_in_details.md)

</details>


## 🚧 Coming Soon
* **📂 Expanded Format Support**: Future updates will introduce compatibility with additional formats such as `csv`, `odt`, `html`, and other widely used open-source document types.
* **🔄 Unified Markdown Conversion**: All files will continue to be converted to markdown using a consistent chunker. Format-specific chunkers (e.g., for CSV, HTML) are planned for enhanced processing.
* **🤖 Advanced Features**: Upcoming releases will include Tool Calling, Agentic RAG, and MCP to elevate your RAG workflows.
* **Enhanced Security**: Ensures data encryption both during transit and at rest.

## 🚀 Installation

For comprehensive documentation and troubleshooting guidance, visit our [documentation site](https://linagora.github.io/openrag/).

### Prerequisites
- **Python 3.12** or higher recommended
- **Docker** and **Docker Compose**
- For GPU capable machines, ensure you have the NVIDIA Container Toolkit installed. Refer to the [NVIDIA documentation](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) for installation instructions.

### Installation and Configuration
#### 1. Clone the repository:
```bash
git clone --recurse-submodules git@github.com:linagora/openrag.git

cd openrag
git checkout main # or a given release
```
#### 2. Create a `.env` File
Create a `.env` file at the root of the project, mirroring the structure of `.env.example`, to configure your environment and supply blank environment variables.

```bash
cp .env.example .env
```
#### 3. File Parser configuration 
All supported file format parsers are pre-configured. For PDF processing, **[MarkerLoader](https://github.com/datalab-to/marker)** serves as the default parser, offering comprehensive support for OCR-scanned documents, complex layouts, tables, and embedded images. MarkerLoader operates efficiently on both GPU and CPU environments.

<details>
<summary>For more PDF options</summary>

For CPU-only deployments or lightweight testing scenarios, you can consider switching to **`PyMuPDF4LLMLoader`** or **`PyMuPDFLoader`**. To change the loader, set the **`PDFLoader`** variable like this `PDFLoader=PyMuPDF4LLMLoader`.

> ⚠️ **Important**: These alternative loaders have limitations - they cannot process non-searchable (image-based) PDFs and do not extract or handle embedded images.
</details>

#### 4.Deployment: Launch the app
>[!IMPORTANT]
> In case **`Indexer UI` (A Web interface for intuitive document ingestion, indexing, and management.)** is not configured already in your `.env`, follow this dedicated guide:
➡ [Deploy with Indexer UI](docs/setup_indexerui.md)

* **Simple and quick** launch for testing
  >[!IMPORTANT]
  > For a **simple `quick deployment`** using only the docker-compose file, only the [quick_start **folder**](./quick_start/) is required. Follow these steps to launch the application:
  
  1. Navigate to the **`quick_start`** directory or download only that folder
  2. Place your **`.env`** file inside the **`quick_start`** directory
  3. Run the appropriate command for your system:

  ```bash
  # GPU deployment (recommended for optimal performance)
  docker compose up -d
  # docker compose down # to stop the application

  # CPU deployment
  docker compose --profile cpu up -d
  # docker compose --profile cpu down # to stop the application
  ```
* **Development Environment**: For development builds, use the **`--build`** flag to rebuild images:
  >[!NOTE]
  > Execute these commands from the project root directory

  ```bash
  # GPU deployment with rebuild (recommended for optimal performance)
  docker compose up --build -d
  # docker compose down # to stop the application

  # CPU deployment with rebuild
  docker compose --profile cpu up --build -d
  # docker compose --profile cpu down # to stop the application
  ```

>[!WARNING]
> The first startup may take longer as required dependencies are installed. 

>[!IMPORTANT]
> For CPU-only deployments, consider these performance optimizations:
> 1. Disable the reranker by setting **`RERANKER_ENABLED=false`** (reranking is computationally intensive on CPU)
> 2. If keeping the reranker enabled (recommended for better RAG accuracy), reduce the number of documents sent for reranking by lowering **`RETRIEVER_TOP_K`** to approximately 10


Once the app is up and running, visit `http://localhost:APP_PORT` or `http:X.X.X.X:APP_PORT` to access via:

1. **`/docs`** – FastAPI’s full API documentation. See this [detailed overview of our api](docs/api_documentation.md) for more details on the endpoints.


2. **`/chainlit`** – [Chainlit chat UI](https://docs.chainlit.io/get-started/overview) to chat with your partitions. To disable it (e.g., for backend-only use), set `WITH_CHAINLIT_UI=False`.

>[!NOTE]
> Chainlit UI has no authentication by default. To enable it, follow the [dedicated guide](./docs/setup_chainlit_ui_auth.md). The same goes for chat data persistancy, enable it with this [guide](docs/chainlit_data_persistency.md)

3. `http://localhost:INDEXERUI_PORT` to access the indexer ui for easy document ingestion, indexing, and management

#### 5. Distributed deployment in a Ray cluster

To scale **OpenRag** in a distributed environment using **Ray**, follow the dedicated guide:
➡ [Deploy OpenRag in a Ray cluster](docs/deploy_ray_cluster.md)


## Tests

To run all unit tests:

```bash
uv run pytest
```

## Documentation

For comprehensive documentation and troubleshooting guidance, visit our documentation site.

To run the documentation site locally for development:
```bash
npm i     # Install dependencies
npm run dev   # Start the development server
```

And then go to http://localhost:4321/openrag


## 🔧 Troubleshooting
<details>
<summary>Troubleshooting</summary>

### Error on dependencies installation

After running `uv sync`, if you have this error:

```
error: Distribution `ray==2.43.0 @ registry+https://pypi.org/simple` can't be installed because it doesn't have a source distribution or wheel for the current platform

hint: You're using CPython 3.13 (`cp313`), but `ray` (v2.43.0) only has wheels with the following Python ABI tag: `cp312`
```

This means your uv installation relies on cpython 3.13 while you are using python 3.12.

To solve it, please run:
```bash
uv venv --python=3.12
uv sync
```
### Error with models' weights downloading
While executing OpenRag, if you encounter a problem that prevents you from downloading the models' weights locally, then you just need to create the needed folder and authorize it to be written and executed

```bash
sudo mkdir /app/model_weights
sudo chmod 775 /app/model_weights
```
</details>





## 🤝 Support and Contributions
We ❤️ your contributions!

We encourage you to contribute to OpenRag! Here's how you can get involved:
1. Fork this repository.
2. Create a new branch for your feature or fix.
3. Submit a pull request for review.

Feel free to ask **`questions`, `suggest features`, or `report bugs` via the GitHub Issues page**. Your feedback helps us improve!


## 📜 License

OpenRag is licensed under the [AGPL-3.0](LICENSE). You are free to use, modify, and distribute this software in compliance with the terms of the license.

For more details, refer to the [LICENSE](LICENSE) file in the repository.
