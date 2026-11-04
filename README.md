# OCIProxy

![OCIProxy](ociproxy.png)

[GitHub repository](https://github.com/chainfirelabs/ociproxy)

Cache Docker/OCI images and Helm charts with a primary and optional replicas.

- **Shared:** explicitly public registries and Helm repositories.
- **Independent:** each node's users, passwords, tokens and private repositories.

Upgrading existing storage? Follow the [migration guide](docs/ISOLATION.md#existing-deployments-explicit-offline-migration) first.

## Install with Docker Compose

Each host runs **one node**: a registry, its web UI and a
[Traefik](https://traefik.io/) reverse proxy that terminates HTTPS. Install a
primary first, then add any number of replicas on other hosts. A single primary
on its own is a complete installation.

Each node has **one DNS name** for everything: `https://<name>/v2/` is the
registry (`docker pull`, Helm) and `https://<name>/` is the web UI. Traefik is
the only container that binds host ports (80 and 443); port 80 only redirects
to HTTPS.

Both stacks pull the published images `ghcr.io/chainfirelabs/ociproxy:1.0.2` and
`ghcr.io/chainfirelabs/ociproxy/ui:1.0.2`.

### Requirements

- Docker with Compose 2.23.1 or later on each host
- One DNS name per node, resolving to its host
- TCP ports 80 and 443 free on the host (stop any other web server or reverse
  proxy using them) and 443 open to your clients
- A TLS certificate for that name: your own, or one from Let's Encrypt. See
  [TLS certificates](#tls-certificates).

Each node lives in its own folder:

| Path | Copied from |
| --- | --- |
| `compose.yaml` | `docker/docker-compose.primary.yml` or `docker/docker-compose.replica.yml` |
| `.env` | `docker/.env.primary.example` or `docker/.env.replica.example` |
| `ociproxy.toml` | `ociproxy.toml.example` or `ociproxy.replica.toml.example` |
| `certs/tls.crt`, `certs/tls.key` | Your certificate (not used with Let's Encrypt) |
| `compose.override.yaml` | `docker/compose.letsencrypt.yml`, only for Let's Encrypt |
| `trust/` | Optional internal CA certificates trusted by the registry and UI (created empty on first start) |

Docker Compose finds `compose.yaml`, `compose.override.yaml` and `.env`
automatically, so every command runs inside that folder with no extra
arguments. Get the templates once on each host:

```sh
git clone https://github.com/chainfirelabs/ociproxy.git ~/ociproxy-src
```

### TLS certificates

Pick one option per node, in the node's folder.

**Your own certificate** (the default). Put the full chain, leaf first, in
`certs/tls.crt` and the unencrypted private key in `certs/tls.key`. Traefik
runs as root with all capabilities dropped, so the key must be owned by root:

```sh
mkdir certs
cp /path/to/fullchain.pem certs/tls.crt
cp /path/to/privkey.pem certs/tls.key
sudo chown root:root certs/tls.crt certs/tls.key && sudo chmod 600 certs/tls.key
```

Copy real files, not symlinks (`cp -L` from certbot's `live/` folder). After
replacing them later, run `docker compose restart traefik`.

**Let's Encrypt.** Traefik obtains and renews the certificate itself. The DNS
name must resolve publicly to the host and port 443 must be reachable from the
internet. Needs Compose 2.24.4 or later. Skip the `certs` folder and run:

```sh
cp ~/ociproxy-src/docker/compose.letsencrypt.yml compose.override.yaml
```

Optionally set `ACME_EMAIL` in `.env`. To try it without hitting rate limits,
first use the staging CA; see [Let's Encrypt](docs/DOCKER.md#lets-encrypt).

**Self-signed, for testing only:**

```sh
HOST=registry-primary.example.com; mkdir -p certs && openssl req -x509 -newkey rsa:2048 -nodes -days 30 -keyout certs/tls.key -out certs/tls.crt -subj "/CN=$HOST" -addext "subjectAltName=DNS:$HOST" && sudo chown root:root certs/tls.* && sudo chmod 600 certs/tls.key
```

Clients must trust whatever CA signed the certificate. For a private CA or
self-signed certificate, put the CA certificate (or the self-signed
certificate itself) at `/etc/docker/certs.d/<name>/ca.crt` on each Docker
client.

**Internal CA.** The registry and UI trust public CAs by default. If the
primary's certificate or your OIDC provider's certificate comes from an
internal CA, put that CA's PEM file in the node's `trust` folder (next to
`compose.yaml`; Compose creates it empty, owned by root, on first start, so use
`sudo` if it already exists), then recreate the containers:

```sh
mkdir -p trust && cp /path/to/internal-ca.crt trust/ && chmod 644 trust/*.crt
docker compose up -d --force-recreate registry ui
```

Every `*.crt` / `*.pem` file there is trusted, in addition to the public CAs,
by the registry (replication to the primary, upstream registries) and the UI
(OIDC provider). Each replica needs the CA that signed the primary's
certificate; every node whose UI uses OIDC needs the CA that signed the
provider's. More in
[HTTPS and networking](docs/DOCKER.md#https-and-networking).

### Primary

1. Create the node folder:

   ```sh
   mkdir ~/ociproxy-primary && cd ~/ociproxy-primary
   cp ~/ociproxy-src/docker/docker-compose.primary.yml compose.yaml
   cp ~/ociproxy-src/docker/.env.primary.example .env
   cp ~/ociproxy-src/ociproxy.toml.example ociproxy.toml
   chmod 600 .env
   ```

2. Edit `.env`:
   - Set `OCIPROXY_HOST` to this node's DNS name, such as
     `registry-primary.example.com` (no `https://`).
   - Replace `OCIPROXY_SECRET` and `PUSH_PASSWORD` with values from
     `openssl rand -hex 32`. Generate each one separately.
   - Set `REPLICATION_PEERS` with one entry and one token per replica. If you
     don't have replicas yet, use `REPLICATION_PEERS='{}'`.

   `ociproxy.toml` lists the sources shared with replicas: Docker Hub, GHCR,
   Quay and Jetstack Helm. Edit it to change them.

3. Set up the certificate for `OCIPROXY_HOST`, using one of the
   [TLS certificate](#tls-certificates) options.

4. Start the node:

   ```sh
   docker compose up -d
   ```

### Replica

1. Generate a replication token with `openssl rand -hex 32`. On the **primary**,
   add it to `REPLICATION_PEERS` in `~/ociproxy-primary/.env`:

   ```sh
   REPLICATION_PEERS='{"site-a":{"token":"THE_TOKEN","publish":false}}'
   ```

   Then apply the change from the primary's folder:

   ```sh
   cd ~/ociproxy-primary && docker compose up -d
   ```

2. On the **replica host**, create the node folder:

   ```sh
   mkdir ~/ociproxy-replica && cd ~/ociproxy-replica
   cp ~/ociproxy-src/docker/docker-compose.replica.yml compose.yaml
   cp ~/ociproxy-src/docker/.env.replica.example .env
   cp ~/ociproxy-src/ociproxy.replica.toml.example ociproxy.toml
   chmod 600 .env
   ```

3. Edit `.env`:
   - Set `OCIPROXY_HOST` to this replica's own DNS name.
   - Set `PRIMARY_URL` to `https://` plus the primary's `OCIPROXY_HOST`. If the
     primary's certificate comes from an internal CA, add that CA to this
     replica's `trust` folder ([Internal CA](#tls-certificates)).
   - Set `REPLICATION_ID` to the key you used on the primary (`site-a`), and set
     `REPLICATION_TOKEN` to the same token.
   - Replace `OCIPROXY_SECRET` and `PUSH_PASSWORD` with new values. Never copy
     them from the primary.

   Shared sources come from the primary automatically. `ociproxy.toml` can
   stay empty, or list sources private to this replica.

4. Set up the certificate for this replica's `OCIPROXY_HOST`, using one of the
   [TLS certificate](#tls-certificates) options.

5. Start the node:

   ```sh
   docker compose up -d
   ```

For more replicas, repeat these steps with a new ID and token for each one.
Set `"publish": true` only for a replica that may add public sources to the shared catalog.

### Check the installation

From the node's folder, all three services (`registry`, `ui`, `traefik`)
should be `healthy`:

```sh
docker compose ps
docker compose logs --tail=100 registry
docker compose logs traefik      # certificate problems show up here
curl -fsS https://registry-primary.example.com/v2/
```

Open `https://<OCIPROXY_HOST>/` and sign in as **`admin`** (unless you changed
`PUSH_USERNAME`), using that node's `PUSH_PASSWORD`. Create day-to-day users
there. Then pull through the same name:

```sh
docker login registry-primary.example.com
docker pull registry-primary.example.com/dockerhub/library/alpine:latest
```

If HTTPS fails, check `docker compose logs traefik`: `permission denied` on
`/certs/tls.key` means the key is not owned by root.

### Upgrade

Back up the data volume, change both image tags in `.env` to the same new
version, then run `docker compose pull && docker compose up -d`. Storage,
backups and upgrades are covered in the [Docker guide](docs/DOCKER.md).

**From 1.0.1 or earlier**, the Compose files changed: Traefik is now included
and one name serves both the registry and the UI. Data volumes are kept. In
each node's folder:

1. Back up the data volume, then replace `compose.yaml` with the new template.
2. In `.env`, replace `REGISTRY_PUBLIC_URL` and `UI_PUBLIC_URL` with
   `OCIPROXY_HOST` (just the name), remove `BIND_ADDRESS`, `REGISTRY_PORT` and
   `UI_PORT`, add `TRAEFIK_IMAGE=traefik:v3.7.17`, and set both image tags to
   `1.0.2`. Compare with the new `.env` example.
3. Set up a [TLS certificate](#tls-certificates), and stop the host reverse
   proxy that used to front the node, so ports 80 and 443 are free.
4. Run `docker compose up -d`.

If the node's address changes, also update clients, replicas' `PRIMARY_URL`,
and the OIDC redirect URI (`https://<OCIPROXY_HOST>/auth/oidc/callback`).

## Install with Helm

Install **one node per Helm release**: the primary, then one release per replica.
Each node gets its own registry and UI, exposed over HTTPS through Kubernetes
Ingress. The examples use the `registry` namespace,
[ingress-nginx](https://kubernetes.github.io/ingress-nginx/) and existing TLS
Secrets. For other ingress controllers, use the equivalent settings.

### Requirements

- An ingress controller and a DNS name for each node: one name for both the
  registry and UI, or separate names (as in the examples below)
- TLS certificates for those names, as Secrets or issued by cert-manager
- A default StorageClass, or set `primary.persistence.storageClass` and
  `replica.persistence.storageClass`

### Shared values

`common.yaml` sets the images and production defaults for every release. The
chart already defaults to these images at its `appVersion`; pinning the tag
keeps upgrades of the chart and the images separate:

```yaml
image:
  repository: ghcr.io/chainfirelabs/ociproxy
  tag: "1.0.2"
ui:
  image:
    repository: ghcr.io/chainfirelabs/ociproxy/ui
    tag: "1.0.2"
config:
  # Signed-in users only, unless a source or [access] rule says otherwise.
  defaultVisibility: private
  logFormat: json
  # Start reclaiming cache at 85% disk use, down to 75%.
  gcDiskHighPercent: 85
  gcDiskLowPercent: 75
```

Registry traffic needs large uploads, long timeouts and no buffering. Every
registry ingress below uses these ingress-nginx annotations:

```yaml
nginx.ingress.kubernetes.io/proxy-body-size: "0"
nginx.ingress.kubernetes.io/proxy-read-timeout: "900"
nginx.ingress.kubernetes.io/proxy-send-timeout: "900"
nginx.ingress.kubernetes.io/proxy-request-buffering: "off"
nginx.ingress.kubernetes.io/proxy-buffering: "off"
```

### Primary

Generate one replication token per replica with `openssl rand -hex 32`.

`primary.yaml`:

```yaml
ui:
  publicUrl: https://registry-primary-ui.example.com
replication:
  peers:
    site-a:
      token: "REPLACE_WITH_SITE_A_TOKEN"
      # true only if this replica may add public sources to the shared catalog.
      publish: false
ingress:
  primary:
    enabled: true
    className: nginx
    host: registry-primary.example.com
    annotations:
      nginx.ingress.kubernetes.io/proxy-body-size: "0"
      nginx.ingress.kubernetes.io/proxy-read-timeout: "900"
      nginx.ingress.kubernetes.io/proxy-send-timeout: "900"
      nginx.ingress.kubernetes.io/proxy-request-buffering: "off"
      nginx.ingress.kubernetes.io/proxy-buffering: "off"
    tls:
      - secretName: registry-primary-tls
        hosts: [registry-primary.example.com]
  ui:
    enabled: true
    className: nginx
    host: registry-primary-ui.example.com
    tls:
      - secretName: registry-primary-ui-tls
        hosts: [registry-primary-ui.example.com]
```

```sh
helm upgrade --install primary charts/ociproxy -n registry --create-namespace \
  -f common.yaml -f primary.yaml
```

### Replica

Install each replica as its own release, in the same cluster or another one.
It connects to the primary's public HTTPS registry address.

`replica.yaml`:

```yaml
primary:
  enabled: false
replica:
  enabled: true
  replicaCount: 1
  primaryUrl: https://registry-primary.example.com
  advertisedUrl: https://registry-replica.example.com
replication:
  id: site-a
  token: "REPLACE_WITH_SITE_A_TOKEN"
config:
  # Shared sources come from the primary. List only this replica's private ones.
  upstreams: []
  helmRepos: []
ui:
  publicUrl: https://registry-replica-ui.example.com
ingress:
  replica:
    enabled: true
    className: nginx
    host: registry-replica.example.com
    annotations:
      nginx.ingress.kubernetes.io/proxy-body-size: "0"
      nginx.ingress.kubernetes.io/proxy-read-timeout: "900"
      nginx.ingress.kubernetes.io/proxy-send-timeout: "900"
      nginx.ingress.kubernetes.io/proxy-request-buffering: "off"
      nginx.ingress.kubernetes.io/proxy-buffering: "off"
    tls:
      - secretName: registry-replica-tls
        hosts: [registry-replica.example.com]
  ui:
    enabled: true
    className: nginx
    host: registry-replica-ui.example.com
    tls:
      - secretName: registry-replica-ui-tls
        hosts: [registry-replica-ui.example.com]
```

```sh
helm upgrade --install site-a charts/ociproxy -n registry \
  -f common.yaml -f replica.yaml
```

`replication.id` and `token` must match the primary's `replication.peers` entry
exactly. For more replicas, add a peer with a new token on the primary, run its
`helm upgrade` again, then install another release with its own ID, hostnames
and TLS Secrets.

### One hostname for registry and UI

Like the Compose install, a node can serve the registry and its UI on one name:
the registry Ingress takes `/v2/`, `/helm/` and `/replication/` (replicas:
`/v2/` and `/helm/`), and the UI Ingress takes everything else. Give both
Ingresses the same host and TLS Secret, and set `ui.publicUrl` to that name.
For the primary, change `primary.yaml` to:

```yaml
ui:
  publicUrl: https://registry-primary.example.com
ingress:
  primary:
    enabled: true
    className: nginx
    host: registry-primary.example.com
    annotations:   # the registry annotations above
      nginx.ingress.kubernetes.io/proxy-body-size: "0"
      nginx.ingress.kubernetes.io/proxy-read-timeout: "900"
      nginx.ingress.kubernetes.io/proxy-send-timeout: "900"
      nginx.ingress.kubernetes.io/proxy-request-buffering: "off"
      nginx.ingress.kubernetes.io/proxy-buffering: "off"
    tls:
      - secretName: registry-primary-tls
        hosts: [registry-primary.example.com]
  ui:
    enabled: true
    className: nginx
    host: registry-primary.example.com
    tls:
      - secretName: registry-primary-tls
        hosts: [registry-primary.example.com]
```

For a replica, do the same with `ingress.replica` and `ingress.ui`. Keep the
registry annotations on the registry Ingress only. OIDC redirect URIs become
`https://<name>/auth/oidc/callback`. `/internal/` and `/metrics` are still not
reachable from outside.

### Sign in

Each release generates its own bootstrap admin password and keeps it across
upgrades. The username is **`ci`** unless you set `auth.pushUsername`. Read the
password and sign in at the node's `ui.publicUrl`:

```sh
kubectl -n registry get secret site-a-ociproxy-auth \
  -o jsonpath='{.data.push-password}' | base64 --decode; printf '\n'
```

For the primary, use `primary-ociproxy-auth`. Create day-to-day users in each
node's UI.

The values files contain replication tokens, so keep them out of Git. To manage
credentials with your own Secrets or SealedSecrets, set `auth.existingSecret` on
**each** release and follow the [Secret examples](docs/SECRETS.md). Each node
needs a different Secret.

## Pull an image

Pull through any node by putting its registry hostname in front of the image:

```sh
docker login registry-replica.example.com
docker pull registry-replica.example.com/dockerhub/library/alpine:latest
```

Both install methods default to `private`, so clients sign in first. Sources
marked `visibility: public` allow anonymous pulls.

To share a Docker or Helm source, set **both** `sharing: public` and
`visibility: public` in Helm values (or `sharing = "public"` and
`visibility = "public"` in TOML). Shared sources must allow anonymous upstream
access. New sources default to private/local.

For an existing installation, also read the [OCIProxy naming upgrade notes](docs/RENAME.md).

Use **Cleanup** in each node’s UI to preview cache removal, pin images/charts,
and set retention or disk limits. The primary can request cleanup across public
replicas. See the [cleanup guide](docs/CLEANUP.md).

## Use as a Docker Hub mirror

Any node, installed with Compose or Helm, can be a Docker Hub pull-through
mirror, so a plain `docker pull nginx` goes through it. Add the node's registry
address (Compose: `https://<OCIPROXY_HOST>`; Helm: the registry Ingress host)
to each client's `/etc/docker/daemon.json` and restart Docker:

```json
{
  "registry-mirrors": ["https://registry-primary.example.com"]
}
```

Docker Hub images (`nginx`, `grafana/grafana`, …) are then pulled through and
cached by the node, with no login on the clients. Images that do not exist on
Docker Hub fail with `not found`, as they would without the mirror. The node's
Docker Hub source must be public (it is in the examples), and clients must
trust its certificate. See [Docker Hub mirror](docs/DOCKER.md#docker-hub-mirror).

## More information

- [Helm configuration](docs/HELM.md) and [Secret examples](docs/SECRETS.md)
- [Configuration, accounts and API reference](docs/REFERENCE.md)
- [Isolation and migration](docs/ISOLATION.md)
- [Security review](docs/SECURITY_REVIEW.md)
- [GitLab and GitHub image builds](docs/CI.md)

## License

Copyright (c) 2026 ChainFire Labs LLC. Original code is [MIT licensed](LICENSE).
See [third-party notices](docs/THIRD_PARTY_NOTICES.md) and
[image release requirements](docs/LICENSE_REVIEW.md).
