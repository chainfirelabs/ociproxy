# Configuration and operations

Run commands from the repository root. For a quick start, see the [README](../README.md).

## Configuration and standalone use

Python 3.11 or later is required. Each node has its own storage directory:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
ociproxy serve --config ociproxy.toml
```

Environment variables prefixed `OCIPROXY_` override the TOML file.
See [ociproxy.toml.example](../ociproxy.toml.example) for a complete
primary example and [ISOLATION.md](ISOLATION.md#configuration) for peer setup.

| Variable | Purpose |
| --- | --- |
| `ROLE` | `primary` or `replica` |
| `SECRET` | This node's local administration and encryption secret |
| `STORAGE_PATH` | Independent blob store, account database and metadata |
| `LISTEN`, `PORT` | Registry listener |
| `PUSH_USERNAME`, `PUSH_PASSWORD` | This node's bootstrap administrator |
| `PRIMARY_URL` | Replica's public coordinator URL |
| `REPLICATION_ID`, `REPLICATION_TOKEN` | Replica's outbound peer identity/token |
| `REPLICATION_PEERS` | Primary's JSON peer allowlist, including publication permissions |
| `UPSTREAMS`, `HELM_REPOS` | JSON arrays of this node's configured sources |
| `MANAGED_SOURCES` | Allow this node's admins to manage additional local sources |
| `DEFAULT_VISIBILITY` | Visibility of paths without a specific source/rule setting |
| `TAG_TTL` | Cached tag/index freshness, default 300 seconds |
| `UPSTREAM_PROXY` | This node's upstream HTTP proxy |
| `GC_INTERVAL` | Maintenance interval, default 3600 seconds |
| `GC_MANIFEST_MAX_IDLE_DAYS` | Mirrored manifest retention, zero keeps indefinitely |
| `GC_BLOB_GRACE` | Minimum orphan blob age before deletion, default 86400 seconds |
| `UPLOAD_IDLE_TIMEOUT`, `UPLOAD_MAX_SESSIONS` | Bound unfinished pushes |
| `LOG_FORMAT` | `text` or `json` |

The table omits the common `OCIPROXY_` prefix. `HTTP_PROXY`, `HTTPS_PROXY`
and `NO_PROXY` apply to upstream traffic; internal replication ignores environment
proxies. Replication and local administration credentials must differ. Each peer
token on the primary must also be unique.

## Docker and Helm

```sh
# Shared public source from the example configuration:
docker pull localhost:5001/dockerhub/library/alpine:latest
# With official_namespace="library", the shorter form also works:
docker pull localhost:5001/dockerhub/alpine:latest

# OCI Helm artifacts use the registry API:
helm pull oci://localhost:5001/dockerhub/bitnamicharts/nginx --plain-http

# Classic HTTP Helm repository (explicitly shared in the example):
helm repo add jetstack http://localhost:5001/helm/jetstack
helm repo update
```

Private source pulls require an account/password or personal token on THAT node.
Do not assume logging in to one node grants access to another.

```sh
docker login localhost:5001 -u alice
docker pull localhost:5001/company/team/app:latest
helm repo add company http://localhost:5001/helm/companycharts \
  --username alice --password '<node-local-password-or-token>'
