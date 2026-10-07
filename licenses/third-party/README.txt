Python wheels carry shared libraries selected by maturin from the build host.
The manifest maps their library names to the accompanying copyright and license
texts, and records the package versions and paths from which they were copied.
It covers the Debian release image and the Arch Linux development build.

The Debian copyright files retain their original references to
/usr/share/common-licenses; the referenced full texts are included here as .txt
files, with GPL and LGPL referring to GPL-3.txt and LGPL-3.txt respectively.
These package notices may also describe programs and source files that are not
part of a bundled shared library.

When changing the build image or native dependencies, refresh the notices from
those package versions and update manifest.json.
tools/build_manifest.py rejects release wheels with an unmapped bundled library
or a missing or empty notice.
The notice check verifies packaging; it does not determine license obligations.
