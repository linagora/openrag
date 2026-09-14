# OpenBao integration

Reference manifests for feeding OpenRAG its secrets from OpenBao (or Vault).
The walkthrough lives in the docs: `docs/content/docs/documentation/openbao_secrets.md`.

| File | Purpose |
|---|---|
| `policy.hcl` | Read-only ACL policy on `secret/openrag/*` (data, metadata and logical paths) |
| `cluster-secret-store.yaml` | External Secrets Operator `ClusterSecretStore` + the ServiceAccount it authenticates with (Kubernetes auth) |

Related pieces elsewhere in the repo:

- `infra/charts/openrag-stack/values-openbao.yaml` — Helm overlay switching the chart to `externalSecret` and wiring the bundled PostgreSQL to the operator-written Secret
- `infra/scripts/openbao_env.py` — renders a compose `.env` from a KV v2 secret (compose / Ansible hosts)
- `infra/ansible/playbooks/openrag.yml` — optional `openbao_*` variables run that script before `docker compose up`
