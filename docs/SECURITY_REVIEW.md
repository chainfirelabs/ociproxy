# Security review — 2026-10-20

Reviewed the Python registry, replication, account APIs, admin UI, OIDC flow,
container definitions, deployment examples, dependency locks and reachable Git
history. This is a source review and automated scan, not a penetration test or
an assurance that all vulnerabilities have been found.

## Changes

- Updated certifi to 2026.10.19 in both hash-verified dependency locks and
  raised the package security floors for certifi and Starlette. The prior
  CA bundle was affected by GHSA-6q48-6c7w-9gw2:
  https://github.com/advisories/GHSA-6q48-6c7w-9gw2
  Rebuild deployed images to pick up the corrected bundle.
- Restricted registry pagination links to the configured origin, including
  scheme and port, and rejected embedded URL credentials. Previously an
  upstream-controlled Link could trigger requests to other services and
  authentication challenges could cause credentials to be sent there.
- Included the token realm in the bearer-token cache key and rejected HTTP
  token endpoints advertised by HTTPS registries.
- Disabled internal HTTP redirects: the custom shared-secret header must not
  follow a redirect to another service.
- Checked discovered OIDC endpoints for HTTPS, allowing the existing explicit
  HTTP override and loopback development use.
- In mixed public/private deployments, anonymous blob pulls require a cached
  manifest in the requested repository referencing the digest. This closes a
  private-layer disclosure through an arbitrary public path in the global
  content-addressed cache. GET and HEAD are covered. Pull the manifest before
  its layers; authenticated readers retain access to private content.
- Removed deployment-specific image naming from the build helper and expanded
  ignore rules for local environments and private-key files. Build contexts
  exclude local configuration, virtual environments and private keys too.

## Secrets and public export

Gitleaks scanned reachable commit history with redacted output. Its single
finding is the deliberately invalid token-format fixture in tests/test_auth.py,
not a working credential. A separate working-directory scan also detected the
ignored local .env secret, which is excluded from the public export. Manual checks found placeholders and test credentials,
not confirmed live credentials. Local .env and containerproxy.toml have never
been tracked. No scanner can prove the absence of every possible secret.

The public export includes tracked project files and the reviewed additions,
with a new empty Git repository. It excludes the original .git directory,
remote configuration, history, ignored local configuration, caches and virtual
environment. Historical deployment files are deliberately excluded. The
original checkout retains its history and remote configuration.

## Deployment boundaries and remaining risks

- The shared node secret grants cluster-wide authority, including account
  provisioning and impersonation. Every replica and the UI are trusted.
  Restrict /internal and /metrics to the cluster network; use TLS across hosts.
- Public visibility intentionally allows anonymous pulls. Review visibility
  rules before connecting a private upstream with service credentials.
- Registry bearer challenges may legitimately use a separate authentication
  host. Upstream operators are trusted to choose that host; use egress policy
  to limit upstream, Helm archive and token requests to approved services.
  Source management is admin-only but is not an SSRF sandbox for admins.
- Apply login rate limits, request/body limits, storage quotas and connection
  limits at the ingress. Password hashing is concurrency-limited, but this is
  not distributed brute-force protection. Manifest bodies are buffered.
- OIDC account names and group claims are trusted provider policy. Ensure the
  username claim is stable and cannot be chosen to impersonate another user.
- Container base-image digests remain pinned. Container OS packages were not
  vulnerability-scanned or rebuilt as part of this source review.

Bandit's medium SQL warnings use constant column lists and parameterized
values; inspection did not identify SQL injection there. Binding the UI to
all interfaces is expected inside its container.

## Validation

- Full Python suite: 206 passed.
- Security and UI subset: 46 passed; exported access-control and upstream
  security tests: 17 passed with the updated dependency environment.
- pip-audit against both final lockfiles: no known vulnerabilities found.
- Gitleaks on the public export: only the reviewed fake token fixture above.
- Byte scan of all exported files, including fresh Git metadata: no matches
  for the removed deployment domain.
- Shell syntax and Git whitespace checks passed.

No container build, OS-image vulnerability scan, live deployment test or
external penetration test was performed.
