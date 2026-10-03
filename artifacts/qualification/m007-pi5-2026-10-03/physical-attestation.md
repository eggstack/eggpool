# M007 Physical Qualification Attestation — Raspberry Pi 5

Date: 2026-10-03

- Device-tree model: Raspberry Pi 5 Model B Rev 1.0.
- OS/kernel: Ubuntu 24.04.4 LTS; Linux `6.8.0-1064-raspi`; `aarch64`.
- CPU/RAM: four reported CPU cores; 7.75 GiB available system memory.
- Project, `/var/lib`, and `/` resolve to `/dev/mmcblk0p2`, ext4 on `/dev/mmcblk0` (MMC, 477.5 GiB). The separate 931.5 GiB NVMe was not mounted or used by this corpus.
- Runner attestation in each JSON: `Linux aarch64 plus device-tree board model`; `root_storage_device_class` is `mmc`.
- Qualification binary source commit: `6f0cfd532e753a43c454b370453850c55679aab5`.
- Feature-enabled release binary SHA-256: `a22e0d4d3962206651fd6f4b7dcc4e6528370a0816dba0ce153a4123fd610d64`.
- Toolchain: `rustc 1.98.1 (48a229cea 2026-09-01)`.

The runner JSON records board, filesystem, storage class, CPU frequency/governor, temperature, RAM, request timings, phase summaries, WAL/checkpointer counters, and lifecycle observations. Inputs use only the runner's synthetic loopback provider and aggregate-only outputs.
