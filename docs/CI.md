# Image builds

GitLab CI builds both images. The public repository also includes
[GitHub Actions](../.github/workflows/images.yml): mirrored pushes, tags, pull
requests and manual runs verify license inventories and build the proxy and UI.
Enable Actions on the GitHub destination and mirror `.github/workflows/`.

GitHub builds do not publish or retain downloadable images. GitLab publication
requires `PUBLISH_IMAGES=true` and completion of the binary/source distribution
checks in [LICENSE_REVIEW.md](LICENSE_REVIEW.md).
