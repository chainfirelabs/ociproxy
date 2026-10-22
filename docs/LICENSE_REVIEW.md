# License review — 2026-10-20

## Conclusion and scope

OCIProxy's original Python code, inline UI code, documentation, build
scripts and chart can be offered under MIT, with ChainFire Labs LLC as the
copyright holder, assuming the company owns or has permission to license those
contributions. No reviewed Python runtime dependency requires the original
application source to be relicensed. This is an engineering license inventory,
not a legal opinion or a certification of the origin of every source line.

The source repository and Python distributions do not vendor dependency code.
The container images do redistribute dependencies and OS libraries. Publishing
these different artifacts has different obligations; an MIT project license
is not a license for everything inside its container images.

## Changes made

- Changed image CI to build without publishing by default. Enable
  `PUBLISH_IMAGES=true` only after the source-delivery requirements below are
  met; the first push of this source repository will not publish images.
- Added the complete MIT license, copyright 2026 ChainFire Labs LLC, at the root,
  in the independently built UI package, and in the distributable Helm chart.
- Declared SPDX `MIT` and explicit `license-files` in both Python projects;
  raised setuptools to a version supporting this metadata.
- Preserved exact Python-wheel license texts and copyright notices, with wheel,
  source and notice checksums in `third_party/python-inventory.json`. The UI
  has its own smaller inventory and license directory.
- Added an MPL source-availability notice for the unmodified certifi package,
  including the exact source archive and checksum.
- Preserved the licenses of all 32 registry crates in cryptography's source
  Cargo.lock, including build dependencies. Archive checksums were verified.
  Where a crate offers Apache/MIT as an alternative to GPL, the permissive
  alternative is available. The full crate list is not a claim that every
  crate is linked into the wheel.
- Added supplemental OpenSSL 4.0.3, LibYAML 0.2.5 and libffi license texts. The
  reviewed installed cryptography wheel reports OpenSSL 4.0.3; PyYAML reports
  LibYAML 0.2.5. CFFI's wheel does not depend dynamically on libffi; its bundled
  libffi version and complete platform-specific notices need upstream artifact
  verification before a binary release. The supplemental libffi text is from
  upstream main and does not establish that version.
- Extracted legal notices and package versions from the exact pinned amd64
  Distroless base. Both Dockerfiles retain all inherited base files and installed
  dependency metadata, and add notices at `/usr/share/licenses/ociproxy/`.

## Python runtime license summary

The exact 14-package proxy inventory and 10-package UI inventory are in
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) and their JSON inventories.
The set includes MIT, MIT-0, BSD-3-Clause, PSF-2.0, MPL-2.0 and cryptography's
Apache-2.0 OR BSD-3-Clause option. Preserve their original copyright, license,
exception and NOTICE files when redistributing. Choosing MIT for this project
does not replace those terms. No dependency replacements were required.

certifi is unmodified and ships readable Python source and the CA data in the
image. Its exact source archive is also linked in the notices. If MPL-covered
files are changed later, make those changed files available under MPL.
Mozilla explains the file-level scope and larger-work rules in its
[MPL FAQ](https://www.mozilla.org/en-US/MPL/2.0/FAQ/).

## Container binary release requirements — outstanding

The reviewed base is recorded in `third_party/base-image-packages.json`, with
38 Debian package versions and their source-package names/versions. Review was
for Linux amd64 only; the Dockerfiles reference a multi-platform image index.

For example, the exact base's Readline notice says GPL-3+, glibc says LGPL-2.1+,
and Python carries PSF and incorporated-software notices. Distroless's build
repository license does not replace the licenses of its Debian contents.

Before distributing images publicly:

1. For every architecture you will publish, inventory the final image, retaining
   original Debian notices, Python notices, `.dist-info` license files and native
   code notices. Check the actual compiled wheel contents and Rust/libffi/native
   dependencies for that architecture, rather than treating top-level package
   metadata as a complete binary inventory.
2. Obtain and retain complete corresponding sources for GPL/LGPL components,
   including the exact Debian `.dsc`, original source archives, Debian patches,
   and build/control material for the recorded source versions. Use Debian
   archive/security repositories or snapshots if a pinned version has moved.
   Preserve installation/relinking rights where applicable. Do not silently
   substitute sources from the latest release.
3. Provide the matching sources alongside the image release, with clear source
   download instructions for recipients and appropriate retention. A generic
   Debian homepage or a link to this MIT application's source does not by itself
   satisfy all binary redistribution obligations. This review creates no written
   source offer on behalf of the company.
4. Confirm that notices and source access survive the publishing process. No
   sources have been published or source-delivery service established in this
   review. Image publication is not yet marked license-cleared.

Debian's [distribution guidance](https://www.debian.org/CD/vendors/legal)
explains source responsibilities for redistributed binaries. The
[GNU license FAQ](https://www.gnu.org/licenses/gpl-faq.en.html) covers aggregation,
linking, and corresponding-source requirements. Keep the application MIT while
meeting the separate terms for each redistributed component.

The builder image, pip, setuptools, pytest, GitLab runner images, Helm CLI and
Docker tooling are build/test tools, not copied wholesale into the runtime.
Their use does not automatically relicense the application. Reassess if you
start distributing those tools, their vendored code, or build-stage images.

## Assets and provenance

The project owner confirmed that `logo-banner.png` and
`webapp/src/ociproxy_ui/logo.png` depict the ChainFire Labs LLC company
logo, which they personally designed. These are company assets, rather than
unidentified third-party artwork. The outstanding logo ownership and
redistribution provenance item is resolved based on that confirmation.

The UI uses inline code and system-font fallbacks; no bundled third-party
JavaScript framework or font files were identified. Naming a system font in
CSS does not distribute that font. The devpi-style description alone is not
evidence of copied devpi code. The review did not independently establish the
authorship history of every source line; retain any notices for code copied
from elsewhere and verify contributor rights.

Images/charts cached or mirrored by the running service keep their own licenses.
The project's MIT license grants no additional redistribution rights over that
content; review upstream terms if you publish a populated cache.

## Maintenance and validation

Run `python tools/check_licenses.py` after dependency updates. It verifies both
lockfile package sets/versions, copied license checksums and MIT license copies.
To refresh notices, download the exact locked wheels with hash checking, copy
their declared license/NOTICE files, record source archives and checksums, and
review native contents. Recheck every platform you ship, including newly added
native or optional dependencies. Keep `webapp/third_party` in sync with its lock.

Both Python sdists and wheels were built and inspected for `License-Expression:
MIT`, `License-File: LICENSE`, and the complete LLC copyright/license text.
Both amd64 container images built successfully. Runtime inspection verified
the LLC license, SPDX metadata, third-party inventory, installed Python license
files, readable certifi CA data, and inherited GPL/LGPL notices. The
original application's behavior is unchanged by the licensing changes.

The Helm chart was packaged and its LLC MIT license inspected. Both Python
source archives include the third-party inventory and notice files. The license
inventory check passed for 14 proxy dependencies, 10 UI dependencies and all
32 reviewed Rust crate notice sets. The dependency vulnerability audit remained
clear. No full application test rerun was needed for these packaging/documentation
changes; both built images were inspected directly. No artifacts were pushed.
