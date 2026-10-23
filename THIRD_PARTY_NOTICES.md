# Third-party notices

Bloom's original code is licensed under the [MIT License](LICENSE).

## Mozilla Readability

The unmodified `src/bloom/web_assets/Readability.js` is vendored from
[mozilla/readability](https://github.com/mozilla/readability), revision
`ab4027a8b37669745016869a37a504727992b2ba`, under Apache-2.0. Its copyright
notices remain in the source. The complete license is retained at
[Readability-LICENSE.md](src/bloom/web_assets/Readability-LICENSE.md).
Pinned file URLs, sizes and hashes are in
[readability-provenance.json](src/bloom/web_assets/readability-provenance.json).

The package license expression includes Apache-2.0 because these files ship
with the distribution. It does not relicense Readability under MIT.

## Downloaded tools and Python dependencies

The optional runtime tool installer downloads a checksum-pinned
[ripgrep](https://github.com/BurntSushi/ripgrep) release. No executable is
included in this repository. Installation preserves its COPYING, LICENSE-MIT
and UNLICENSE files beside the binary in the local workspace.

Python dependencies are installed separately from `uv.lock` and retain their
respective licenses. No credentials, model weights or provider software are
included.
