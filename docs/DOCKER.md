# Production Docker Compose on separate hosts

Run a primary node on one host and a replica node on each replica host. Each
node lives in its own folder holding `compose.yaml`, `.env` and `ociproxy.toml`,
so Compose commands run there without `-f` or `--env-file`. Each stack includes its own
registry, UI and persistent named data volume. They require Docker Compose v2
(2.23.1 or later, for inline configs). Each stack includes Traefik, which
terminates HTTPS. The [README](../README.md#install-with-docker-compose)
has the short install steps; this guide covers the details.

## Prepare images and configuration

Both stacks pull the published GHCR images by default:

- `ghcr.io/chainfirelabs/ociproxy:1.0.3`
- `ghcr.io/chainfirelabs/ociproxy/ui:1.0.3`

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

Edit each `.env`: set the image references, `OCIPROXY_HOST` and
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

Each stack runs [Traefik](https://traefik.io/) in front of the registry and UI.
Traefik is the only container that binds host ports, 80 and 443; the registry
and UI are reachable only on the stack's internal Compose network. Give each
node one DNS name, set it as `OCIPROXY_HOST` in `.env`, and Traefik routes:

| Path on `https://<OCIPROXY_HOST>` | Backend |
| --- | --- |
| `/v2/`, `/helm/`, `/replication/` | registry (`registry:5000`) |
| Everything else | web UI (`ui:8080`) |

`OCIPROXY_HOST` also sets the registry's advertised URL and the UI's public URL
(`https://<OCIPROXY_HOST>`), so OIDC providers need the redirect URI
`https://<OCIPROXY_HOST>/auth/oidc/callback`. `/internal/` and `/metrics` are
not routed publicly; the UI reaches administration endpoints over the Compose
network. Port 80 only redirects to HTTPS.

Traefik passes paths, query strings, Host and Authorization headers through
unchanged, does not buffer, and has no read/write deadline on HTTPS so large
layers can stream.

### Your own certificate

By default Traefik serves the certificate in the `certs` folder next to
`compose.yaml` (set `TLS_CERT_DIR` in `.env` to use another folder):

| File | Contents |
| --- | --- |
| `certs/tls.crt` | PEM certificate for `OCIPROXY_HOST`, then any intermediates (full chain, leaf first) |
| `certs/tls.key` | PEM private key, unencrypted |

```sh
mkdir certs
cp /path/to/fullchain.pem certs/tls.crt
cp /path/to/privkey.pem certs/tls.key
sudo chown root:root certs/tls.crt certs/tls.key && sudo chmod 600 certs/tls.key
```

Traefik runs as root with all capabilities dropped, so it can read a mode-600
key only when root owns it; a key owned by your user fails with
`permission denied` in `docker compose logs traefik` and HTTPS stops working.
Copy real files rather than symlinks (for example from certbot's
`/etc/letsencrypt/live/<name>/`, use `cp -L`), because symlinks pointing outside
the folder do not resolve inside the container. The same certificate is also
served to clients that send no SNI.

Traefik reads the files at startup. After renewing them, run
`docker compose restart traefik`. Check what is being served with
`openssl s_client -connect <OCIPROXY_HOST>:443 -servername <OCIPROXY_HOST> </dev/null | openssl x509 -noout -subject -dates`.

A certificate from a private or internal CA works for clients that trust it.
Docker clients need the CA at `/etc/docker/certs.d/<OCIPROXY_HOST>/ca.crt`
(no daemon restart required), or in the host's system trust store.

### Internal CA trust for the registry

The registry verifies outbound HTTPS (replication to `PRIMARY_URL`, and
upstream registries) against the public CA bundle in the image plus every
`*.crt` / `*.pem` file in the node's `trust` folder (`TRUST_DIR` in `.env`,
default `./trust`, mounted read-only and created empty if missing). When the
primary's certificate is signed by an internal CA, put that CA in each
replica's `trust` folder. The web UI reads the same folder for its OIDC
provider (discovery, token and userinfo requests), so an identity provider
with an internal-CA certificate works the same way:

```sh
mkdir -p trust && cp /path/to/internal-ca.crt trust/ && chmod 644 trust/*.crt
docker compose up -d --force-recreate registry ui
```

Files must be readable by the container's UID 65532. The registry loads them
at startup and logs `trusting N extra CA file(s)`; an unreadable or invalid
file stops it with an error naming the file. Python's strict X.509 profile is
relaxed for these CAs, so internal CAs that Docker and curl accept (for
example ones without a key usage extension) work too. Signature, expiry and
hostname checks still apply. The folder must be readable by UID 65532 (755,
files 644); otherwise the container stops with `not readable by this
container's user`. Traefik does not use this folder.

### Let's Encrypt

Traefik can instead obtain and renew a Let's Encrypt certificate for
`OCIPROXY_HOST` itself, using the TLS-ALPN challenge on port 443. This needs:

- `OCIPROXY_HOST` resolving publicly to the host, and TCP 443 reachable from the
  internet (not only from your clients)
- Docker Compose 2.24.4 or later

Enable it on any node by copying the override file into the node's folder.
Compose merges `compose.override.yaml` into `compose.yaml` automatically, and
the `certs` folder is then not used:

```sh
cp ~/ociproxy-src/docker/compose.letsencrypt.yml compose.override.yaml
```

Optionally add to `.env`:

```sh
ACME_EMAIL=you@example.com
# Test against the staging CA first to avoid rate limits:
ACME_CA_SERVER=https://acme-staging-v02.api.letsencrypt.org/directory
```

Then run `docker compose up -d` and watch `docker compose logs -f traefik`.
Certificates and the ACME account persist in the stack's `acme` volume and
renew automatically. A staging certificate is not trusted by clients: once it
works, remove `ACME_CA_SERVER`, delete the staging certificates with
`docker compose down && docker volume rm ociproxy-primary_acme` (or
`ociproxy-replica_acme`), and run `docker compose up -d` again.

Hosts without inbound internet access cannot use the TLS-ALPN challenge. Use
your own certificate, or adapt the override to Traefik's DNS challenge for your
DNS provider.

To go back to your own certificate, delete `compose.override.yaml`, add the
`certs` folder and run `docker compose up -d`.

The replica must resolve and reach `PRIMARY_URL` over HTTPS, with a certificate
trusted by the image. Replication is outbound from replica to primary; the
primary needs no connection back to the replica. `OCIPROXY_HOST` is the
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
Run one node per host: each stack's Traefik needs host ports 80 and 443.

## Docker Hub mirror

Any node can be a Docker Hub pull-through mirror for Docker daemons. On each
client host, add the node to `/etc/docker/daemon.json` (merge with existing
settings), then `sudo systemctl restart docker`:

```json
{
  "registry-mirrors": ["https://registry-primary.example.com"]
}
```

Check it with `docker info` (listed under `Registry Mirrors`), then
`docker pull nginx` and confirm `GET /v2/library/nginx/...` requests returning
200 in `docker compose logs registry` on the node. Images keep their normal
names (`nginx`, `docker.io/library/nginx`); nothing in Dockerfiles or manifests
changes. Clients may use the replica nearest to them.

How it works:

- A mirroring daemon requests the image's normal path plus the registry it
  stands in for: `docker pull grafana/grafana` arrives as
  `/v2/grafana/grafana/manifests/latest?ns=docker.io`. The node serves that
  from its Docker Hub source (the one whose `base_url` is
  `https://registry-1.docker.io`), exactly like
  `/v2/dockerhub/grafana/grafana/...`, including caching and replication.
- Mirror requests are always anonymous: the daemon never sends credentials to
  a mirror. They get the Docker Hub source's visibility, which is public in the
  example TOML, so every Docker Hub namespace works. Locally pushed images are
  never served or exposed to mirror requests, even with the same name, and
  `DEFAULT_VISIBILITY=private` still applies to them.
- If the node has no source for the requested registry, or the source is
  private, the request fails (404/401) and the daemon silently pulls from
  Docker Hub directly. Check the registry logs to confirm pulls are mirrored.
- `registry-mirrors` applies only to Docker Hub. Pull other registries through
  their prefix, e.g. `docker pull registry-primary.example.com/ghcr/<org>/<image>`.
- The client's Docker daemon must trust the node's certificate. For a private
  CA, put it at `/etc/docker/certs.d/<OCIPROXY_HOST>/ca.crt` on each client.

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
