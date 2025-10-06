# OpenRag scripts

## openrag/scripts/embed.py

A command-line utility for generating text embeddings using any **OpenAI-compatible embedding endpoint**.  
It supports adaptive batching for optimal performance and handles both plain and `.xz` compressed [backup files](README-backup.md).

```
python3 openrag/scripts/embed.py \
    -u http://openai-compatible-endpoint/v1 \
    -m Qwen/Qwen3-Embedding-0.6B \
    -k sk-... \
    -b 1024 \
    -i input-file.openrag(.xz) \
    -o output-file.openrag(.xz)
```

This script can also be used in pipelines, reading input from STDIN (`-i -`) and writing output to STDOUT (`-o -`).

