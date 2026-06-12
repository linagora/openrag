import { http, HttpResponse } from "msw";

const API = "http://localhost:8000";

// --- Sample Data ---

const partitions = [
  {
    name: "legal-docs",
    exists: true,
    description: "Corporate legal documents and contracts",
    dimension: 1024,
    collection_name: "col_legal_docs",
    chat_history_depth: 3,
    indexation_preset: "default",
    retrieval_preset: "default",
    indexation_overrides: {},
    retrieval_overrides: {},
    indexation_pipeline: { chunking: { strategy: "recursive", chunk_size: 512, chunk_overlap: 50 }, embedder: "bge-m3", image_captioning: false },
    retrieval_pipeline: { type: "multiquery", embedder: "bge-m3", reranker: "bge-reranker-v2", llm: "llama-3.1", top_k: 10 },
    created_at: "2024-11-20T09:00:00Z",
  },
  {
    name: "technical-manuals",
    exists: true,
    description: "Product technical documentation and user manuals",
    dimension: 1024,
    collection_name: "col_tech_manuals",
    chat_history_depth: 5,
    indexation_preset: "high-quality",
    retrieval_preset: "precise",
    indexation_overrides: { contextualization: true },
    retrieval_overrides: {},
    indexation_pipeline: { chunking: { strategy: "sentence", chunk_size: 256, chunk_overlap: 30 }, embedder: "bge-m3", image_captioning: true, vlm: "qwen-vl" },
    retrieval_pipeline: { type: "hyde", embedder: "bge-m3", reranker: "bge-reranker-v2", llm: "llama-3.1", top_k: 15 },
    created_at: "2024-12-05T14:30:00Z",
  },
  {
    name: "hr-policies",
    exists: true,
    description: "Human resources policies and employee handbook",
    dimension: 768,
    collection_name: null,
    chat_history_depth: 0,
    indexation_preset: "default",
    retrieval_preset: "default",
    indexation_overrides: {},
    retrieval_overrides: {},
    indexation_pipeline: { chunking: { strategy: "fixed", chunk_size: 400, chunk_overlap: 40 }, embedder: "bge-m3" },
    retrieval_pipeline: { type: "simple", embedder: "bge-m3", reranker: "bge-reranker-v2", top_k: 5 },
    created_at: "2025-01-10T11:15:00Z",
  },
];

const documents = [
  { id: "doc-001", filename: "contract-template-2024.pdf", content_type: "application/pdf", partition: "legal-docs", chunk_count: 47, file_size_bytes: 2458000, status: "INDEXED", error_message: null, job_id: "job-001", created_at: "2025-01-15T10:30:00Z", updated_at: "2025-01-15T10:35:00Z", indexed_at: "2025-01-15T10:35:00Z" },
  { id: "doc-002", filename: "employee-handbook-v3.pdf", content_type: "application/pdf", partition: "hr-policies", chunk_count: 128, file_size_bytes: 5120000, status: "INDEXED", error_message: null, job_id: "job-002", created_at: "2025-01-14T09:00:00Z", updated_at: "2025-01-14T09:12:00Z", indexed_at: "2025-01-14T09:12:00Z" },
  { id: "doc-003", filename: "api-reference-guide.md", content_type: "text/markdown", partition: "technical-manuals", chunk_count: 85, file_size_bytes: 890000, status: "INDEXED", error_message: null, job_id: null, created_at: "2025-01-13T14:20:00Z", updated_at: "2025-01-13T14:22:00Z", indexed_at: "2025-01-13T14:22:00Z" },
  { id: "doc-004", filename: "quarterly-report-Q4.pdf", content_type: "application/pdf", partition: "legal-docs", chunk_count: 0, file_size_bytes: 3200000, status: "PROCESSING", error_message: null, job_id: "job-003", created_at: "2025-01-16T08:00:00Z", updated_at: "2025-01-16T08:01:00Z", indexed_at: null },
  { id: "doc-005", filename: "installation-guide.html", content_type: "text/html", partition: "technical-manuals", chunk_count: 0, file_size_bytes: 150000, status: "QUEUED", error_message: null, job_id: "job-003", created_at: "2025-01-16T08:00:00Z", updated_at: "2025-01-16T08:00:00Z", indexed_at: null },
  { id: "doc-006", filename: "corrupted-file.pdf", content_type: "application/pdf", partition: "legal-docs", chunk_count: 0, file_size_bytes: 45000, status: "FAILED", error_message: "Unable to parse PDF: file appears to be corrupted or password-protected", job_id: "job-001", created_at: "2025-01-15T10:30:00Z", updated_at: "2025-01-15T10:31:00Z", indexed_at: null },
];

const jobs = [
  { id: "job-001", status: "SUCCESS", total_documents: 3, partition: "legal-docs", created_at: "2025-01-15T10:30:00Z", updated_at: "2025-01-15T10:35:00Z", started_at: "2025-01-15T10:30:05Z", completed_at: "2025-01-15T10:35:00Z" },
  { id: "job-002", status: "SUCCESS", total_documents: 1, partition: "hr-policies", created_at: "2025-01-14T09:00:00Z", updated_at: "2025-01-14T09:12:00Z", started_at: "2025-01-14T09:00:02Z", completed_at: "2025-01-14T09:12:00Z" },
  { id: "job-003", status: "RUNNING", total_documents: 2, partition: "technical-manuals", created_at: "2025-01-16T08:00:00Z", updated_at: "2025-01-16T08:02:00Z", started_at: "2025-01-16T08:00:10Z", completed_at: null },
  { id: "job-004", status: "QUEUED", total_documents: 5, partition: "legal-docs", created_at: "2025-01-16T09:00:00Z", updated_at: "2025-01-16T09:00:00Z", started_at: null, completed_at: null },
  { id: "job-005", status: "FAILED", total_documents: 1, partition: "hr-policies", created_at: "2025-01-12T16:00:00Z", updated_at: "2025-01-12T16:01:00Z", started_at: "2025-01-12T16:00:03Z", completed_at: "2025-01-12T16:01:00Z" },
  { id: "job-006", status: "PARTIAL", total_documents: 4, partition: "legal-docs", created_at: "2025-01-13T11:00:00Z", updated_at: "2025-01-13T11:08:00Z", started_at: "2025-01-13T11:00:05Z", completed_at: "2025-01-13T11:08:00Z" },
];

