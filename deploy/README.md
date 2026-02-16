# OpenRAG Deployment

Two deployment modes sharing the same infrastructure (Milvus, PostgreSQL, Indexer UI).

## Local (Laptop, no GPU)

Ollama for embeddings, external LLM/VLM, no reranker.

```bash
cd deploy
cp env.local.example .env
# Edit .env with your LLM/VLM endpoints

mkdir -p data volumes/milvus volumes/etcd volumes/minio db ray_mount/logs .cache/huggingface
touch ray_mount/.env

# Stop native Ollama if installed
sudo systemctl stop ollama 2>/dev/null; true

docker compose -f docker-compose.base.yaml -f docker-compose.local.yaml build
docker compose -f docker-compose.base.yaml -f docker-compose.local.yaml up -d
```

## Server (GPU)

vLLM for embeddings, Infinity reranker, all on GPU.

```bash
cd deploy
cp env.server.example .env
# Edit .env with your LLM/VLM endpoints

mkdir -p data volumes/milvus volumes/etcd volumes/minio db ray_mount/logs
touch ray_mount/.env

docker compose -f docker-compose.base.yaml -f docker-compose.server.yaml up -d
```

## Verify

```bash
curl http://localhost:8090/health_check   # local mode (port 8090)
curl http://localhost:8080/health_check   # server mode (port 8080)
```

## UIs

| UI | Local | Server |
|---|---|---|
| Indexer UI | http://localhost:8067 | http://localhost:8067 |
| Chainlit Chat | http://localhost:8090/chainlit | http://localhost:8080/chainlit |
| API Swagger | http://localhost:8090/docs | http://localhost:8080/docs |
| Ray Dashboard | http://localhost:8265 | http://localhost:8265 |
