# OCIProxy Helm chart

Deploy **one independent node per release**, with a node-local UI, accounts,
credentials and data. Chart 0.3 changes the deployment and replication model;
follow the repository's [ISOLATION.md](ISOLATION.md) before upgrading old
volumes. Do not mix legacy nodes with public-v2 nodes.

## Primary

Images are published to [GitHub Container Registry](https://github.com/chainfirelabs/ociproxy/pkgs/container/ociproxy)
as `ghcr.io/chainfirelabs/ociproxy:1.0.2` and `ghcr.io/chainfirelabs/ociproxy/ui:1.0.2`.
The chart defaults to both (its `appVersion`); set `image.tag` and
`ui.image.tag` together to pin another version.
The [README](../README.md#install-with-helm) has complete production values
files for a primary and a replica, including Ingress and TLS.

```sh
helm upgrade --install primary charts/ociproxy --namespace registry --create-namespace \
  -f common.yaml -f primary.yaml
```

Defaults deploy one primary. The chart creates a local Secret and generates its
administration secret and bootstrap password, preserving them across upgrades.
The secret belongs to this node and UI. Never copy it to a replica.

Configure the primary's peer allowlist through a protected values file:

```yaml
replication:
  peers:
    site-a:
      token: replace-with-site-a-replication-token
      publish: false
```

## Each replica is a separate release

```sh
helm upgrade --install site-a charts/ociproxy --namespace registry \
  -f common.yaml -f replica.yaml
```

`replica.yaml` sets `primary.enabled: false`, `replica.enabled: true` and
`replica.primaryUrl` to the primary's public HTTPS registry address.

The replica identity defaults to the release name (`site-a`); set
`replication.id` if needed. Its separate Secret contains a generated
`replication-token`. Register ONLY that token under the matching peer ID on the
primary. Alternatively supply the same randomly generated peer token on both
sides using protected secret management. Do not share the local `secret` or
`push-password`. Each replica has its own bootstrap login and user database.

`primary.enabled` and `replica.enabled` cannot both be true. Replica count must
be one; create additional releases for additional nodes. Each UI targets its
release's node, including sign-in, users, private sources and cached images.
There is no fallback to the primary's account management.

The generated authentication Secret contains `secret`, `push-password`,
`replication-peers` (JSON), and `replication-token`. If using `auth.existingSecret`,
provide the first three keys on a primary, plus `replication-token` on a replica.
Use a different local authentication Secret per node. Optional upstream proxy
and OIDC secret keys follow the existing values settings.

## Sources and network access

`config.upstreams` and `config.helmRepos` belong to THIS node. Defaults mark
Docker Hub, GHCR and Quay anonymous/public/shared explicitly. New source entries
without explicit settings default to local/private. Service credentials are
allowed only for local sources; never attach them to shared public sources.
Each node's local administrator may manage its own private sources in the UI.

Replicas import the public catalog without replacing their private configuration.
A peer needs explicit `publish: true` permission to publish anonymous public
sources. It can update/delete only its own managed public entries. Private
Docker and Helm fetches go directly upstream from whichever node owns them.

When nodes or the OIDC provider use certificates from an internal CA, give the
release that CA: each replica needs the one that signed the primary's
certificate, and any node whose UI uses OIDC needs the provider's. Create a
ConfigMap (or Secret) holding the PEM file and name it in `trustedCA`; every
`.crt`/`.pem` key is trusted, on top of the public CAs, by the registry
(replication, upstream registries) and the UI (OIDC):

```sh
kubectl -n registry create configmap internal-ca --from-file=internal-ca.crt
```

```yaml
trustedCA:
  configMapName: internal-ca   # or secretName: ...
```

NetworkPolicy is optional. Egress is unrestricted by default so private sources
can be reached. Setting `networkPolicy.replicaExtraEgress` enables explicit
replica egress restrictions; include its remote primary, private registries,
Helm servers, auth/CDN hosts and any proxy. DNS rules are included automatically.
`networkPolicy.primaryEgress` restricts primary upstream access similarly.
No primary-to-replica fan-out connection or credential is needed.

## Ingress

Enable `ingress.primary` (or `ingress.replica`) and `ingress.ui` per release,
each with its own host and TLS Secret, and set `ui.publicUrl` to the UI's
HTTPS address. Registry Ingresses publish only `/v2/` and `/helm/`, plus
`/replication/` on the primary. `/internal/` and `/metrics` stay reachable only
inside the cluster. Override with `ingress.<component>.paths` if needed.
Registry traffic needs unlimited body size, 900-second timeouts and no request or
response buffering; [values.yaml](../charts/ociproxy/values.yaml) lists the
ingress-nginx annotations.

Persistence, probes, resource limits, ServiceMonitor and OIDC settings
are in [values.yaml](../charts/ociproxy/values.yaml). Configure them per node/release. Registry authentication
and UI identities are local even when public cached content is shared.
