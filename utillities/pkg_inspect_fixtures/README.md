# pep_pkg_inspect fixtures

Text/JSON golden fixtures for `utillities/pep_pkg_inspect.py`
(`test_pep_pkg_inspect.py`). **No binary packages are committed** — the tests use
mocked tool output and fake executables, and real package inspection is a manual
local acceptance check (below).

## Files

- `golden.json` — one entry per case with the **raw external-tool output**
  (`rpm -qp --queryformat …` line, or `dpkg-deb --field …` block) and the
  **expected `pep-members/1` member**. Tests inject `sha256` (64×`a`) and the
  case's `artifact_member_path`, run the pure parser
  (`parse_rpm_output`/`member_from_rpm`, `parse_deb_output`/`member_from_deb`),
  and compare. Cases cover: runtime/source/debug RPM, source RPM with **both**
  `x86_64` and `aarch64` in the ARCH header (class comes from `SOURCEPACKAGE=1`,
  never ARCH), RPM epoch, DEB runtime/dbgsym, DEB epoch, a native DEB with **no**
  revision, and a **hyphenated upstream** DEB version.

## Provenance

Field shapes mirror **rpm 4.18.2** (`%{NAME}\t%{EPOCHNUM}\t%{VERSION}\t%{RELEASE}\t%{ARCH}\t%{SOURCEPACKAGE}\t%{SOURCERPM}`)
and **dpkg-deb 1.22.6** (`--field … Package Version Architecture`) output, as
captured on 2026-09-14 from the preserved RAG2 v2.0.0 packages inside an
`ubuntu:24.04` container. Empirically confirmed there and encoded in these
fixtures:

- An **aarch64** host queried **x86_64** RPM and **amd64** DEB headers with exit 0
  — package-header inspection needs no matching host architecture.
- `%{SOURCEPACKAGE}` is `1` for a source RPM and `(none)` for a binary RPM;
  `%{EPOCHNUM}` is `0` when there is no epoch.
- A `.src.rpm` reports its **build** arch in `%{ARCH}` (observed `x86_64` for the
  el9 source and `aarch64` for the el10 source) — so source classification uses
  `%{SOURCEPACKAGE}`, never ARCH or the filename.
- The inspector accepts only internally consistent marker pairs: a source RPM
  (`%{SOURCEPACKAGE}==1`) must report `%{SOURCERPM}==(none)`, and a binary RPM
  (`%{SOURCEPACKAGE}==(none)`) must name a nonblank, non-`(none)` `%{SOURCERPM}`.
  `%{SOURCERPM}` is queried only to validate this consistency; it is never added
  to the member schema and never independently classifies a package.

## Manual real-package acceptance (not in CI, not committed)

The real RAG2 v2.0.0 packages live **outside** the repo at
`~/Downloads/pep-fixtures/rag2-v2.0.0-run-33759277144/artifacts/` (source
run `33759277144`, `pgedge-rag-server2`). Run the inspector manually against them
(RPM + DEB, cross-arch, and the runtime + source RPM pair) to confirm real-tool
behavior matches these golden shapes. Do not add those binaries to Git.
