# Image builds

GitLab CI builds both images. The public repository also includes
[GitHub Actions](../.github/workflows/images.yml): mirrored pushes, tags, pull
requests and manual runs verify license inventories and build the proxy and UI.
Enable Actions on the GitHub destination and mirror `.github/workflows/`.

Git tag pushes publish both images to GHCR with the exact tag name:

- `ghcr.io/chainfirelabs/ociproxy:1.0.0`
- `ghcr.io/chainfirelabs/ociproxy/ui:1.0.0`

Future tags such as `1.0.1` publish the corresponding image tags automatically.
Git tags must also be valid container image tags. Branch pushes, pull requests
and manual runs without a release tag build only.

To publish an existing tag created before this workflow supported publishing,
run **Build container images** from the Actions tab on the updated default
branch and enter that tag in **release_tag** (for example, `1.0.0`). Both jobs
check out the specified tag, so published images contain that release's code.

Publishing uses the workflow's `GITHUB_TOKEN` with `packages: write`; no separate
registry secret is required. Organization policy must allow package creation,
and existing packages must grant this repository Actions access. Set GHCR
package visibility to public if anonymous pulls are required.

GitLab publication requires `PUBLISH_IMAGES=true`. Binary/source distribution
checks are documented in [LICENSE_REVIEW.md](LICENSE_REVIEW.md).