const users = [
  { id: "usr-001", email: "admin@openrag.io", display_name: "System Admin", role: "superadmin", is_active: true, created_at: "2024-12-01T00:00:00Z", updated_at: "2025-01-10T12:00:00Z" },
  { id: "usr-002", email: "alice@company.com", display_name: "Alice Johnson", role: "admin", is_active: true, created_at: "2025-01-05T10:00:00Z", updated_at: "2025-01-15T09:00:00Z" },
  { id: "usr-003", email: "bob@company.com", display_name: "Bob Smith", role: "user", is_active: true, created_at: "2025-01-08T14:30:00Z", updated_at: "2025-01-08T14:30:00Z" },
  { id: "usr-004", email: "carol@company.com", display_name: "Carol Davis", role: "user", is_active: false, created_at: "2025-01-10T08:00:00Z", updated_at: "2025-01-14T16:00:00Z" },
];

const modelEndpoints = [
  { name: "bge-m3", model_type: "embedder", endpoint: "http://tei-embedder:8080", model_name: "BAAI/bge-m3", batch_size: 64, timeout: 30, extra: {}, created_at: "2024-12-15T00:00:00Z", updated_at: "2025-01-10T00:00:00Z" },
  { name: "bge-reranker-v2", model_type: "reranker", endpoint: "http://tei-reranker:8080", model_name: "BAAI/bge-reranker-v2-m3", batch_size: 32, timeout: 30, extra: {}, created_at: "2024-12-15T00:00:00Z", updated_at: "2025-01-10T00:00:00Z" },
  { name: "llama-3.1", model_type: "llm", endpoint: "http://vllm:8000/v1", model_name: "meta-llama/Llama-3.1-8B-Instruct", batch_size: 1, timeout: 120, extra: { max_tokens: 2048 }, created_at: "2024-12-20T00:00:00Z", updated_at: "2025-01-12T00:00:00Z" },
  { name: "qwen-vl", model_type: "vlm", endpoint: "http://vllm-vlm:8000/v1", model_name: "Qwen/Qwen2-VL-7B-Instruct", batch_size: 1, timeout: 60, extra: {}, created_at: "2025-01-05T00:00:00Z", updated_at: "2025-01-05T00:00:00Z" },
];

const presets = [
  { name: "default", preset_type: "indexation", config: { chunking_strategy: "recursive", chunk_size: 512, chunk_overlap: 50, embedder: "bge-m3", image_captioning: false, contextualization: false, batch_size: 16 }, created_at: "2024-12-15T00:00:00Z", updated_at: "2025-01-10T00:00:00Z" },
  { name: "high-quality", preset_type: "indexation", config: { chunking_strategy: "sentence", chunk_size: 256, chunk_overlap: 30, embedder: "bge-m3", image_captioning: true, vlm: "qwen-vl", contextualization: true, batch_size: 8 }, created_at: "2025-01-02T00:00:00Z", updated_at: "2025-01-14T00:00:00Z" },
  { name: "default", preset_type: "retrieval", config: { type: "simple", embedder: "bge-m3", reranker: "bge-reranker-v2", top_k: 10, rerank_top_k: 5 }, created_at: "2024-12-15T00:00:00Z", updated_at: "2025-01-10T00:00:00Z" },
  { name: "precise", preset_type: "retrieval", config: { type: "multiquery", embedder: "bge-m3", reranker: "bge-reranker-v2", llm: "llama-3.1", top_k: 15, rerank_top_k: 5, num_queries: 3 }, created_at: "2025-01-03T00:00:00Z", updated_at: "2025-01-12T00:00:00Z" },
];

