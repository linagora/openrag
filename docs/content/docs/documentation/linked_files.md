---
title: 🔗 Document Relationships & Linked Files
---

This document explains how **document relationships** are modeled in OpenRAG, covering hierarchical relationships (email threads) and folder-based relationships (files from the same folder).

## **1. Overview**

OpenRAG supports two types of file relationships to enable context-aware retrieval and preserve document structure:

- **Relationship-based grouping**: Groups related documents together (e.g., emails in a thread, files in a folder)
- **Hierarchical parent-child links**: Tracks parent-child relationships within a group (e.g., email reply chains)

### Relationship Fields

Two fields enable these relationship types:

* **`relationship_id`**: A shared identifier for documents that belong together
  - **For folders:** The folder/project path (e.g., `documents/2024/q1`)
  - **For email threads:** The thread ID (e.g., `thread-123`)

* **`parent_id`**: Tracks hierarchical relationships within a group by pointing to the direct parent document's ID
  - **For email threads:** References the email being replied to
  - Enables recursive traversal from any document back to the root

These relationships enable powerful retrieval patterns where searching for one document can automatically include its context (ancestors, related documents).

## **2. Data Model**

### File Model Extensions

The `File` model includes now two relationship fields:

| Field            | Type   | Description |
|------------------|--------|-------------|
| `relationship_id` | String (nullable, indexed) | Groups related documents (e.g., email thread ID, folder path) |
| `parent_id`       | String (nullable, indexed) | Points to hierarchical parent (e.g., parent email) |

**Indexes:**
- `ix_relationship_partition (relationship_id, partition_name)` — enables efficient relationship queries
- `ix_parent_partition (parent_id, partition_name)` — enables efficient ancestor traversal

### Example: Email Thread

```
Email A (root)
├── relationship_id: "thread-123"
├── parent_id: null 
├── file_id: "email-a-id"
└── Email B (reply)
    ├── relationship_id: "thread-123"
    ├── parent_id: "email-a-id"
    ├── file_id: "email-b-id"
    └── Email C (reply to B)
        ├── relationship_id: "thread-123"
        ├── parent_id: "email-b-id"
        └── file_id: "email-c-id"
```

- All three emails share `relationship_id: "thread-123"` (grouped in the same thread)
- Each reply points to its parent via `parent_id` (forms hierarchical chain)

### Example: Folder Structure

```
Documents/2024/Q1/
├── Report.pdf
│   └── relationship_id: "documents/2024/q1"
├── Budget.xlsx
│   └── relationship_id: "documents/2024/q1"
└── Notes.md
    └── relationship_id: "documents/2024/q1"
```

All files in the same folder share the same `relationship_id` (normalized folder path).

:::caution
**Current Scope:** This implementation models **file grouping within folders** only, not nested folder hierarchies.

While the data model supports folder hierarchies via `parent_id`, retrieval behavior for nested folders is currently undefined:
- Should the system fetch files from **parent folders** when a file is retrieved?
- Or only files from the **same folder**?

This design decision requires clarification before implementing folder hierarchy support.
:::

## **3. Use Cases**

### 📧 Email Threads

**Problem:**  
When retrieving a single email from search results, conversation context is lost.

**Solution:**  
- `relationship_id` = email thread ID (from mail server)
- `parent_id` = ID of the email being replied to

**Benefits:**
- Retrieve entire conversation with `/relationships/{relationship_id}`
- Navigate thread hierarchy with `/file/{file_id}/ancestors`
- Context-aware search expands single email results to full threads

### 📁 Folder-Based Organization

**Problem:**  
Files in the same folder are conceptually related but stored independently.

**Solution:**  
- `relationship_id` = normalized folder path (e.g., `documents/projects/2024`)
- `parent_id` = **not used for folders**

**Benefits:**
- Find all files in a folder via relationship query
- Group related documents from the same folder
- Search within folder context

**Note:**  
Folder hierarchy (parent folders, nested folders) is **not modeled** in the current implementation. Only file grouping within a single folder is supported via `relationship_id`.

## **4. API Endpoints**

