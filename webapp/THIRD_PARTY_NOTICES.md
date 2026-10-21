# Third-party notices

ContainerProxy's original code is Copyright (c) 2026 ChainFire Labs LLC and
licensed under MIT. Dependencies, bundled libraries and base-image components
retain their own licenses and copyright notices; they are not relicensed MIT.

## Locked Python runtime dependencies

These versions were reviewed from hash-verified Linux x86-64 Python 3.13 wheels.
Full license texts and original copyright notices are in `third_party/licenses/python/`.
The accompanying `third_party/python-inventory.json` records wheel checksums,
license-file checksums and exact upstream source URLs/checksums.
The UI inventory is restricted to the packages shipped by the UI.

| Package | Version | License expression |
| --- | --- | --- |
| anyio | 4.15.1 | MIT |
| certifi | 2026.10.19 | MPL-2.0 |
| click | 8.5.0 | BSD-3-Clause |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| starlette | 1.7.0 | BSD-3-Clause |
| typing_extensions | 4.16.0 | PSF-2.0 |
| uvicorn | 0.53.0 | BSD-3-Clause |

## certifi: MPL-2.0 source availability

The unmodified certifi package and its Mozilla CA bundle remain available under
MPL-2.0. The full license is in `third_party/licenses/python/certifi-2026.10.19/LICENSE`.
Source for the exact distributed version is available at:

https://files.pythonhosted.org/packages/a6/bd/1436c50cc46699de0230fbaacc1d1de7bfec413176099949e9d5112e88f2/certifi-2026.10.19.tar.gz

SHA-256: `035c1e85572b141bf1c3a3eff0e33a8eb74917522f2559075ff8d2fea780a1ae`.

The installed `.py` files and CA data also remain readable in the container at
`/app/site-packages/certifi/`; this project does not modify them. If you modify
MPL-covered files, preserve their MPL license and make those modified files
available to recipients. The MIT license does not restrict these rights.

## Container base image

Both images inherit the pinned Google Distroless Python 3 / Debian 13 image.
Python, Debian libraries and Distroless have separate license terms, including
GPL/LGPL-covered components. Preserve `/usr/share/doc/*/copyright`,
`/usr/share/common-licenses/`, Python's license and installed dependency
`.dist-info` directories. Do not describe the entire image as MIT-only.

Project license and notices are in `/usr/share/licenses/containerproxy/`.
Dependency wheel notices also remain in `/app/site-packages/*.dist-info/`.
See the repository's `LICENSE_REVIEW.md` for exact base-image review scope and
source-distribution requirements before publishing container binaries.
