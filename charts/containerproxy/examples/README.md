# Existing authentication Secret examples

These files contain placeholders, not usable credentials. Copy them to a private
location outside the repository, replace every placeholder, and adjust namespaces
to match your releases. Generate each distinct secret/password/token independently,
for example with `openssl rand -hex 32`. Only the site-a replication token is
intentionally identical on both nodes. Keep completed files out of Git.

Both releases should use:

```yaml
auth:
  existingSecret: containerproxy-auth
```

The replica must additionally use:

```yaml
primary:
  enabled: false
replica:
  enabled: true
  replicaCount: 1
  primaryUrl: https://primary.example.com
replication:
  id: site-a
config:
  upstreams: []
  helmRepos: []
```

The namespaces must already exist. Apply your completed private copies before
starting the corresponding releases:

```sh
kubectl apply -f /private/path/primary-auth-secret.yaml
kubectl apply -f /private/path/replica-auth-secret.yaml
```

The primary's `replication-peers` value is JSON. Add a separate entry with its own
ID and token for each additional replica. Set `publish` to `false` for replicas
that should only consume the public catalog. With `auth.existingSecret`, manage
these keys in the Secret itself; the chart does not populate them from values.

`push-password` bootstraps the local account database. Editing it does not reset
an existing user's password. Existing installations must follow
[the offline migration guide](../../../ISOLATION.md#existing-deployments-explicit-offline-migration)
before adopting the new credentials and protocol.