### Indexation Endpoints
See the [related documents indexing section](/openrag/documentation/api/#upload-files-while-modeling-relations-between-them).

### Get Files by Relationship

```http
GET /{partition}/relationships/{relationship_id}
```

Returns all files sharing the same `relationship_id` within a partition.

**Parameters:**
- `partition` — partition name
- `relationship_id` — the relationship group identifier (thread ID, folder path, etc.)

**Response:**
```json
{
  "files": [
    {
      "file_id": "email-a-id",
      "filename": "Re: Project Update",
      "relationship_id": "thread-123",
      "parent_id": null
    },
    {
      "file_id": "email-b-id",
      "filename": "Re: Re: Project Update",
      "relationship_id": "thread-123",
      "parent_id": "email-a-id"
    }
  ]
}
```

**Use Cases:**
- Retrieve all emails in a thread
- Retrieve all documents in a folder
- Group related documents for bulk operations

### Get File Ancestors

```http
GET /{partition}/file/{file_id}/ancestors
```

Returns the complete ancestor path from root to the specified file.

**Parameters:**
- `partition` — partition name
- `file_id` — the file to trace ancestors for

**Response:**
```json
{
  "ancestors": [
    {
      "file_id": "email-a-id",
      "filename": "Original Email",
      "parent_id": null
    },
    {
      "file_id": "email-b-id",
      "filename": "First Reply",
      "parent_id": "email-a-id"
    },
    {
      "file_id": "email-c-id",
      "filename": "Second Reply",
      "parent_id": "email-b-id"
    }
  ]
}
```

**Use Cases:**
- Reconstruct email conversation thread
- Trace document evolution history
- Navigate hierarchical email replies

**Note:**  
- This endpoint is primarily designed for **email threads** with parent-child relationships.
- For **folders**, use the `/relationships/{relationship_id}` endpoint instead (folders don't have hierarchy).
- Returns only the direct ancestor path, not sibling branches. For email threads with parallel replies, each branch has its own ancestor path.

## **5. Context-Aware Search**

The search API supports automatic expansion of results based on document relationships.

### Search Parameters

```http
GET /search?text=query&include_related=true&include_ancestors=true
```

| Parameter           | Type    | Default | Description |
|---------------------|---------|---------|-------------|
| `text`              | string  | required | Search query |
| `top_k`             | integer | 5       | Number of initial results |
| `include_related`   | boolean | false   | Include chunks from files with same `relationship_id` |
| `include_ancestors` | boolean | false   | Include chunks from ancestor files (via `parent_id` chain) |
| `partitions`        | array   | ["all"] | Partitions to search |

### Expansion Behavior

#### Without Expansion (default)
```
Search: "budget report" → Returns 5 chunks from 5 different files
```

#### With `include_related=true`
```
Search: "budget report" → Returns:
  - 5 initial chunks
  + All other chunks from files sharing same `relationship_id`
  = Complete context from related documents (e.g., entire email thread)
```

#### With `include_ancestors=true`
```
Search: "budget report" → Returns:
  - 5 initial chunks
  + All chunks from ancestor files (parent_id chain)
  = Historical context (e.g., original email + all parent replies)
```

:::info
**Parameter Usage Guidelines**

The two expansion parameters serve distinct purposes and should be used based on your relationship type:

**For folder-based relationships:**
- Use `include_related=true` to retrieve all files from the same folder
- Do not use `include_ancestors` (folders don't have parent-child hierarchy)

**For email thread relationships:**
- Use `include_related=true` to retrieve the entire conversation thread
- Use `include_ancestors=true` to retrieve only parent emails in the reply chain

**Note:** These parameters should not be combined in a single request.
:::

### Deduplication

The expansion algorithm automatically deduplicates chunks based on their `_id` field to prevent duplicate content in results.

### Limiting Results

When using `include_related` or `include_ancestors`, set reasonable `related_limit` values to prevent excessive expansion:

```python
# Default limit is 20 additional chunks per expansion
related_limit: int = 20
```
> For very large threads or folders, pagination or lazy loading may be considered if necessary in the future.

## **6. Usage with the RAG Pipeline**

Document relationships integrate seamlessly into the RAG pipeline workflow:

```
Query → Hybrid Search → Reranking → Data Expansion → LLM
```

**Pipeline Integration:**

1. **Initial Retrieval**: Hybrid search (semantic + keyword) retrieves top-k most relevant chunks
2. **Reranking**: Results are reranked based on relevance scores
3. **Context Expansion**: Related and/or ancestor chunks are fetched based on relationships
4. **LLM Processing**: Expanded context is sent to the language model for answer generation

**Considerations for Context Expansion:**

- **Context Window Limitations**: Data expansion can significantly increase token count, potentially exceeding LLM context limits
- **Quality vs Quantity**: More context doesn't always mean better answers; filtering strategies may be needed
- **Potential Solutions**:
  - **Map-Reduce**: Process chunks in batches, summarize individually, then combine summaries
  - **Relevance Filtering**: Apply additional filtering after expansion to keep only the most relevant chunks

## **7. Extension to Other Use Cases**

Another use case this feature supports is **document versioning**. When multiple versions of a document are indexed, simple semantic search doesn't guarantee retrieving the most up-to-date version. 

**Solution:**  
When calling the indexation endpoint, users can provide a shared tag between document versions using `relationship_id`. This enables fetching all related versions and selecting the correct one through time-based ordering or filtering.