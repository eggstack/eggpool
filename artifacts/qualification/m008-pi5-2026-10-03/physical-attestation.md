# M008 Physical Target Attestation

Evidence source: the eight machine-readable reports in this directory. They were captured on 2026-10-03 UTC with one feature-built release binary; report integrity checks passed after graceful shutdown. Local target evidence only; no hosted CI result is implied.

- Target: Raspberry Pi 5 Model B Rev 1.0, four Cortex-A76 CPUs, aarch64.
- OS/kernel: Ubuntu 24.04.4 LTS, Linux 6.8.0-1065-raspi.
- Memory: 8,322,752,512 bytes. CPU governor: `ondemand`; observed operating frequencies varied under load.
- Root storage: `/dev/mmcblk0p2`, ext4, MMC, non-rotational. The reports retain device class but do not expose device serials.
- Toolchain: rustc 1.98.1, Cargo 1.98.1.
- SQLite lineage: bundled SQLite 3.53.2 (`libsqlite3-sys` 0.38.2, `rusqlite` 0.40.2, `tokio-rusqlite` 0.8.0).
- Candidate binary SHA-256: see `binary.sha256`.
- The benchmark reports include aggregate latency/storage facts, effective pragmas, lifecycle outcomes, and read-only `quick_check` / foreign-key checks. Absolute diagnostic database paths are redacted in retained JSON.