const prompts = [
  // _default (global) prompts
  { id: "prm-def-001", partition: "_default", prompt_type: "rag_system", name: "Default RAG System", content: "You are a helpful AI assistant. Use the provided context to answer the user's question accurately. If you cannot find the answer in the context, say so clearly. Always cite the source documents.", is_active: true, created_at: "2024-12-20T00:00:00Z", updated_at: "2025-01-05T00:00:00Z" },
  { id: "prm-def-002", partition: "_default", prompt_type: "contextualization", name: "Default Contextualizer", content: "Given the following chunk from a document, provide additional context that helps understand this excerpt within the broader document. Focus on what section this belongs to and what key concepts are being discussed.", is_active: true, created_at: "2024-12-20T00:00:00Z", updated_at: "2025-01-05T00:00:00Z" },
  { id: "prm-def-003", partition: "_default", prompt_type: "multiquery", name: "Default MultiQuery", content: "Generate 3 alternative search queries for the following question. Each query should approach the topic from a different angle to improve retrieval coverage.\n\nOriginal query: {query}", is_active: true, created_at: "2024-12-20T00:00:00Z", updated_at: "2025-01-05T00:00:00Z" },
  { id: "prm-def-004", partition: "_default", prompt_type: "hyde", name: "Default HyDE", content: "Write a hypothetical document passage that would answer the following question. Include specific details that would appear in a real document.\n\nQuestion: {query}", is_active: true, created_at: "2024-12-20T00:00:00Z", updated_at: "2025-01-05T00:00:00Z" },
  { id: "prm-def-005", partition: "_default", prompt_type: "vlm_caption", name: "Default Image Captioner", content: "Describe this image in detail. Identify any text, diagrams, charts, tables, or visual elements. Provide a structured description that would help someone searching for this content.", is_active: true, created_at: "2024-12-20T00:00:00Z", updated_at: "2025-01-05T00:00:00Z" },

  // legal-docs prompts
  { id: "prm-001", partition: "legal-docs", prompt_type: "rag_system", name: "Legal Assistant", content: "You are a helpful legal assistant. Answer questions about contracts and legal documents accurately. Always cite the source document and relevant clause numbers.", is_active: true, created_at: "2025-01-10T00:00:00Z", updated_at: "2025-01-10T00:00:00Z" },
  { id: "prm-002", partition: "legal-docs", prompt_type: "rag_system", name: "Legal Assistant v2 (concise)", content: "You are a legal expert. Provide brief, precise answers citing specific contract clauses. Use bullet points for clarity.", is_active: false, created_at: "2025-01-14T00:00:00Z", updated_at: "2025-01-14T00:00:00Z" },
  { id: "prm-003", partition: "legal-docs", prompt_type: "contextualization", name: "Legal Context", content: "Given the following document chunk from a legal document, provide additional context that helps understand this excerpt within the broader legal agreement. Focus on which party is being referenced and what obligations are described.", is_active: true, created_at: "2025-01-11T00:00:00Z", updated_at: "2025-01-11T00:00:00Z" },
  { id: "prm-004", partition: "legal-docs", prompt_type: "multiquery", name: "Legal MultiQuery", content: "Generate 3 alternative search queries for the following legal question. Each query should approach the topic from a different angle (e.g. by clause type, by party, by obligation).\n\nOriginal query: {query}", is_active: true, created_at: "2025-01-12T00:00:00Z", updated_at: "2025-01-12T00:00:00Z" },

  // technical-manuals prompts
  { id: "prm-005", partition: "technical-manuals", prompt_type: "rag_system", name: "Tech Support", content: "You are a technical support assistant. Help users find information in product documentation. Be concise and provide step-by-step instructions when applicable. Include code examples where relevant.", is_active: true, created_at: "2025-01-11T00:00:00Z", updated_at: "2025-01-13T00:00:00Z" },
  { id: "prm-006", partition: "technical-manuals", prompt_type: "contextualization", name: "Tech Contextualizer", content: "Given this chunk from a technical manual, add context about which product feature or API endpoint is being described. Mention any prerequisites or related sections.", is_active: true, created_at: "2025-01-12T00:00:00Z", updated_at: "2025-01-12T00:00:00Z" },
  { id: "prm-007", partition: "technical-manuals", prompt_type: "vlm_caption", name: "Diagram Captioner", content: "Describe this technical diagram or screenshot in detail. Identify UI elements, data flows, architecture components, or code structures shown. Provide a structured description that would help someone searching for this content.", is_active: true, created_at: "2025-01-13T00:00:00Z", updated_at: "2025-01-13T00:00:00Z" },
  { id: "prm-008", partition: "technical-manuals", prompt_type: "hyde", name: "Tech HyDE", content: "Write a hypothetical technical documentation passage that would answer the following question. Include specific technical details, configuration examples, and step-by-step instructions.\n\nQuestion: {query}", is_active: true, created_at: "2025-01-14T00:00:00Z", updated_at: "2025-01-14T00:00:00Z" },

  // hr-policies prompts
  { id: "prm-009", partition: "hr-policies", prompt_type: "rag_system", name: "HR Bot", content: "You are an HR assistant. Answer employee questions about company policies. Always refer to the official policy document and provide the relevant section number.", is_active: false, created_at: "2025-01-12T00:00:00Z", updated_at: "2025-01-12T00:00:00Z" },
  { id: "prm-010", partition: "hr-policies", prompt_type: "rag_system", name: "HR Bot v2 (friendly)", content: "You are a friendly HR assistant helping employees navigate company policies. Answer in a warm, approachable tone while remaining accurate. Always cite the policy section for reference.", is_active: true, created_at: "2025-01-15T00:00:00Z", updated_at: "2025-01-15T00:00:00Z" },
];

