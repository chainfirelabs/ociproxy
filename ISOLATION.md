# Independent nodes and public-only replication (protocol public-v2)

Each primary or replica has its own account database, bootstrap administrator,
passwords, token hashes, upstream credentials, local secret, sessions and OIDC
configuration. A primary is a coordinator for public content, not an account
server for replicas. The public export is maintained separately and is not
updated by this change.

## What is shared

Only sources explicitly configured with BOTH `sharing = "public"` and
`visibility = "public"` are eligible for sharing. Docker/OCI and classic HTTP
Helm sources follow the same rules. Shared sources must support anonymous
upstream access; attaching service credentials is rejected, because those
credentials could expose private repositories under an otherwise public host.

New sources default to `sharing = "local"`, `visibility = "private"`. A source
can be public to clients on this node but still `sharing = "local"`. Upstream
credentials are encrypted with this node's own local secret and never enter
replication. Local pushed images remain on their node, including public pushes;
they are not implicitly published to the shared source catalog.

The sanitized public catalog, eligible manifests/tags, Helm index/chart pointer
records, and proven public blobs replicate. Blobs require repository ownership and verified public-byte provenance, not
just a digest asserted by a public manifest or an upstream HTTP 200 response.
Privately cached bytes are not reused publicly until the public source produces
bytes that pass SHA-256 verification. Identical
content explicitly available from a public source may be shared even if the
same bytes are also used privately. Users, tokens, private source URLs, private
manifests, private Helm metadata, and arbitrary blobs do not replicate.

Local private namespaces are stored separately (`private:<name>` and
`helm/private:<name>`). They cannot be replaced by a public catalog entry with
the same name or prefix. Public entries have stable, kind-qualified catalog
IDs (`oci:<name>` / `helm:<name>`); names cannot be changed by editing an entry.
A conflicting imported public entry is not activated on that node. Remove the
local conflict or choose a different prefix if you want to use it.

Source scope changes require deletion and recreation. Deleting a shared source
removes its catalog entry and cached manifests on connected replicas; changing
its upstream URL invalidates old cached manifests/ownership grants. A node's
local sources and users survive public catalog updates. Offline nodes converge
when they reconnect; previously public data cannot be made secret retroactively.

## Trust and routing

`CONTAINERPROXY_SECRET` / `[node].secret` is LOCAL to the node and its own UI.
Use independent values and bootstrap passwords on every node. It grants broad
local administration authority and must never be given to a replication peer.
Each UI's `CONTAINERPROXY_URL` points to its own node. The old `UI_NODE_URL`
setting, if supplied, now selects that local backend for ALL UI operations.
Sessions are bound to the backend URL as well as the local signing secret.

Public replication uses separate bearer credentials over `/replication/v2/`.
The primary has a peer-ID-to-token allowlist; replicas each have one outbound
peer ID/token pair. Every token must be unique and different from the local
administration secret. Use TLS between hosts; the bearer token is a credential.
Tokens only authorize public catalog/content and registration/progress calls.
They do not authorize `/internal` administration, source probes or private data.
Peer identity comes from the authenticated peer header, never an arbitrary
acting-user or node header supplied by the caller.

Replication is pull-only: replicas long-poll the primary's public feed, verify
blob hashes and cache content. Legacy fan-out/ingest endpoints are removed. The
primary never needs a credential that can write to a replica. Filtered feed
positions advance even when a batch contains no transferable data. Current
policy is checked again before serving old feed events and blob requests.
Old protocols, account events and individual legacy source events are rejected.

Local administrators can use **Sync now** in either node's dashboard. On a replica,
it interrupts polling or retry backoff, refreshes the public catalog and resumes
the feed from its saved position. On the primary, it wakes connected replicas'
long polls and requests a catalog refresh through the existing public feed.
Disconnected replicas receive the latest refresh request when they reconnect.
The response confirms that sync was requested, not that every replica has finished.
Private sources, users and tokens remain local. This action does not invalidate
upstream tag TTLs or prefetch every image in a registry. The local admin API is
`POST /internal/sync`; it requires the local secret and an acting administrator.

Private sources on any node fetch directly from their upstream. Public sources
on replicas fetch through the coordinator, with cached copies available during
outages. Each replica needs egress to its own private upstreams (and any relevant
authentication/CDN hosts). Local private pulls, user management and sign-in do
not depend on the primary being available.

## Configuration

Primary (local secret/password normally supplied through secrets/environment):

```toml
[node]
role = "primary"

[replication.peers.site-a]
token = "replace-with-a-unique-random-peer-token"
publish = false

[[upstream]]
name = "dockerhub"
prefix = "dockerhub"
base_url = "https://registry-1.docker.io"
official_namespace = "library"
sharing = "public"
visibility = "public"

[[helm]]
name = "jetstack"
base_url = "https://charts.jetstack.io"
sharing = "public"
visibility = "public"
```

