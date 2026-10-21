# ContainerProxy Helm chart

Deploy **one independent node per release**, with a node-local UI, accounts,
credentials and data. Chart 0.3 changes the deployment and replication model;
follow the repository's [ISOLATION.md](ISOLATION.md) before upgrading old
volumes. Do not mix legacy nodes with public-v2 nodes.

## Primary

Images use [GitHub Container Registry](https://github.com/chainfirelabs/containerproxy/pkgs/container/containerproxy).
Replace `YOUR_TAG` with a published tag. The current GitHub workflow builds only;
these paths become pullable after image publication.

```sh
helm install cp charts/containerproxy --namespace registry --create-namespace \
  --set image.repository=ghcr.io/chainfirelabs/containerproxy \
  --set ui.image.repository=ghcr.io/chainfirelabs/containerproxy/ui \
  --set image.tag=YOUR_TAG --set ui.image.tag=YOUR_TAG
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
helm install site-a charts/containerproxy --namespace registry \
  --set primary.enabled=false --set replica.enabled=true \
  --set replica.primaryUrl=https://primary.example.com \
  --set image.repository=ghcr.io/chainfirelabs/containerproxy \
  --set ui.image.repository=ghcr.io/chainfirelabs/containerproxy/ui \
  --set image.tag=YOUR_TAG --set ui.image.tag=YOUR_TAG
```

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

NetworkPolicy is optional. Egress is unrestricted by default so private sources
can be reached. Setting `networkPolicy.replicaExtraEgress` enables explicit
replica egress restrictions; include its remote primary, private registries,
Helm servers, auth/CDN hosts and any proxy. DNS rules are included automatically.
`networkPolicy.primaryEgress` restricts primary upstream access similarly.
No primary-to-replica fan-out connection or credential is needed.

Persistence, probes, resource limits, ingress, ServiceMonitor and OIDC settings
are in [values.yaml](../charts/containerproxy/values.yaml). Configure them per node/release. Registry authentication
and UI identities are local even when public cached content is shared.
