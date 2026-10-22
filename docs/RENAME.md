# Upgrading to OCIProxy naming

This release renames the application, chart, packages and configuration interface.
Upgrade all primary/replica backends and UIs together; mixed header names do not
authenticate. This is a naming upgrade, not an automatic migration of live storage.

1. Back up each node's storage, configuration and Secrets. Stop writers before
   copying a volume. Keep each node's accounts and private sources separate.
2. Update environment variables to `OCIPROXY_*`. Use `ociproxy.toml` and
   `ociproxy.replica.toml.example`, and run the `ociproxy` command. Python imports
   are `ociproxy` and `ociproxy_ui`; distributions are `ociproxy` and `ociproxy-ui`.
   Reinstall Python packages and clear generated `build/` and `webapp/build/`
   directories before building wheels from an existing checkout.
3. Build/publish the new `ociproxy` and `ociproxy/ui` images. Update image references,
   Secret references, ingress service targets and monitoring configuration.
   The default in-container storage mount is `/var/lib/ociproxy`. The storage
   layout, `index.db`, password hashes and existing personal tokens are unchanged.
   Preserve each node's own encryption secret: existing encrypted upstream
   credentials remain readable with it.
4. Compose now defaults to project name `ociproxy`. Preserve your existing
   project name with `docker compose -p NAME` to reuse its node-specific volumes;
   changing the project name otherwise creates a separate empty deployment.
   Update your private `.env` and mounted config filenames before starting.
5. For Helm, use `charts/ociproxy` (chart 0.4 and later). Chart names and labels change
   resource names and immutable StatefulSet selectors. Do not assume a normal
   in-place upgrade will preserve or reattach old claims. Plan a stopped-node
   restore into the new release's storage, retain the old claims until verified,
   and never attach the same live database to two writers.
6. Each UI uses new session cookies, so sign in again. Update monitoring to the
   `ociproxy_*` metric names. Custom clients must use `X-OCIProxy-Secret`,
   `X-OCIProxy-Acting-User` and `X-OCIProxy-Peer` headers as appropriate.

Hosted Git repository names and remotes are managed separately. Documentation
uses `github.com/chainfirelabs/ociproxy` and the matching GHCR paths; rename the
hosted repository and adjust mirrors/remotes when ready. No running deployment,
production credential or hosted repository is changed by this code update.

If your storage predates node isolation, follow the separate
[offline isolation migration](ISOLATION.md#existing-deployments-explicit-offline-migration) first.