```

A local `pusher` or `admin` may push to either role. Pushes are stored locally;
they do not publish a source or replicate automatically. Prefixes configured as
upstream mirrors cannot be overwritten by a push.

Tags are revalidated after `tag_ttl`; digest-pinned content is immutable. Tag
lists merge cached tags with the source's list. Blob downloads are streamed and
coalesced, SHA-256 verified, and cached even if the initiating client disconnects.
Cached files support Range requests. Helm index rewriting preserves scalar
formatting and timestamps; chart digests are verified when supplied by the index.

## Accounts, UI and OIDC

Each node supports `reader`, `pusher` and `admin` roles. Local passwords are
scrypt-hashed. Personal token secrets are only displayed once and stored hashed;
they may expire or be revoked on that node. OIDC users can create CLI tokens but
have no local password. Bootstrap accounts cannot create personal tokens.

```sh
ociproxy user add alice --role reader --config /path/to/this-node.toml
ociproxy user list --config /path/to/this-node.toml
ociproxy token create alice --name laptop --config /path/to/this-node.toml
```

CLI commands act on the selected node's database and never replicate accounts.
Local API changes invalidate local credential caches immediately; direct CLI
changes are picked up by the running process's short credential-cache expiry.

The UI is a separate package/image. Set `OCIPROXY_URL` to the LOCAL node,
`OCIPROXY_SECRET` to that node's local secret, and `UI_PUBLIC_URL` to its
browser-facing URL. HTTPS public URLs enable Secure cookies. Sessions are signed,
HttpOnly, SameSite=Lax and bound to the backend URL. State-changing browser API
calls require the CSRF header. Users and source management require local admin.

Optional OIDC is configured per UI with `OIDC_ISSUER`, `OIDC_CLIENT_ID`,
`OIDC_CLIENT_SECRET` and `UI_PUBLIC_URL`. The callback is
`<UI_PUBLIC_URL>/auth/oidc/callback`. Configure the provider's username/group claims
and local `OIDC_ADMIN_GROUP`, `OIDC_PUSHER_GROUP`, `OIDC_READER_GROUP` mappings.
The authorization-code flow uses PKCE, state and nonce checks. Use independent
client configuration per node as appropriate; account provisioning remains local.

## HTTP surface

The Docker/OCI API lives under `/v2/`; classic Helm under `/helm/<name>/`.
`/v2/` remains public for health checks. `/v2/_catalog` lists this node's repository
names and is authenticated when it contains private paths. Manifests, tags,
blobs and Helm requests enforce this node's visibility rules.

Local administration endpoints under `/internal/` require this node's local
secret. Account/source mutations also require a valid local acting user and role:

```text
POST /internal/auth/login                 local password sign-in
POST /internal/auth/oidc                  local OIDC provisioning
GET  /internal/me                        local profile and tokens
POST /internal/me/password               change local password
POST /internal/me/tokens                 create local personal token
GET/POST /internal/users                 local account administration
PATCH/DELETE /internal/users/<name>      local account administration
GET /internal/overview                   this node's state
POST /internal/sync                      request public sync (local admin)
GET /internal/cleanup                    local cache, pins, policy and cleanup jobs
POST /internal/cleanup/<action>          preview/execute/pin/policy/run (local admin)
GET /internal/images                     this node's cache
GET /internal/upstreams                  this node's merged source list
POST /internal/sources                   add a source (local/private by default)
PUT/DELETE /internal/sources/<kind>/<name>
POST /internal/sources/test              probe from this node
```

Public machine endpoints accept only authorized peer credentials on a primary:

```text
POST /replication/v2/register
GET  /replication/v2/catalog
GET  /replication/v2/changelog?since&wait
GET/HEAD /replication/v2/pull/manifest
GET  /replication/v2/pull/tags
GET/HEAD /replication/v2/pull/blob/<digest>
POST /replication/v2/sources              authorized public catalog publishers
```

`/internal/register`, `/internal/changelog`, `/internal/pull/*` and
`/internal/ingest/*` are removed. Replication credentials cannot access local
administration or private content. TLS is required when credentials cross hosts.

## Maintenance and testing

`/metrics` exposes Prometheus counters and gauges using the node's own local
secret (also accepted as a bearer token there). Scrape nodes with their respective
credentials. Progress acknowledgments and last-pull timestamps are attributed to
authenticated peer IDs. Garbage collection is independent on each node, expires
idle mirrored manifests when enabled, and removes unreferenced blobs after their
grace period. Local pushed manifests are not aged out as mirrored content.

```sh
pip install -e '.[test]' -e ./webapp
pytest -q
helm lint charts/ociproxy
helm template cp charts/ociproxy >/dev/null
docker compose --env-file .env.example config --quiet
```

Tests include multi-node isolation, independent users/tokens/UI sessions,
Docker/Helm private fetches during coordinator outages, public publication and
revocation, ownership checks, legacy migration, streaming, authentication,
revalidation, garbage collection and protocol input validation.

Known limits: public catalog changes require the primary; offline nodes converge
when they reconnect. Previously public content cannot be made secret by deleting
it later. Changing sharing scope requires delete/recreate. Helm `.prov` files are
not proxied. The project has one public coordinator, without leader election.
