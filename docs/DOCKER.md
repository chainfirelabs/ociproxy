# Production Docker Compose on separate hosts

Run a primary node on one host and a replica node on each replica host. Each
node lives in its own folder holding `compose.yaml`, `.env` and `ociproxy.toml`,
so Compose commands run there without `-f` or `--env-file`. Each stack includes its own
registry, UI and persistent named data volume. They require Docker Compose v2
or later and a host-level HTTPS reverse proxy. The [README](../README.md#install-with-docker-compose)
has the short install steps; this guide covers the details.

## Prepare images and configuration

Both stacks pull the published GHCR images by default:

- `ghcr.io/chainfirelabs/ociproxy:1.0.1`
- `ghcr.io/chainfirelabs/ociproxy/ui:1.0.1`

`OCIPROXY_IMAGE` and `OCIPROXY_UI_IMAGE` in the environment files set them
explicitly. Keep both on the same version, and pin digests
(`image@sha256:...`) for fully immutable deployments. Stacks never build from
source or use `latest`. To run your own builds instead, publish both images
from one reviewed revision and set both variables:

```sh
REGISTRY=registry.example.com/ociproxy ./build-images.sh --push
```

Get the templates from a checkout, for example
`git clone https://github.com/chainfirelabs/ociproxy.git ~/ociproxy-src`.

On the primary host:

```sh
mkdir ~/ociproxy-primary && cd ~/ociproxy-primary
cp ~/ociproxy-src/docker/docker-compose.primary.yml compose.yaml
cp ~/ociproxy-src/docker/.env.primary.example .env
cp ~/ociproxy-src/ociproxy.toml.example ociproxy.toml
chmod 600 .env
```

On the replica host:

```sh
mkdir ~/ociproxy-replica && cd ~/ociproxy-replica
cp ~/ociproxy-src/docker/docker-compose.replica.yml compose.yaml
cp ~/ociproxy-src/docker/.env.replica.example .env
cp ~/ociproxy-src/ociproxy.replica.toml.example ociproxy.toml
chmod 600 .env
```

Commands in this guide run inside the node's folder. Compose loads
`compose.yaml` and `.env` from there automatically. The TOML defaults to
`./ociproxy.toml` next to `compose.yaml`; set `NODE_CONFIG` in `.env` to use a
different path. The folder can live anywhere, such as `/opt/ociproxy`; the
project name (and its data volume) comes from `compose.yaml`, not the folder name.

Edit each `.env`: set the image references, public DNS names and
replace every `REPLACE_*` value. Generate each local secret and password
independently using `openssl rand -hex 32`. Generate another token for site-a;
put that same token in the primary's `REPLICATION_PEERS.site-a.token` JSON entry
and the replica's `REPLICATION_TOKEN`. The replica's `REPLICATION_ID=site-a`
must match the primary's JSON key. Keep all other credentials independent.
The UI uses only its own registry's local secret and account database.

The primary TOML example shares anonymous Docker Hub, GHCR, Quay and Jetstack
Helm sources. The replica imports those automatically; its TOML can stay empty
or define its own private sources. Both `sharing = "public"` and
`visibility = "public"` are required for a source to replicate. Private sources,
accounts and locally pushed OCI artifacts stay on their own node. OCI charts
use the registry API; classic Helm repositories use `/helm/<name>/`.
Environment settings override TOML, including the persistent storage path.
Mounted TOML must be readable by UID 65532; keep credentials in the protected
environment file or node UI instead of making a secret-bearing TOML public.

## HTTPS and networking

Create DNS records and trusted TLS certificates for each registry and UI name.
On **each host**, configure your existing host reverse proxy as follows:

| HTTPS virtual host | Local backend | Routes |
| --- | --- | --- |
| `registry-primary.example.com` (replica: `registry-replica.example.com`) | `http://127.0.0.1:5000` | `/v2/`, `/helm/`, `/replication/` |
| `registry-primary-ui.example.com` (replica: `registry-replica-ui.example.com`) | `http://127.0.0.1:8080` | All paths |

Preserve request paths, query strings, Host and Authorization headers, forward
the original HTTPS scheme, allow large streaming uploads, disable request/response
buffering for registry traffic, and allow at least 900-second upstream timeouts.
Do not expose `/internal/` or `/metrics` publicly; the UI reaches administration
endpoints over its local Compose network. Avoid additional proxy login middleware
on replication routes: they already authenticate using the peer bearer token.

Open HTTPS port 443 on each host; keep backend ports 5000/8080 private. Defaults
bind these ports to loopback for a proxy running on the host. A containerized
reverse proxy needs an explicitly configured Docker network or reachable private
bind address; its own loopback cannot reach these backends. TLS termination is
required infrastructure and is not included in these Compose files.

The replica must resolve and reach `PRIMARY_URL` over HTTPS, with a certificate
trusted by the image. Replication is outbound from replica to primary; the
primary needs no connection back to the replica. `REGISTRY_PUBLIC_URL` is the
node's advertised address, while `PRIMARY_URL` is the actual replication target.
Neither should use Compose service names across hosts. Each node also needs
DNS and outbound access to its upstream registries, authentication/CDN services,
and optional OIDC provider. `UPSTREAM_PROXY` applies only to upstream fetches.

## Start and verify

On each host, from the node's folder:

```sh
docker compose config --quiet
docker compose pull
docker compose up -d
```

Check `ps` and `logs --tail=100 registry` on both hosts. Container health checks
verify the local HTTP service; verify replication separately in the primary UI
and replica logs. The replica retries if the primary is temporarily unavailable.
Sign in to each UI using that node's bootstrap account, then create local users.
For the default shared sources, test through the replica:

```sh
curl -fsS https://registry-replica.example.com/v2/
docker pull registry-replica.example.com/dockerhub/library/alpine:latest
helm repo add cached-jetstack https://registry-replica.example.com/helm/jetstack/
helm repo update cached-jetstack
```

For more replicas, use a distinct ID, local secret/password and data volume on
each host; add a unique token entry to the primary's `REPLICATION_PEERS` and
recreate its registry service. `publish:false` permits consumption only. Set it
to `true` only if that replica may publish anonymous sources to the shared catalog.
Multiple nodes on one host need separate folders, plus a distinct
`COMPOSE_PROJECT_NAME` and distinct `REGISTRY_PORT` / `UI_PORT` in each `.env`.

## Storage and operation

The project-scoped `data` volume is initialized writable for the image's nonroot
UID 65532. Keep one registry process per volume; never share a live data directory
between nodes. Containers use read-only root filesystems, dropped capabilities,
memory limits, rotated JSON logs and restart policies. An unhealthy container is
reported by Docker; the restart policy restarts exited processes, not health-check
failures. Monitor health, replication progress and disk space externally.

GC defaults begin reclaiming eligible cache data at 85% filesystem usage toward
75%; pinned and pushed content can prevent reaching that target. Tune limits to
your disks and monitor capacity. Back up the full data volume while the stack is
stopped, along with its local secret, protected environment and TOML. The secret
is needed to decrypt saved upstream credentials. `docker compose down` preserves
the volume; `down -v` deletes it.

To upgrade, back up first, update both image references to matching versions,
then run `docker compose pull` and `docker compose up -d` in the node's folder. Migrate legacy volumes using
[the isolation migration instructions](ISOLATION.md#existing-deployments-explicit-offline-migration)
before starting new software against them. Rollback requires the matching backup
and software when a storage schema changes.
