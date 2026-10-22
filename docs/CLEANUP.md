# Cleaning cached images and charts

Sign in as a local administrator and open **Cleanup**.

- **This node:** select cached entries, preview, then confirm. Docker/OCI indexes
  include cached platform manifests. Helm indexes and chart versions appear too.
- **Primary + all public replicas:** available on the primary. Only explicitly
  shared public sources qualify. The primary records a durable request; replicas
  apply it when connected and report completed, completed with skips, or pending.
  Pending peers with no heartbeat in 90 seconds are marked offline.
  Upgrade every backend/UI to OCIProxy 0.2 or later before using this scope.
- **Pins:** protect selected entries, aliases of their digest, and cached platform
  children. Pins are node-local, persist across restarts, and override local and
  coordinated cleanup. A retained image index also protects its platform entries.
- **Local pushes:** excluded by default and always protected from automatic
  cleanup. Explicitly include them for manual local deletion; confirmation warns
  that this may delete their only copy. They are never targets of public cleanup.

Preview estimates account for shared layers on this node; replicas may have
other references, pins and grace periods. Previews expire after five minutes and
are single-use. Execution rechecks pins, digests and index dependencies. Tags that
moved to different digests are skipped. Removing a manifest does not immediately
remove its layers: only unreferenced blobs older than the grace period are freed.

Cleanup is eviction, not blocking: an upstream pull can download content again.
It does not remove configured sources, users, passwords, tokens or private data
on other nodes. Each public cleanup request targets the selected primary-cached
entries; it is not an inventory or purge of arbitrary replica-only data. Public
requests/results are retained durably so offline peers do not miss them. Existing
requests target the peer IDs configured when the request was created; a peer that
is permanently removed can therefore remain pending in historical results.

## Routine disk management

Save a local policy or set deployment defaults:

| Setting | Effect |
| --- | --- |
| Unused days | Expire idle mirrored entries; `0` disables age-based expiration. |
| Disk high / low % | At the high watermark, evict least recently used mirrored content toward the low watermark. High `0` disables. |
| Maximum cache GiB | Limit cached blob bytes, including shared/pushed bytes; `0` disables. |
| Pins | Always protected; local pushes are also protected automatically. |

Policies act on this node only, including its private mirrored cache. When a
filesystem/cache limit is exceeded, eviction considers oldest mirrored groups
first. Other files on the same filesystem, retained layers, pins, local pushes
and the blob grace period can prevent reaching the requested limit. The UI reports
remaining pressure rather than deleting protected content. These are maintenance
targets, not hard admission quotas; downloads may exceed them between runs.

**Run cleanup now** applies the local policy and collects eligible orphan blobs.
The same work runs on the maintenance interval (`0` disables background runs).
UI-saved policies persist in the node database and override deployment defaults.

Helm defaults, configured independently per release:

```yaml
config:
  gcInterval: 3600
  gcManifestMaxIdleDays: 30
  gcBlobGrace: 86400
  gcDiskHighPercent: 85
  gcDiskLowPercent: 75
  gcMaxCacheBytes: 53687091200 # 50 GiB; 0 disables
```

Compose `.env` equivalents are `GC_INTERVAL`, `GC_MANIFEST_MAX_IDLE_DAYS`,
`GC_BLOB_GRACE`, `GC_DISK_HIGH_PERCENT`, `GC_DISK_LOW_PERCENT`, and
`GC_MAX_CACHE_BYTES`. Standalone environments use the `OCIPROXY_` prefix.
TOML uses `[gc]` keys `interval`, `manifest_max_idle_days`, `blob_grace`,
`disk_high_percent`, `disk_low_percent`, and `max_cache_bytes`.

## Administration API

`GET /internal/cleanup` shows entries (use `?offset=N` for pages of 2000), pins,
policy, disk usage, recent local jobs, and primary-side replica results.
The POST actions are `/internal/cleanup/preview`, `/execute`, `/pin`, `/policy`,
and `/run`. Every endpoint requires this node's local secret and a valid local
acting administrator. UI routes also require a local admin session and CSRF header.

Preview accepts `scope` (`local` or `public`), `targets` (arrays of upstream,
repository, reference) and optional `include_pushed`. Execute accepts only the
returned `preview_id`. Pin accepts `target` and boolean `pinned`. Policy accepts
all four fields: `manifest_max_idle_days`, `disk_high_percent`,
`disk_low_percent`, `max_cache_bytes`. Run accepts `{}`.

Peer credentials can only acknowledge their own assigned public requests via
`POST /replication/v2/cleanup/<id>/ack`. They cannot preview, execute, pin or
change policy. Public cleanup events are retained through changelog compaction;
replayed events return the saved result instead of deleting newly refetched data.
