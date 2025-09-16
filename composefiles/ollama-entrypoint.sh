#!/bin/bash

/bin/ollama serve &
pid=$!

sleep 5

echo "🔴 Retrieve jina embedder model..."
ollama pull jina/jina-embeddings-v2-base-en
echo "🟢 Done!"

echo "🔴 Retrieve bge reranker model..."
ollama pull qllama/bge-reranker-v2-m3:latest
echo "🟢 Done!"

echo "🔴 Retrieve bge reranker model..."
ollama pull qwen3:0.6b
echo "🟢 Done!"

# Wait for Ollama process to finish.
wait $pid