// --- Chunk sample data generator ---
function generateChunks(docId: string, count: number) {
  const sampleTexts: Record<string, string[]> = {
    "doc-001": [
      "# Master Service Agreement\n\nThis Master Service Agreement (the \"Agreement\") is entered into as of January 1, 2024, by and between **Company A** (\"Client\") and **Company B** (\"Service Provider\").",
      "## 1. Definitions\n\n**\"Confidential Information\"** means any information disclosed by either party that is marked as confidential or that reasonably should be understood to be confidential given the nature of the information and circumstances of disclosure.\n\n**\"Services\"** means the professional services described in one or more Statements of Work executed under this Agreement.",
      "## 2. Scope of Services\n\nService Provider shall perform the Services described in each Statement of Work (\"SOW\") executed by the parties. Each SOW shall specify:\n\n- Description of Services\n- Deliverables\n- Timeline and milestones\n- Fees and payment schedule\n- Acceptance criteria",
      "## 3. Term and Termination\n\n### 3.1 Term\nThis Agreement shall commence on the Effective Date and continue for a period of **twelve (12) months**, unless terminated earlier in accordance with this Section 3.\n\n### 3.2 Termination for Convenience\nEither party may terminate this Agreement upon **thirty (30) days** prior written notice to the other party.",
      "### 3.3 Termination for Cause\n\nEither party may terminate this Agreement immediately upon written notice if:\n\n1. The other party materially breaches this Agreement and fails to cure such breach within **fifteen (15) days** after receiving written notice\n2. The other party becomes insolvent or files for bankruptcy\n3. The other party ceases to conduct business in the normal course",
      "## 4. Payment Terms\n\n### 4.1 Fees\nClient shall pay Service Provider the fees specified in each SOW. All fees are in **USD** unless otherwise specified.\n\n### 4.2 Invoicing\nService Provider shall invoice Client monthly in arrears. Payment is due within **net thirty (30) days** of receipt of invoice.\n\n### 4.3 Late Payments\nLate payments shall accrue interest at the rate of **1.5% per month** or the maximum rate permitted by law, whichever is less.",
      "## 5. Intellectual Property\n\n### 5.1 Pre-existing IP\nEach party retains all rights to its pre-existing intellectual property.\n\n### 5.2 Work Product\nAll work product created by Service Provider specifically for Client under this Agreement shall be **owned by Client** upon full payment of applicable fees.\n\n### 5.3 License\nService Provider grants Client a non-exclusive, perpetual license to use any Service Provider tools or methodologies incorporated into the deliverables.",
      "## 6. Confidentiality\n\n### 6.1 Obligations\nEach party agrees to:\n\n- Hold the other party's Confidential Information in strict confidence\n- Not disclose Confidential Information to third parties without prior written consent\n- Use Confidential Information only for purposes of this Agreement\n- Protect Confidential Information using at least the same degree of care used to protect its own confidential information\n\n### 6.2 Exceptions\nConfidential Information does not include information that:\n\n1. Is or becomes publicly available through no fault of the receiving party\n2. Was known to the receiving party prior to disclosure\n3. Is independently developed without use of Confidential Information",
      "## 7. Limitation of Liability\n\n### 7.1 Cap\nNeither party's aggregate liability under this Agreement shall exceed the **total fees paid or payable** in the twelve (12) months preceding the claim.\n\n### 7.2 Exclusion\nNeither party shall be liable for any **indirect, incidental, special, consequential, or punitive damages**, including loss of profits, data, or business opportunities.\n\n### 7.3 Exceptions\nThe limitations in this Section 7 shall not apply to:\n- Breaches of confidentiality obligations\n- Indemnification obligations\n- Willful misconduct or gross negligence",
      "## 8. Indemnification\n\nService Provider shall indemnify, defend, and hold harmless Client from any third-party claims arising from:\n\n1. Service Provider's negligence or willful misconduct\n2. Infringement of third-party intellectual property rights by the deliverables\n3. Violation of applicable laws by Service Provider\n\nClient shall provide prompt written notice of any claim and reasonable cooperation in the defense thereof.",
      "## 9. Governing Law and Dispute Resolution\n\n### 9.1 Governing Law\nThis Agreement shall be governed by the laws of the **State of Delaware**, without regard to conflict of laws principles.\n\n### 9.2 Arbitration\nAny dispute arising under this Agreement shall be resolved through **binding arbitration** administered by the American Arbitration Association under its Commercial Arbitration Rules.\n\n### 9.3 Venue\nThe arbitration shall take place in **Wilmington, Delaware**.",
      "## 10. General Provisions\n\n### 10.1 Entire Agreement\nThis Agreement, together with all SOWs, constitutes the entire agreement between the parties.\n\n### 10.2 Amendments\nNo modification shall be effective unless in writing and signed by authorized representatives of both parties.\n\n### 10.3 Assignment\nNeither party may assign this Agreement without the prior written consent of the other party.\n\n### 10.4 Severability\nIf any provision is held unenforceable, the remaining provisions shall continue in full force and effect.",
    ],
    "doc-002": [
      "# Employee Handbook v3\n\n## Welcome to Our Company\n\nWelcome to the team! This handbook outlines our policies, procedures, and the benefits available to all employees. Please read it carefully and keep it for future reference.",
      "## Employment Policies\n\n### Equal Opportunity\nWe are an equal opportunity employer. We do not discriminate based on race, color, religion, sex, national origin, age, disability, or any other protected status.\n\n### At-Will Employment\nEmployment with the Company is **at-will**, meaning either party may terminate the employment relationship at any time, with or without cause or notice.",
      "## Compensation & Benefits\n\n### Pay Schedule\nEmployees are paid **bi-weekly** on alternating Fridays. Direct deposit is available and encouraged.\n\n### Health Insurance\nFull-time employees are eligible for health insurance coverage beginning on the **first day of the month** following 30 days of employment.\n\n### 401(k) Plan\nEmployees may participate in the company's 401(k) plan after **90 days** of employment. The company matches contributions up to **4%** of salary.",
      "## Time Off\n\n### Paid Time Off (PTO)\n- **0-2 years**: 15 days per year\n- **3-5 years**: 20 days per year\n- **6+ years**: 25 days per year\n\n### Sick Leave\nEmployees receive **10 days** of sick leave per year. Unused sick leave carries over up to a maximum of 30 days.\n\n### Holidays\nThe company observes **11 paid holidays** per year:\n\n| Holiday | Date |\n|---------|------|\n| New Year's Day | January 1 |\n| MLK Jr. Day | 3rd Monday in January |\n| Presidents' Day | 3rd Monday in February |\n| Memorial Day | Last Monday in May |\n| Independence Day | July 4 |\n| Labor Day | 1st Monday in September |\n| Thanksgiving | 4th Thursday in November |\n| Day after Thanksgiving | 4th Friday in November |\n| Christmas Eve | December 24 |\n| Christmas Day | December 25 |\n| New Year's Eve | December 31 |",
      "## Code of Conduct\n\n### Professional Behavior\nAll employees are expected to:\n\n1. Treat colleagues, clients, and partners with **respect and dignity**\n2. Maintain a **professional appearance** appropriate to the workplace\n3. Communicate openly and honestly\n4. Report any concerns about workplace safety or ethics\n\n### Anti-Harassment Policy\nThe company has a **zero-tolerance policy** for harassment of any kind. This includes:\n- Sexual harassment\n- Verbal abuse or intimidation\n- Discriminatory jokes or comments\n- Retaliation against those who report harassment",
    ],
    "doc-003": [
      "# OpenRAG API Reference\n\n## Overview\n\nThe OpenRAG API provides a RESTful interface for document management, indexing, and retrieval. All endpoints use JSON for request and response bodies.\n\n## Base URL\n\n```\nhttps://api.openrag.io/v1\n```\n\n## Authentication\n\nAll API requests require a Bearer token in the Authorization header:\n\n```bash\ncurl -H \"Authorization: Bearer <your-token>\" https://api.openrag.io/v1/health\n```",
      "## Endpoints\n\n### POST /retrieve\n\nPerform a retrieval query against a partition.\n\n**Request Body:**\n```json\n{\n  \"query\": \"What are the termination clauses?\",\n  \"partition\": \"legal-docs\",\n  \"top_k\": 10\n}\n```\n\n**Response:**\n```json\n{\n  \"results\": [\n    {\n      \"chunk_id\": \"chunk-001\",\n      \"text\": \"Either party may terminate...\",\n      \"score\": 0.95,\n      \"metadata\": {\n        \"document\": \"contract-template-2024.pdf\",\n        \"page\": 3\n      }\n    }\n  ]\n}\n```",
      "### POST /admin/indexing/document\n\nIndex a single document into a partition.\n\n**Request (multipart/form-data):**\n\n| Field | Type | Required | Description |\n|-------|------|----------|-------------|\n| file | File | Yes | The document file to index |\n| partition | string | Yes | Target partition name |\n| metadata | JSON | No | Additional metadata |\n| tags | string[] | No | Document tags |\n\n**Response:**\n```json\n{\n  \"document_id\": \"doc-abc123\",\n  \"partition\": \"legal-docs\",\n  \"chunk_count\": 47,\n  \"status\": \"success\"\n}\n```",
      "### Error Handling\n\nThe API uses standard HTTP status codes:\n\n| Status | Meaning |\n|--------|---------|\n| `200` | Success |\n| `201` | Created |\n| `400` | Bad Request — invalid parameters |\n| `401` | Unauthorized — invalid or missing token |\n| `403` | Forbidden — insufficient permissions |\n| `404` | Not Found — resource doesn't exist |\n| `409` | Conflict — resource already exists |\n| `422` | Unprocessable Entity — validation error |\n| `500` | Internal Server Error |\n\n**Error Response Format:**\n```json\n{\n  \"detail\": \"Partition 'my-partition' not found\",\n  \"error_code\": \"PARTITION_NOT_FOUND\"\n}\n```",
    ],
  };

  // Document-level metadata (auto-extracted, stored on every chunk)
  const docMetadata: Record<string, Record<string, unknown>> = {
    "doc-001": { doc_type: "pdf", filename: "contract-template-2024.pdf", category: "contract", year: 2024, quarter: null, month: 1, auto_tags: ["service agreement", "contract", "legal", "termination", "payment"], language: "en" },
    "doc-002": { doc_type: "pdf", filename: "employee-handbook-v3.pdf", category: "policy", year: 2024, quarter: null, month: null, auto_tags: ["employee handbook", "HR policy", "benefits", "PTO", "code of conduct"], language: "en" },
    "doc-003": { doc_type: "markdown", filename: "api-reference-guide.md", category: "manual", year: 2025, quarter: null, month: null, auto_tags: ["API", "REST", "documentation", "endpoints", "authentication"], language: "en" },
  };

  const docTexts = sampleTexts[docId];
  const meta = docMetadata[docId] ?? { doc_type: "text", filename: "unknown" };
  const chunks = [];
  for (let i = 0; i < count; i++) {
    const text = docTexts ? docTexts[i % docTexts.length] : `## Chunk ${i + 1}\n\nThis is sample chunk content for chunk index ${i}. It contains text that was extracted from the source document during the indexing process.\n\n- Point A\n- Point B\n- Point C`;
    chunks.push({
      id: `${docId}-chunk-${String(i).padStart(3, "0")}`,
      document_id: docId,
      chunk_index: i,
      chunk_type: i === 0 ? "title" : "text",
      text,
      content: text,
      context: i > 0 ? `This chunk is from section ${Math.floor(i / 3) + 1} of the document.` : null,
      header: docTexts ? (text.match(/^#+ (.+)/m)?.[1] ?? null) : `Section ${Math.floor(i / 3) + 1}`,
      page_number: Math.floor(i / 4) + 1,
      token_count: 50 + Math.floor(Math.random() * 200),
      metadata: { ...meta, contextualized: i > 0 },
    });
  }
  return chunks;
}

// Pre-generate chunks for documents that have chunk_count > 0
const documentChunks: Record<string, ReturnType<typeof generateChunks>> = {};
for (const doc of documents) {
  if (doc.chunk_count > 0) {
    documentChunks[doc.id] = generateChunks(doc.id, doc.chunk_count);
  }
}

// --- Handlers ---

export const handlers = [
  // Auth
  http.post(`${API}/api/v1/auth/login`, () => {
    return HttpResponse.json({
      access_token: "header.eyJzdWIiOiJ1c3ItMDAxIiwidHlwZSI6ImFjY2VzcyIsImVtYWlsIjoiYWRtaW5AbWFuZHJhZ29yYS5pbyIsInJvbGUiOiJzdXBlcmFkbWluIn0=.signature",
      refresh_token: "mock-refresh-token",
      token_type: "bearer",
    });
  }),

  http.post(`${API}/api/v1/auth/refresh`, () => {
    return HttpResponse.json({
      access_token: "header.eyJzdWIiOiJ1c3ItMDAxIiwidHlwZSI6ImFjY2VzcyIsImVtYWlsIjoiYWRtaW5AbWFuZHJhZ29yYS5pbyIsInJvbGUiOiJzdXBlcmFkbWluIn0=.signature",
      token_type: "bearer",
    });
  }),

  // Single per-user API token regenerate (OpenRag model — one rotating token).
  http.post(`${API}/api/v1/user/account/regenerate-token`, () => {
    const rand = Array.from({ length: 32 }, (_, i) => "0123456789abcdef"[(i * 7 + 3) % 16]).join("");
    return HttpResponse.json({ token: `or-${rand}` });
  }),

  // Partitions
  http.get(`${API}/api/v1/admin/partitions`, ({ request }) => {
    const url = new URL(request.url);
    const search = url.searchParams.get("search")?.toLowerCase();
    const limit = parseInt(url.searchParams.get("limit") ?? "100");
    let filtered = partitions;
    if (search) filtered = filtered.filter((p) => p.name.toLowerCase().includes(search));
    return HttpResponse.json({ partitions: filtered.slice(0, limit) });
  }),

  http.get(`${API}/api/v1/admin/partitions/:name`, ({ params }) => {
    const p = partitions.find((x) => x.name === params.name);
    if (!p) return HttpResponse.json({ detail: "Not found" }, { status: 404 });
    return HttpResponse.json(p);
  }),

  http.post(`${API}/api/v1/admin/partitions`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    const created = { ...partitions[0], ...body, exists: true, indexation_pipeline: {}, retrieval_pipeline: {}, created_at: new Date().toISOString() };
    return HttpResponse.json(created);
  }),

  http.put(`${API}/api/v1/admin/partitions/:name`, ({ params }) => {
    const p = partitions.find((x) => x.name === params.name);
    return HttpResponse.json(p || partitions[0]);
  }),

  http.delete(`${API}/api/v1/admin/partitions/:name`, ({ params }) => {
    return HttpResponse.json({ deleted: params.name });
  }),

  http.get(`${API}/api/v1/admin/partitions/:name/users`, () => {
    return HttpResponse.json({
      partition: "legal-docs",
      users: [
        { user_id: "usr-001", partition: "legal-docs", role: "owner", created_at: "2025-01-10T00:00:00Z" },
        { user_id: "usr-002", partition: "legal-docs", role: "reader", created_at: "2025-01-12T00:00:00Z" },
      ],
    });
  }),

  // Documents
  http.get(`${API}/api/v1/admin/documents`, ({ request }) => {
    const url = new URL(request.url);
    const partition = url.searchParams.get("partition");
    const status = url.searchParams.get("status");
    let filtered = documents;
    if (partition) filtered = filtered.filter((d) => d.partition === partition);
    if (status) filtered = filtered.filter((d) => d.status === status);
    return HttpResponse.json({ documents: filtered, total: filtered.length, offset: 0, limit: 50 });
  }),

  http.get(`${API}/api/v1/admin/documents/:id`, ({ params }) => {
    const doc = documents.find((d) => d.id === params.id);
    if (!doc) return HttpResponse.json({ detail: "Not found" }, { status: 404 });
    return HttpResponse.json(doc);
  }),

  http.delete(`${API}/api/v1/admin/documents/:id`, ({ params }) => {
    return HttpResponse.json({ deleted: true, document_id: params.id });
  }),

  http.get(`${API}/api/v1/admin/documents/:id/chunks`, ({ params }) => {
    const chunks = documentChunks[params.id as string];
    if (!chunks) return HttpResponse.json({ chunks: [], total: 0 });
    return HttpResponse.json({ chunks, total: chunks.length });
  }),

  http.post(`${API}/api/v1/admin/documents/:id/copy`, ({ params }) => {
    const doc = documents.find((d) => d.id === params.id);
    return HttpResponse.json({ ...doc, id: "doc-new-copy", partition: "hr-policies" });
  }),

  // Indexing
  http.post(`${API}/api/v1/admin/indexing/document`, () => {
    return HttpResponse.json({ document_id: "doc-new-001", partition: "legal-docs", chunk_count: 32, status: "success" });
  }),

  http.post(`${API}/api/v1/admin/indexing/batch`, () => {
    return HttpResponse.json({ job_id: "job-new-001", status: "QUEUED", total_documents: 3 }, { status: 202 });
  }),

  // Jobs
  http.get(`${API}/api/v1/admin/jobs`, ({ request }) => {
    const url = new URL(request.url);
    const status = url.searchParams.get("status");
    let filtered = jobs;
    if (status) filtered = filtered.filter((j) => j.status === status);
    return HttpResponse.json({ jobs: filtered, offset: 0, limit: 50 });
  }),

  http.get(`${API}/api/v1/admin/jobs/:id/events`, () => {
    // SSE mock: emit a few chunk_stored events then job_completed
    const encoder = new TextEncoder();
    const events = [
      { type: "chunk_stored", doc_id: "doc-001", filename: "contract-template-2024.pdf", chunk_index: 0, stored: true, timing: { parse_s: 12.4, caption_s: 3.2, chunk_s: 0.5 } },
      { type: "chunk_stored", doc_id: "doc-001", filename: "contract-template-2024.pdf", chunk_index: 1, stored: true, timing: { parse_s: 12.4, caption_s: 3.2, chunk_s: 0.5, contextualize_s: 1.1, embed_s: 0.3, store_s: 0.2 } },
      { type: "chunk_stored", doc_id: "doc-006", filename: "corrupted-file.pdf", chunk_index: 0, stored: true, timing: { parse_s: 8.1, chunk_s: 0.3 } },
      { type: "job_completed", job_id: "job-003", status: "SUCCESS" },
    ];
    let index = 0;
    const stream = new ReadableStream({
      start(controller) {
        const interval = setInterval(() => {
          if (index >= events.length) {
            clearInterval(interval);
            controller.close();
            return;
          }
          controller.enqueue(encoder.encode(`data: ${JSON.stringify(events[index])}\n\n`));
          index++;
        }, 1500);
      },
    });
    return new HttpResponse(stream, {
      headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
    });
  }),

  http.get(`${API}/api/v1/admin/jobs/:id`, ({ params }) => {
    const job = jobs.find((j) => j.id === params.id);
    if (!job) return HttpResponse.json({ detail: "Not found" }, { status: 404 });
    return HttpResponse.json({
      ...job,
      documents: [
        { id: "doc-001", filename: "contract-template-2024.pdf", status: "INDEXED", chunk_count: 47, error_message: null, stage_timings: { parse_s: 12.4, caption_s: 3.2, chunk_s: 0.5, contextualize_s: 8.7, embed_s: 1.2, store_s: 0.8 } },
        { id: "doc-006", filename: "corrupted-file.pdf", status: "FAILED", chunk_count: 0, error_message: "Unable to parse PDF", stage_timings: null },
        { id: "doc-002", filename: "employee-handbook-v3.pdf", status: "INDEXED", chunk_count: 128, error_message: null, stage_timings: { parse_s: 45.1, caption_s: 0.0, chunk_s: 1.2, contextualize_s: 32.5, embed_s: 4.8, store_s: 2.1 } },
      ],
    });
  }),

  // Users
  http.get(`${API}/api/v1/admin/users`, () => {
    return HttpResponse.json({ users, offset: 0, limit: 50 });
  }),

  http.get(`${API}/api/v1/admin/users/:id`, ({ params }) => {
    const user = users.find((u) => u.id === params.id);
    if (!user) return HttpResponse.json({ detail: "Not found" }, { status: 404 });
    return HttpResponse.json(user);
  }),

  http.post(`${API}/api/v1/admin/users`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({ id: "usr-new", ...body, is_active: true, created_at: new Date().toISOString(), updated_at: new Date().toISOString() });
  }),

  http.patch(`${API}/api/v1/admin/users/:id`, ({ params }) => {
    const user = users.find((u) => u.id === params.id);
    return HttpResponse.json(user || users[0]);
  }),

  http.delete(`${API}/api/v1/admin/users/:id`, () => {
    return new HttpResponse(null, { status: 204 });
  }),

  // Regenerate a user's single API token (admin on behalf of the user).
  http.post(`${API}/api/v1/admin/users/:id/regenerate_token`, () => {
    const rand = Array.from({ length: 32 }, (_, i) => "0123456789abcdef"[(i * 11 + 5) % 16]).join("");
    return HttpResponse.json({ token: `or-${rand}` });
  }),

  http.post(`${API}/api/v1/admin/users/:id/api-keys`, () => {
    return HttpResponse.json({
      id: "key-001",
      key_prefix: "mnd_ak_7f3b",
      name: "dev-key",
      is_active: true,
      created_at: new Date().toISOString(),
      expires_at: null,
      raw_key: "mnd_ak_7f3b2c9e4d1a8f5b6e3c0d7a9f2b4e6c8d0a1b3e5f7c9d",
    });
  }),

  http.delete(`${API}/api/v1/admin/users/:id/api-keys/:keyId`, () => {
    return new HttpResponse(null, { status: 204 });
  }),

  http.post(`${API}/api/v1/admin/users/:id/partitions`, () => {
    return HttpResponse.json({ user_id: "usr-001", partition: "legal-docs", role: "reader", created_at: new Date().toISOString() });
  }),

  http.delete(`${API}/api/v1/admin/users/:id/partitions/:partition`, () => {
    return new HttpResponse(null, { status: 204 });
  }),

  // Model Endpoints
  http.get(`${API}/api/v1/admin/model-endpoints`, ({ request }) => {
    const url = new URL(request.url);
    const type = url.searchParams.get("model_type");
    let filtered = modelEndpoints;
    if (type) filtered = filtered.filter((m) => m.model_type === type);
    return HttpResponse.json({ endpoints: filtered });
  }),

  http.get(`${API}/api/v1/admin/model-endpoints/:type/:name`, ({ params }) => {
    const ep = modelEndpoints.find((m) => m.model_type === params.type && m.name === params.name);
    if (!ep) return HttpResponse.json({ detail: "Not found" }, { status: 404 });
    return HttpResponse.json(ep);
  }),

  http.post(`${API}/api/v1/admin/model-endpoints`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({ ...modelEndpoints[0], ...body, created_at: new Date().toISOString(), updated_at: new Date().toISOString() });
  }),

  http.put(`${API}/api/v1/admin/model-endpoints/:type/:name`, ({ params }) => {
    const ep = modelEndpoints.find((m) => m.model_type === params.type && m.name === params.name);
    return HttpResponse.json(ep || modelEndpoints[0]);
  }),

  http.delete(`${API}/api/v1/admin/model-endpoints/:type/:name`, ({ params }) => {
    return HttpResponse.json({ deleted: `${params.type}/${params.name}` });
  }),

  // Presets
  http.get(`${API}/api/v1/admin/presets`, ({ request }) => {
    const url = new URL(request.url);
    const type = url.searchParams.get("preset_type");
    let filtered = presets;
    if (type) filtered = filtered.filter((p) => p.preset_type === type);
    return HttpResponse.json({ presets: filtered });
  }),

  http.get(`${API}/api/v1/admin/presets/:type/:name`, ({ params }) => {
    const preset = presets.find((p) => p.preset_type === params.type && p.name === params.name);
    if (!preset) return HttpResponse.json({ detail: "Not found" }, { status: 404 });
    return HttpResponse.json(preset);
  }),

  http.post(`${API}/api/v1/admin/presets`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({ ...presets[0], ...body, created_at: new Date().toISOString(), updated_at: new Date().toISOString() });
  }),

  http.put(`${API}/api/v1/admin/presets/:type/:name`, ({ params }) => {
    const preset = presets.find((p) => p.preset_type === params.type && p.name === params.name);
    return HttpResponse.json(preset || presets[0]);
  }),

  http.delete(`${API}/api/v1/admin/presets/:type/:name`, ({ params }) => {
    return HttpResponse.json({ deleted: `${params.type}/${params.name}` });
  }),

  // Prompts
  http.get(`${API}/api/v1/admin/prompts`, ({ request }) => {
    const url = new URL(request.url);
    const partition = url.searchParams.get("partition");
    const type = url.searchParams.get("prompt_type");
    let filtered = prompts;
    if (partition) filtered = filtered.filter((p) => p.partition === partition);
    if (type) filtered = filtered.filter((p) => p.prompt_type === type);
    return HttpResponse.json({ prompts: filtered, offset: 0, limit: 50 });
  }),

  http.get(`${API}/api/v1/admin/prompts/:id`, ({ params }) => {
    const prompt = prompts.find((p) => p.id === params.id);
    if (!prompt) return HttpResponse.json({ detail: "Not found" }, { status: 404 });
    return HttpResponse.json(prompt);
  }),

  http.post(`${API}/api/v1/admin/prompts`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>;
    return HttpResponse.json({ id: "prm-new", ...body, created_at: new Date().toISOString(), updated_at: new Date().toISOString() });
  }),

  http.patch(`${API}/api/v1/admin/prompts/:id`, ({ params }) => {
    const prompt = prompts.find((p) => p.id === params.id);
    return HttpResponse.json(prompt || prompts[0]);
  }),

  http.delete(`${API}/api/v1/admin/prompts/:id`, () => {
    return new HttpResponse(null, { status: 204 });
  }),

  // Pipelines
  http.get(`${API}/api/v1/admin/pipelines/:partition`, ({ params }) => {
    const p = partitions.find((x) => x.name === params.partition);
    return HttpResponse.json({
      partition: params.partition,
      indexation: p?.indexation_pipeline ?? {},
      retrieval: p?.retrieval_pipeline ?? {},
    });
  }),

  http.put(`${API}/api/v1/admin/pipelines/:partition`, ({ params }) => {
    const p = partitions.find((x) => x.name === params.partition);
    return HttpResponse.json({
      partition: params.partition,
      indexation: p?.indexation_pipeline ?? {},
      retrieval: p?.retrieval_pipeline ?? {},
    });
  }),

  // Audit Log
  http.get(`${API}/api/v1/admin/audit-log`, () => {
    return HttpResponse.json({
      items: [
        {
          id: "aud-001",
          user_id: "user-admin",
          actor_email: "admin@example.com",
          action: "create",
          resource_type: "partition",
          resource_id: "legal-docs",
          details_json: null,
          request_id: "req_abc123",
          created_at: "2026-03-27T10:00:00Z",
        },
        {
          id: "aud-002",
          user_id: "user-admin",
          actor_email: "admin@example.com",
          action: "create",
          resource_type: "user",
          resource_id: "user-005",
          details_json: null,
          request_id: "req_def456",
          created_at: "2026-03-27T09:30:00Z",
        },
        {
          id: "aud-003",
          user_id: "user-admin",
          actor_email: "admin@example.com",
          action: "delete",
          resource_type: "document",
          resource_id: "doc-042",
          details_json: null,
          request_id: "req_ghi789",
          created_at: "2026-03-27T09:00:00Z",
        },
      ],
      total: 3,
      offset: 0,
      limit: 50,
    });
  }),

  // System
  http.get(`${API}/api/v1/admin/system/health`, () => {
    return HttpResponse.json({
      status: "ok",
      services: {
        "embedder:bge-m3": true,
        "reranker:bge-reranker-v2": true,
        "llm:llama-3.1": true,
        "vlm:qwen-vl": false,
        "milvus": true,
        "postgresql": true,
      },
    });
  }),

  http.get(`${API}/api/v1/admin/system/metrics`, () => {
    return HttpResponse.text(
      `# HELP openrag_requests_total Total API requests\n# TYPE openrag_requests_total counter\nopenrag_requests_total{method="POST",endpoint="/v1/chat/completions"} 1247\nopenrag_requests_total{method="POST",endpoint="/api/v1/retrieve"} 893\nopenrag_requests_total{method="POST",endpoint="/api/v1/admin/indexing/document"} 156\n# HELP openrag_request_duration_seconds Request duration\n# TYPE openrag_request_duration_seconds histogram\nopenrag_request_duration_seconds_bucket{le="0.1"} 450\nopenrag_request_duration_seconds_bucket{le="0.5"} 1800\nopenrag_request_duration_seconds_bucket{le="1.0"} 2100\nopenrag_request_duration_seconds_bucket{le="5.0"} 2290\nopenrag_request_duration_seconds_bucket{le="+Inf"} 2296\n# HELP openrag_documents_indexed_total Total documents indexed\n# TYPE openrag_documents_indexed_total counter\nopenrag_documents_indexed_total 342\n# HELP openrag_chunks_total Total chunks in vector store\n# TYPE openrag_chunks_total gauge\nopenrag_chunks_total 18947\n`,
    );
  }),

  http.get(`${API}/api/v1/admin/system/config`, () => {
    return HttpResponse.json({
      api: { host: "0.0.0.0", port: 8000, cors_origins: ["http://localhost:5173"], debug: false },
      auth: { jwt_secret: "***", token_expire_minutes: 60 },
      milvus: { host: "milvus", port: 19530, database: "openrag" },
      ray: { address: "ray://ray-head:10001", num_workers: 4 },
      postgresql: { host: "postgres", port: 5432, database: "openrag" },
      storage: { type: "minio", endpoint: "minio:9000", bucket: "documents" },
    });
  }),

  // Chat completions (streaming)
  http.post(`${API}/v1/chat/completions`, async ({ request }) => {
    const body = await request.json();
    const { model, messages: _messages, stream } = body as { model: string; messages: Array<{ role: string; content: string }>; stream: boolean };

    if (!stream) {
      // Non-streaming response
      return HttpResponse.json({
        id: "chatcmpl-mock-001",
        object: "chat.completion",
        created: Math.floor(Date.now() / 1000),
        model,
        choices: [{
          index: 0,
          message: { role: "assistant", content: "This is a mock response from MSW. The LLM would normally generate this based on your question." },
          finish_reason: "stop",
        }],
        usage: { prompt_tokens: 10, completion_tokens: 20, total_tokens: 30 },
      });
    }

    // Streaming response
    const sampleResponse = "This is a **mock streaming response** from MSW.\n\nHere are some key points:\n\n1. Point one\n2. Point two\n3. Point three\n\nThe real LLM would generate actual content based on your question and the retrieved context.";
    const words = sampleResponse.split(" ");
    
    const encoder = new TextEncoder();
    let index = 0;
    
    const streamData = new ReadableStream({
      start(controller) {
        // Send initial chunk
        controller.enqueue(encoder.encode(`data: ${JSON.stringify({
          id: "chatcmpl-mock-001",
          object: "chat.completion.chunk",
          created: Math.floor(Date.now() / 1000),
          model,
          choices: [{ index: 0, delta: { role: "assistant", content: "" }, finish_reason: null }],
        })}\n\n`));

        const interval = setInterval(() => {
          if (index >= words.length) {
            clearInterval(interval);
            // Send final chunk
            controller.enqueue(encoder.encode(`data: ${JSON.stringify({
              id: "chatcmpl-mock-001",
              object: "chat.completion.chunk",
              created: Math.floor(Date.now() / 1000),
              model,
              choices: [{ index: 0, delta: {}, finish_reason: "stop" }],
            })}\n\n`));
            // Send sources event
            controller.enqueue(encoder.encode(`data: ${JSON.stringify({
              type: "mandragora.sources",
              sources: [],
            })}\n\n`));
            // Send DONE
            controller.enqueue(encoder.encode("data: [DONE]\n\n"));
            controller.close();
            return;
          }
          
          controller.enqueue(encoder.encode(`data: ${JSON.stringify({
            id: "chatcmpl-mock-001",
            object: "chat.completion.chunk",
            created: Math.floor(Date.now() / 1000),
            model,
            choices: [{ index: 0, delta: { content: words[index] }, finish_reason: null }],
          })}\n\n`));
          index++;
        }, 100);
      },
    });

    return new HttpResponse(streamData, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
      },
    });
  }),

];
