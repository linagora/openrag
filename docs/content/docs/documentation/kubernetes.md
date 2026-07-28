---
title: Deploying OpenRAG on Kubernetes
---

This guide explains how to deploy the **OpenRAG** stack on a Kubernetes cluster using Helm.

---

## Prerequisites

- A **Kubernetes cluster** with **GPU nodes** available (NVIDIA runtime) and nvidia-gpu-operator installed.
- A **StorageClass** that supports **ReadWriteMany** (`RWX`) access mode.  
  This is required because the Ray cluster workers and the OpenRAG app need to access the same shared volumes (e.g. for `.venv`, model weights, logs, data).
- If using ingress, the ingress-nginx controller needs to be installed on the cluster.

---

## Steps

1. **Create a `values.yaml` file**:

   - Copy or create a new `values.yaml` at the root of your repo.
   - You can see the full example file inside the chart:
     [values.yaml](https://github.com/linagora/openrag/blob/dev/charts/openrag-stack/values.yaml)
   - Customize the values you need (e.g., image tags, resources, ingress host, storage class, environment variables, secrets).

2. **Set environment and secrets**:

   - Edit the `env.config` and `env.secrets` sections in your `values.yaml`.
   - Secrets (API keys, tokens, Hugging Face credentials, etc.) will be mounted into the cluster as Kubernetes secrets.
   - For managed PostgreSQL, point the `POSTGRES_*` values at the external database and disable database auto-creation.

3. **Install or upgrade the release from GHCR**:

   ```bash
   helm upgrade\
      --install openrag oci://ghcr.io/linagora/openrag-stack\
      -f ./values.yaml\
      --version 0.1.0
   ```

   - `openrag` is the Helm release name.
   - `oci://ghcr.io/linagora/openrag-stack` is the remote chart location.
   - `-f ./values.yaml` specifies your custom configuration.
   - `--version 0.1.0` ensures you deploy a specific chart version.

---

## Notes

- If using a public IP instead of a hostname, you can leave `ingress.host` empty in your `values.yaml`.  
  The ingress will then match all hosts.

- If you later configure a hostname + TLS (via cert-manager), just update `ingress.host` and redeploy.

- Ensure your GPU nodes have the correct NVIDIA drivers and `nvidia` `RuntimeClass` configured.

- `OPENRAG_INTERNAL_URL` is set for you, to `http://<release>-openrag:<openrag.service.port>`. Out-of-process workers — the evaluation runner, which drives its corpus upload and promptfoo over HTTP — run in the RayCluster pod and use it to reach the API. The built-in default is the compose service name and does not resolve here. Override it only if you front the API with a different in-cluster Service.

## Managed PostgreSQL

The chart can run against a database that is provisioned outside OpenRAG, which is the recommended setup on OpenShift or cloud-managed PostgreSQL.

Pre-create the database before installing the release. If `POSTGRES_DATABASE` is not set, OpenRAG uses `partitions_for_collection_<VDB_COLLECTION_NAME>`. The app role does not need `CREATEDB` or superuser rights; it needs to connect to that database and own, or be allowed to create objects in, the target schema.

In `values.yaml`, disable the bundled PostgreSQL chart, set `postgresProvisioning.autoCreateDatabase` to `false`, set `postgresProvisioning.runMigrationsInApp` to `false`, and enable `postgresProvisioning.migrationJob`. Then provide the managed database connection through `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, and optionally `POSTGRES_DATABASE`.

The migration Job (`templates/postgres-migration-job.yaml`) is a Helm hook, annotated with `helm.sh/hook: pre-install,pre-upgrade`. You never invoke it directly: Helm runs it automatically as part of each `helm install` and `helm upgrade`, before it creates or updates the OpenRAG Deployment, and waits for it to finish. It applies the Alembic migrations against the pre-created database (it migrates the schema but does not create the database). The OpenRAG API then starts against an already-migrated schema.

When `postgresProvisioning.migrationJob` is disabled (the default), the Job is not rendered at all and the application runs migrations itself at startup instead.
