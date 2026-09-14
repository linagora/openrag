# OpenBao ACL policy for an OpenRAG deployment: read-only on the KV v2
# secrets under secret/openrag/*. Bind it to the auth role the deployment
# uses (Kubernetes auth for ESO, AppRole for compose/Ansible hosts).
#
#   bao policy write openrag-read infra/openbao/policy.hcl
#
# Adjust the mount ("secret") and prefix ("openrag") to your namespace layout.

# KV v2 data — what ESO and infra/scripts/openbao_env.py actually read.
path "secret/data/openrag/*" {
  capabilities = ["read"]
}

# KV v2 metadata — lets a client list versions and lets the UI/CLI browse.
path "secret/metadata/openrag/*" {
  capabilities = ["read", "list"]
}

# The UI and `bao kv get` resolve the logical path first; without this they
# answer 403 even though the data/ path above is allowed.
path "secret/openrag/*" {
  capabilities = ["read"]
}