Replica:

```toml
[node]
role = "replica"
primary_url = "https://primary.example.com"

[replication]
id = "site-a"
token = "the-token-registered-for-site-a-on-the-primary"

[[upstream]]
name = "company"
base_url = "https://registry.internal.example"
# Local/private are the defaults. Set credentials through a secret or this UI.
```

Equivalent environment variables are `CONTAINERPROXY_REPLICATION_ID`,
`CONTAINERPROXY_REPLICATION_TOKEN`, and (on the primary)
`CONTAINERPROXY_REPLICATION_PEERS`, a JSON object such as
`{"site-a":{"token":"...","publish":false}}`.

A replica's local administrator can add private sources locally without any
primary permission. To submit anonymous public sources to the shared catalog,
its peer entry must explicitly grant `publish = true`. This permission permits
public catalog publication and primary upstream egress; grant it only to
trusted publishers. A peer may update/delete only its own managed public
entries. It cannot adopt another peer's or the primary's sources. The primary's
own local administrator can administer the shared catalog. Failed publication
has no local private-source fallback. Configuration-file entries are read-only
in the UI; explicitly public replica config entries are proposed at connection.

Account and token CLI commands now operate on the node/database named by their
configuration, for either role. No user records are synchronized.

## Existing deployments: explicit offline migration

This is a breaking change. Do not mix old and new nodes or upgrade an old volume
without migration. A database without the isolation schema marker refuses to
serve if it contains legacy data. No live data is migrated automatically.

1. Stop all old nodes and UIs. Keep them stopped throughout migration. Decide
   which sources should actually be shared, and explicitly mark those anonymous
   sources public/shared in their new configuration. Unspecified sources become
   local/private.
2. For each stopped node, run the migration with the actual storage path in its
   configuration and a new backup directory OUTSIDE that storage tree:

   ```sh
   python -m containerproxy.migrate --config /path/to/node.toml \
     --backup /secure/backups/node-before-isolation --stopped
   ```

   The tool copies the full storage tree and takes a consistent SQLite backup.
   Do not run it against a live process. Use a mounted maintenance container if
   the storage directory is owned by the container UID.
3. On the primary, existing account records are retained but disabled, old
   tokens are revoked, and local upstream namespaces are remapped. Eligible
   public cache history is rebuilt on startup. On replicas, formerly shared
   accounts, tokens, source rows and all cached content are reset; their public
   cache is rebuilt from the new protocol. Private replica content cannot be
   reliably distinguished from previously copied primary data, so it is retained
   in the backup rather than trusted in the new store.
4. Generate separate local secrets and bootstrap passwords for ALL nodes, plus
   separate per-replica replication tokens. Never retain a previously shared
   local administration secret. Configure the primary peer allowlist. Re-enter
   encrypted upstream credentials after changing their local encryption secret.
5. Start the primary, then replicas and their local UIs. Bootstrap each local
   account database independently. Reset/re-enable primary accounts as needed
   and issue new local tokens. Verify public sharing and private isolation.

Backups contain formerly shared password hashes, tokens and private layers;
keep them private. Rollback requires restoring the matching stopped-node backup
and old configuration/software, not running old code against the migrated DB.
Migration and credential rotation have NOT been run against your live storage
or `.env` files as part of implementing this feature.

## Deployment packaging

Compose now supplies independent primary/replica secrets, passwords and peer
credentials, provides a separate UI per node (ports 8080/8081/8082), and gives
replicas upstream egress. Copy `.env.example`, generate every secret separately,
and use `REPLICA1_CONFIG` / `REPLICA2_CONFIG` for optional private config files.
Do not blindly copy the primary's private configuration onto replicas.

The Helm chart deploys ONE independent node per release. Defaults deploy a
primary only. Deploy each replica in a separate release with
`primary.enabled=false`, `replica.enabled=true`, `replica.replicaCount=1`, and
`replica.primaryUrl` set. Each release has its own local Secret, data and UI.
A shared multi-replica StatefulSet or primary+replicas release is rejected to
avoid distributing common admin credentials and load-balancing account calls
across unrelated account databases. Do not reuse `auth.existingSecret` across
node releases. Existing custom Secrets also need `replication-peers` (JSON) and,
for replicas, `replication-token` keys. The replica identity defaults to its
release name and can be set with `replication.id`.

Register a replica's `replication-token` on the primary with
`replication.peers.<id>.token`; only that token crosses the trust boundary.
Do not copy its local `secret` or `push-password`. Replica egress is unrestricted
by default, like primary egress; setting `networkPolicy.replicaExtraEgress`
enables an explicit egress policy, so include the primary, DNS and all approved
private upstream/token/CDN destinations. HTTP proxies can be configured per
node. Configure OIDC separately per node/UI if needed.
