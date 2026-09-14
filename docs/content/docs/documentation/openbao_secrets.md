---
title: Secrets from OpenBao (or Vault)
---

OpenRAG reads every credential from its environment: there is no secrets
client in the application. Keeping secrets in OpenBao therefore means one
thing — getting them *into* that environment at deploy time without writing
them into a values file or a committed `.env`. Two paths are supported:

| Deployment | Mechanism | What ships in the repo |
|---|---|---|
| Kubernetes / Helm | [External Secrets Operator](https://external-secrets.io/) (ESO) writes the chart's env Secret from OpenBao | `values-openbao.yaml` overlay, `infra/openbao/cluster-secret-store.yaml` |
| Docker compose / Ansible | `infra/scripts/openbao_env.py` renders the `.env` compose loads | the script, an optional Ansible step |

OpenBao is a fork of HashiCorp Vault and keeps its API, so everything below
also works against Vault (`VAULT_*` variables are accepted as fallbacks).

## What counts as a secret

Name the keys of the OpenBao secret after the OpenRAG environment variables.
Everything else (`BASE_URL`, `MODEL`, ports, ...) is plain configuration and
stays in `env.config` / `.env`.

| Key | Used by |
|---|---|
| `AUTH_TOKEN` | API bearer token, bootstraps the admin user |
| `API_KEY`, `VLM_API_KEY`, `EMBEDDER_API_KEY`, `TRANSCRIBER_API_KEY`, `RERANKER_API_KEY` | model endpoints |
| `HF_TOKEN` | Hugging Face downloads (bundled vLLM on Kubernetes) |
| `POSTGRES_PASSWORD` | PostgreSQL — must equal the password the database enforces |
| `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY` | compose stack only (Milvus object store) |
| `CHAINLIT_AUTH_SECRET` | Chainlit session cookies |
| `OIDC_CLIENT_SECRET`, `OIDC_TOKEN_ENCRYPTION_KEY` | SSO (`AUTH_MODE=oidc`) |
| `WEBSEARCH_API_TOKEN` | web search provider |
| `GRAFANA_ADMIN_PASSWORD`, `LOKI_PASSWORD` | monitoring / logging overlays |

A key absent from the secret simply stays unset, so start with the ones your
deployment actually uses.

## OpenBao side

Done once per environment, inside the team namespace. With the `bao` CLI
(`BAO_ADDR` and `BAO_NAMESPACE` exported):

```bash
# One KV v2 secret per environment; keys are the env var names.
bao kv put secret/openrag/staging \
  AUTH_TOKEN="$(openssl rand -hex 16)" \
  POSTGRES_PASSWORD="$(openssl rand -hex 16)" \
  API_KEY=sk-... VLM_API_KEY=sk-... HF_TOKEN=hf_...

# Read-only policy on secret/openrag/* (infra/openbao/policy.hcl).
bao policy write openrag-read infra/openbao/policy.hcl
```

Then enable the auth method the deployment will use and bind the policy to it:

- **Kubernetes auth** for Helm/ESO — no OpenBao credential is stored in the
  cluster; ESO trades a ServiceAccount token for an OpenBao token. The
  commands are in the header of `infra/openbao/cluster-secret-store.yaml`.
- **AppRole** for compose hosts and CI — a `role_id` / `secret_id` pair the
  deploy step logs in with:

  ```bash
  bao auth enable approle
  bao write auth/approle/role/openrag token_policies=openrag-read token_ttl=1h
  bao read auth/approle/role/openrag/role-id
  bao write -f auth/approle/role/openrag/secret-id
  ```

The policy grants `read` on the logical path (`secret/openrag/*`) in addition
to `secret/data/...` and `secret/metadata/...`: without it the UI and
`bao kv get` answer 403 even though the API path is allowed.

## Kubernetes: Helm + External Secrets Operator

1. Install ESO (0.14 or newer, which serves `external-secrets.io/v1`):

   ```bash
   helm repo add external-secrets https://charts.external-secrets.io
   helm upgrade --install external-secrets external-secrets/external-secrets \
     -n external-secrets --create-namespace
   ```

2. Create the store. Edit the server URL, OpenBao namespace and role in
   `infra/openbao/cluster-secret-store.yaml`, then apply it. It also creates
   the `openrag-secrets-reader` ServiceAccount that the OpenBao Kubernetes
   role is bound to.

   ```bash
   kubectl apply -f infra/openbao/cluster-secret-store.yaml
   kubectl get clustersecretstore openbao   # STATUS must be Valid
   ```

3. Deploy with the overlay. `values-openbao.yaml` switches
   `env.secretsProvider.type` to `externalSecret`, points it at
   `openrag/staging` on the `openbao` store, and wires the bundled PostgreSQL
   to read its password from the same Secret:

   ```bash
   helm upgrade --install openrag oci://ghcr.io/linagora/openrag-stack \
     -f values.yaml \
     -f infra/charts/openrag-stack/values-openbao.yaml
   ```

   ESO writes `openrag-env-secrets`; every OpenRAG pod loads it with
   `envFrom`, exactly as with `env.secrets`. Check the sync:

   ```bash
   kubectl get externalsecret openrag-env-secrets   # READY True
   kubectl get secret openrag-env-secrets -o jsonpath='{.data}' | jq 'keys'
   ```

### Why PostgreSQL needs the extra wiring

The bitnami PostgreSQL sub-chart enforces its own credential. With an
operator-managed Secret nothing feeds `postgresql.auth.password` any more, and
left empty bitnami generates a random one at install — OpenRAG's
`POSTGRES_PASSWORD` could never match. The overlay sets
`postgresql.auth.existingSecret` to the ESO-written Secret with
`secretKeys.adminPasswordKey` / `userPasswordKey` = `POSTGRES_PASSWORD`, so
both read one value. The chart refuses to render an operator-managed setup
that leaves this out.

Switching an **existing** release to OpenBao: the database keeps the password
it was initialised with. Either store that current password in OpenBao, or
change it in the database first (`ALTER USER root PASSWORD '...'`).

### Rotation

ESO rewrites the Secret on its `refreshInterval`, but nothing restarts. After
rotating a key in OpenBao, either:

```bash
kubectl annotate externalsecret openrag-env-secrets force-sync=$(date +%s) --overwrite
kubectl rollout restart deployment/openrag-openrag
```

or install [stakater/Reloader](https://github.com/stakater/Reloader) and set
`openrag.annotations` to `reloader.stakater.com/auto: "true"` (commented in
the overlay). Ray head/worker pods (`ray.enabled`) are managed by KubeRay and
must be cycled by hand in both cases, and a rotated `POSTGRES_PASSWORD` must
also be changed in the database.

### Migration Job on a first install

`postgresProvisioning.migrationJob` is a `pre-install` hook: on the very
first install it runs before the ExternalSecret exists and waits forever for
`openrag-env-secrets`. Keep the Job disabled for the first install (the app
migrates at startup by default), or create the ExternalSecret beforehand.

## Docker compose and Ansible

Compose reads secrets from the `.env` file (`SHARED_ENV`). Rather than
keeping that file by hand, render it from OpenBao at deploy time with
`infra/scripts/openbao_env.py` — standard library only, so it runs on any
host with `python3`:

```bash
export BAO_ADDR=https://poc-obao.linagora.com
export BAO_NAMESPACE=openrag
export BAO_ROLE_ID=... BAO_SECRET_ID=...      # or BAO_TOKEN=...

# Fresh .env from the example plus the secrets
infra/scripts/openbao_env.py --path secret/openrag/staging \
  --base infra/compose/.env.example --out infra/compose/.env

# Later: refresh the secrets in place, everything else untouched
infra/scripts/openbao_env.py --path secret/openrag/staging \
  --base infra/compose/.env --out infra/compose/.env
```

The script rewrites the `KEY=` lines it knows in place, appends the others
under a marker comment, writes the file with mode `0600`, and never prints
values (the summary lists key names only). `--only AUTH_TOKEN,API_KEY` takes
a subset; without `--out` the lines go to stdout. Values are quoted so
compose, `uv --env-file` and python-dotenv read them identically; a value
containing a single quote or a newline is refused rather than mangled.

With Ansible (`infra/ansible`), set the OpenBao variables on the host or
group and the `openrag.yml` playbook runs the same step before starting the
stack, with `no_log` so nothing reaches the play output:

```ini
[cpu_servers:vars]
openbao_addr=https://poc-obao.linagora.com
openbao_namespace=openrag
openbao_kv_path=secret/openrag/staging
openbao_role_id=...
openbao_secret_id=...
```

Pass the two AppRole values from the environment or a vault-encrypted vars
file rather than committing them.

## Troubleshooting

- **`permission denied` / 403** — the token's policy lacks the path. The
  KV v2 API path is `secret/data/openrag/...`; the script and ESO use it, the
  UI uses the logical path (see the policy).
- **`ExternalSecret` stuck `SecretSyncedError`** — `kubectl describe` it. A
  `Kubernetes auth` failure means the OpenBao role's bound ServiceAccount
  name/namespace do not match the store's `serviceAccountRef`.
- **OpenRAG cannot connect to PostgreSQL after switching** — the database
  still enforces its previous password (see above).
- **`no KV v2 data at ...`** — the mount is KV v1, or the path has a typo.
  `--path` must include the mount: `secret/openrag/staging`.
