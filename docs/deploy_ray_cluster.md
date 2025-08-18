# ⚡ Distributed Deployment in a Ray Cluster

This guide explains how to deploy **OpenRAG** across multiple machines using **Ray** for distributed indexing and processing.

---

## ✅ 1. Set Environment Variables

Ensure your `.env` file includes the standard app variables **plus Ray-specific ones** listed below:

```env
# Ray
# Resources for all files
RAY_NUM_GPUS=0.1
RAY_POOL_SIZE=1
RAY_MAX_TASKS_PER_WORKER=5

# PDF specific resources when using marker
MARKER_MAX_TASKS_PER_CHILD=10
MARKER_MAX_PROCESSES=5 # Number of subprocesses <-> Number of concurrent pdfs per worker
MARKER_MIN_PROCESSES=3 # Minimum number of subprocesses available before triggering a process pool reset.
MARKER_POOL_SIZE=1 # Number of workers (typically 1 worker per cluster node)
MARKER_NUM_GPUS=0.6

SHARED_ENV=/ray_mount/.env
RAY_DASHBOARD_PORT=8265
RAY_ADDRESS=ray://X.X.X.X:10001
HEAD_NODE_IP=X.X.X.X
RAY_HEAD_ADDRESS=X.X.X.X:6379
# RAY_ENABLE_RECORD_ACTOR_TASK_LOGGING=1 # to enable logs at task level in ray dashboard
RAY_task_retry_delay_ms=3000

# Ray volumes
DATA_VOLUME=/ray_mount/data
MODEL_WEIGHTS_VOLUME=/ray_mount/model_weights
CONFIG_VOLUME=/ray_mount/.hydra_config
UV_LINK_MODE=copy
UV_CACHE_DIR=/tmp/uv-cache 
```

✅ Use host IPs instead of Docker service names :

- EMBEDDER_BASE_URL=http://<HOST-IP>:8000/v1  # ✅ instead of http://vllm:8000/v1
- VDB_HOST=<HOST-IP>                          # ✅ instead of VDB_HOST=milvus


> 🧠 **Tips**  
>
> - `RAY_NUM_GPUS` defines **per-actor resource requirements**. Ray will not start a task until these resources are available on one of the nodes.  
>   For example, if one indexation consumes ~1GB of VRAM and your GPU has 4GB, setting `RAY_NUM_GPUS=0.25` allows you to run **4 indexers per node**. In a 2-node cluster, that means up to **8 concurrent indexation tasks**.  
>
> - `RAY_POOL_SIZE` defines the number of worker actors that will be created to handle indexation tasks. It acts like a **maximum concurrency limit**.  
>   Using the previous example, you can set `POOL_SIZE=8` to fully utilize your cluster capacity.  
>   ⚠️ If other GPU-intensive services are running on your nodes (e.g. vLLM, the RAG API), make sure to **reserve enough GPU memory** for them and subtract that from your total when calculating the safe pool size.

---

## 📁 2. Set Up Shared Storage

All nodes need to access shared configuration and data folders.  
We recommend using **GlusterFS** for this.

➡ Follow the [GlusterFS Setup Guide](./setup_glusterfs.md) to configure:

- Shared access to:
  - `.env`
  - `.hydra_config`
  - `/data` (uploaded files)
  - `/model_weights` (embedding model cache)

---

## 🚀 3. Start the Ray Cluster

First, prepare your `cluster.yaml` file. Here's an example for a **local provider**:

```yaml
cluster_name: rag-cluster
provider:
  type: local
  head_ip: 10.0.0.1
  worker_ips: [10.0.0.2]  # Static IPs of other nodes (does not auto-start workers)

docker:
  image: ghcr.io/linagora/openrag-ray
  pull_before_run: true
  container_name: ray_node
  run_options:
    - --gpus all
    - -v /ray_mount/model_weights:/app/model_weights
    - -v /ray_mount/data:/app/data
    - -v /ray_mount/.hydra_config:/app/.hydra_config
    - -v /ray_mount/logs:/app/logs
    - --env-file /ray_mount/.env

auth:
  ssh_user: ubuntu
  ssh_private_key: path/to/private/key # Replace with your actual ssh key path

head_start_ray_commands:
    - uv run ray stop
    - uv run ray start --head --dashboard-host 0.0.0.0 --dashboard-port ${RAY_DASHBOARD_PORT:-8265} --node-ip-address ${HEAD_NODE_IP} --autoscaling-config=~/ray_bootstrap_config.yaml
worker_start_ray_commands:
    - uv run ray stop
    - uv run ray start --address ${HEAD_NODE_IP:-10.0.0.1}:6379
```

> 🛠️ The base image (`ghcr.io/linagora/openrag-ray`) must be built from `Dockerfile.ray` and pushed to a container registry before use.

### ⬆️ Launch the cluster

```bash
uv run ray up -y cluster.yaml
```

## 🐳 4. Launch the OpenRAG App

Use the Docker Compose setup:

```bash
docker compose up -d
```

Once running, **OpenRAG will auto-connect** to the Ray cluster using `RAY_ADDRESS` from `.env`.

---

With this setup, your app is now fully distributed and ready to handle concurrent tasks across your Ray cluster.


## 🛠️ Troubleshooting

### ❌ Permission Denied Errors

If you encounter errors like `Permission denied` when Ray or Docker tries to access shared folders (SQL database, model files, ...), it's likely due to insufficient permissions on the host system.

👉 To resolve this, you can set full read/write/execute permissions on the shared directory:

```bash
sudo chmod -R 777 /ray_mount
```