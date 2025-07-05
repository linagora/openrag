# FastAPI RAG Backend API Documentation

## 🌟 Overview

This FastAPI-powered backend provides a comprehensive document-based question answering system using Retrieval-Augmented Generation (RAG). The API supports semantic search, document indexing, and chat completions across multiple data partitions with full OpenAI compatibility.

## 🔐 Authentication

All endpoints require authentication when **enabled**. Include your `AUTH_TOKEN` in the HTTP request header:

```
Authorization: Bearer YOUR_AUTH_TOKEN
```

For OpenAI-compatible endpoints, `AUTH_TOKEN` serves as the `api_key` parameter, or use a placeholder like `'sk-1234'` when authentication is disabled (necessary for when using OpenAI client).

---

## 🚀 API Endpoints
### ℹ️ System Health
Verify server status and availability.
```http
GET /health_check
```

---

### 📦 Document Indexing

#### Upload New File
```http
POST /indexer/partition/{partition}/file/{file_id}
```

Upload a new file to a specific partition for indexing.

**Parameters:**
- `partition` (path): Target partition name
- `file_id` (path): Unique identifier for the file

**Request Body (form-data):**
- `file` (binary): File to upload
- `metadata` (JSON string): File metadata (e.g., `{"file_type": "pdf"}`)

**Responses:**
- `201 Created`: Returns task status URL
- `409 Conflict`: File already exists in partition

#### Replace Existing File
```http
PUT /indexer/partition/{partition}/file/{file_id}
```

Replace an existing file in the partition. Deletes the current entry and creates a new indexing task.

**Parameters:** Same as POST endpoint
**Request Body:** Same as POST endpoint
**Response:** `202 Accepted` with task status URL

#### Update File Metadata
```http
PATCH /indexer/partition/{partition}/file/{file_id}
```

Update file metadata without reindexing the document.

**Request Body (form-data):**
- `metadata` (JSON string): Updated metadata

**Response:** `200 OK` on successful update

#### Delete File
```http
DELETE /indexer/partition/{partition}/file/{file_id}
```

Remove a file from the specified partition.

**Responses:**
- `204 No Content`: Successfully deleted
- `404 Not Found`: File not found in partition

#### Check Indexing Status
```http
GET /indexer/task/{task_id}
```

Monitor the progress of an asynchronous indexing task.

**Response:** Task status information

---

#### See logs of a given task
```http
GET /indexer/task/{task_id}/logs
```

#### Get error details of a failed task 
```http
GET /indexer/task/{task_id}/error
```


### 🔍 Semantic Search

#### Search Across Multiple Partitions
```http
GET /search/
```

Perform semantic search across specified partitions.

**Query Parameters:**
- `partitions` (optional): List of partition names (default: `["all"]`)
- `text` (required): Search query text
- `top_k` (optional): Number of results to return (default: `5`)

**Responses:**
- `200 OK`: JSON list of document links (HATEOAS format)
- `400 Bad Request`: Invalid partitions parameter

#### Search Within Single Partition
```http
GET /search/partition/{partition}
```

Search within a specific partition only.

**Query Parameters:**
- `text` (required): Search query text
- `top_k` (optional): Number of results (default: `5`)

**Response:** Same as multi-partition search

#### Search Within Specific File
```http
GET /search/partition/{partition}/file/{file_id}
```

Search within a particular file in a partition.

**Query Parameters:** Same as partition search
**Response:** Same as other search endpoints

---

### 📄 Document Extraction

#### Get Extract Details
```http
GET /extract/{extract_id}
```

Retrieve specific document extract (chunk) by ID.

**Response:** JSON containing extract content and metadata

---

### 💬 OpenAI-Compatible Chat

These endpoints provide full OpenAI API compatibility for seamless integration with existing tools and workflows. For detailed example of openai usage [see this section](#openai-client-integration)

* List Available Models
```http
GET /v1/models
```

List all available RAG models (partitions).

**Model Naming Convention:**
- Pattern: `openrag-{partition_name}` => This model allows to chat specifically with the partition `{partition_name}`
- Special model: `partition-all` (queries entire vector database)

* Chat Completions
```http
POST /v1/chat/completions
```

OpenAI-compatible chat completion using **`RAG` pipeline**.

**Request Body:**
```json
{
  "model": "openrag-{partition_name}",
  "messages": [
    {"role": "user", "content": "Your question here"}
  ],
  "temperature": 0.7,
  "stream": 0.3
}
```

* Text Completions
```http
POST /v1/completions
```

OpenAI-compatible text completion endpoint.


## 💡 Usage Examples

### Bulk File Indexing

For indexing multiple files programmatically, you can use this script [`data_indexer.py`](../utility/data_indexer.py) utility script in the [`📁 utility`](../utility/) folder.

### OpenAI Client Integration

For detailed examples of using OpenAI clients with this API, see the [`openai_compatibility_guide.ipynb`](./utility/openai_compatibility_guide.ipynb) notebook in the [`📁 utility`](./utility/) folder.

#### Example OpenAI Client Usage

```python
from openai import OpenAI, AsyncOpenAI

api_base_url = "http://localhost:8080" # api base url 
base_url = f"{api_base_url}/v1"

auth_key = 'sk-1234' # your api authentification key. AUTH_TOKEN from your .env
client = OpenAI(api_key=auth_key, base_url=base_url)

your_partition=... # name of your partition
model = f"openrag-{your_partition}"
settings = {
    'model': model,
    'temperature': 0.3,
    'stream': False
}


response = client.chat.completions.create(
    **settings,
    messages=[
        {"role": "user", "content": "What information do you have about...?"}
    ]
)
```

---

## ⚠️ Error Handling

The API uses standard HTTP status codes:

- `200 OK`: Successful request
- `201 Created`: Resource created successfully
- `202 Accepted`: Request accepted for processing
- `204 No Content`: Successful deletion
- `400 Bad Request`: Invalid request parameters
- `404 Not Found`: Resource not found
- `409 Conflict`: Resource already exists

Error responses include detailed JSON messages to help with debugging and integration.