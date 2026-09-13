# cert-plan reducer & adapter fixtures

Compact, self-contained JSON for `utillities/pep_cert_plan.py`
(`test_pep_cert_plan.py`) and the evidence adapter `utillities/pep_cert_adapter.py`
(`test_pep_cert_adapter.py`). The `spike0_*` and `rag2_members` files are complete
`reduce()` inputs; `rag_detector_matrix.json` is an adapter input (a captured
detector matrix). All are **derived from** preserved evidence but carry no runtime
dependency on it — the raw RPMs/DEBs/ZIPs and raw GitHub API dumps are **not**
committed; only the structured fields consumed downstream.

## Files & SHA-256

| fixture | sha256 |
|---|---|
| `spike0_attempt2.json` | `07c96206ec62529d3439e965e1b4b222d1d74d792945d14d645d594db34b3959` |
| `spike0_attempt3.json` | `ceeeddfedc2ae7e11b92900a53c42e7daec4c218a0e7e54d326e69db2381b10f` |
| `rag2_members.json`    | `42a4e9d3e8b281d0b9bd78a51a9e596fa0119eef273cbaeed895ece2c6337687` |
| `rag_detector_matrix.json` | `30511ee3ed51f7940610b85d335325cc7b08ed9f76e710d48182d6aa5ed79508` |

## Provenance

- **spike0_attempt2 / spike0_attempt3** — normalized jobs-by-attempt + artifact
  inventory from **Spike 0 run `34495033138`** (repo `pgEdge/pgedge-rag-server`,
  branch `spike/pep-artifact-rerun`, workflow SHA `6100e51`). Cells `fj-A`, `fj-B`,
  `full-A`, `full-B`; the `[pep-cell:<id>]` marker was resolved to `cell_id` when
  deriving the fixture (job→cell mapping is a live-adapter concern, not the
  reducer's). **attempt-2 is the carried-forward-job case** — "Re-run failed jobs"
  genuinely re-ran only fj-B; fj-A/full-A/full-B carried their attempt-1 result and
  artifact forward (all four resolve to `available`). **attempt-3 is "Re-run all
  jobs"** — every leg genuinely re-executed; full-A failed before upload (its
  artifact deleted), the other three succeeded with their stable artifacts present.
- **rag2_members** — real member metadata for **run `33759277144`** (RAG2 v2.0.0,
  `pgedge-rag-server2`), one RPM cell (`rpm:el-9:amd64`, source + binary members)
  and one DEB cell (`deb:bookworm:amd64`, binary member). Job records are minimal
  synthesized `available` records; member `sha256` values are the real per-file
  checksums.
- **rag_detector_matrix** — the real RPM+DEB detector matrix for `pgedge-rag-server`
  (component `pkg`, PG-decoupled: 4 RPM + 12 DEB cells), produced by the
  `pgEdge/pgedge-detect-build-matrix` composite action at merged-`main` commit
  `3fb36518ee7ed2f626a7a8cffd7d68d11fdf25c4`. It is a snapshot for tests, not an
  authoritative contract. Reproduce with the detector's own harness against a
  `pgedge-rag-server` checkout:

  ```
  bash test/run_detector.sh <detector_dir> <rag-server_checkout> pkg "" \
    '["almalinux:9","almalinux:10"]' \
    '["ubuntu:jammy","ubuntu:noble","ubuntu:resolute","debian:bullseye","debian:bookworm","debian:trixie"]' \
    '["amd64","arm64"]' | jq -S '{rpm_matrix, deb_matrix}'
  ```

## Source checksums (what the fixtures were derived from)

| source | sha256 |
|---|---|
| Spike0 attempt2 `artifacts.json` | `e6fb341ccc2175a87037fcc4a29071bbb262992eb37eff980661d94c4eee9ab0` |
| Spike0 attempt3 `artifacts.json` | `19135225756783c8c58a3d909c6498b038e97ac4341ad6862af6077956db88aa` |
| RAG2 `SHA256SUMS`                 | `5aaf526cb3ad9da4d02aad63aca675831a0ca6e61fcc1788c8ed9a22397ed18e` |

Original evidence lives outside the repo (not committed):
`~/Downloads/pep-spike0/evidence-run-34495033138/` and
`~/Downloads/pep-fixtures/rag2-v2.0.0-run-33759277144/`.
