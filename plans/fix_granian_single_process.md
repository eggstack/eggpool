# Plan to ensure EggPool runs as a single Granian process on small devices

The current EggPool deployment occasionally exhibits multiple `eggpool` processes in system monitors, each consuming tens of megabytes of memory.  Under Raspberry Pi or other single-board-computer (SBC) deployments, this multiplicity wastes limited RAM and CPU resources and makes it difficult to bound resource usage.  The intended design is for EggPool to run a single process using Granian’s asynchronous event loop to handle all concurrent sessions; one process should be able to handle dozens of WebSocket and HTTP connections without spawning additional workers.

## 1 Verify and characterise the existing behaviour

Before attempting changes it is essential to reproduce the issue and document precisely what “multiple processes” means on the target machine.  Tools like `ps`, `pstree`, and `ps -eLf` will distinguish between true processes and kernel threads.  On systems where `htop` shows 11 entries for EggPool, one should inspect the `PPID`, `NLWP` (number of threads), and memory fields:

* **Process vs thread count.** Use `ps -eLf | grep '[e]ggpool'` to see each light‑weight thread; many monitors display each thread as if it were a separate process when “show userland threads” is enabled.  If there is only one PID with a high `NLWP` value then the issue is an observability artefact; Granian internally spawns a pool of worker threads to handle network I/O and offload blocking operations.  These threads share the same address space and do not multiply memory consumption.

* **Multiple distinct PIDs.** If `ps` reveals several distinct PIDs with the same command line and different parent process IDs, then EggPool has spawned multiple worker processes.  In this case the problem is real and must be addressed.

Recording this baseline on a Raspberry Pi (e.g., `ps -eLf` and `pstree -p`) will inform whether configuration changes produce the desired single‑process footprint.

## 2 Understand Granian’s worker model

Granian is a Rust‑based ASGI/WSGI server that can manage multiple operating‑system processes and threads.  In our repository the server is started via `Granian(..., workers=1, interface="asgi")`, which means one worker process plus a supervising parent.  Granian will still create a pool of threads to service requests even with `workers=1`; this is why the `NLWP` count may be higher than one.  There are two relevant configuration parameters:

* **`workers`** controls the number of worker processes.  Setting `workers=1` instructs Granian to start exactly one worker process in addition to its parent supervisor.  If a PID file is written from within the worker, management scripts may only kill the worker, leaving the supervisor alive and causing a second invocation to create an extra set of processes.

* **`threads`** controls the number of threads per worker.  By default this may be set to the number of logical CPUs.  On an SBC with limited memory and I/O concurrency this value can be decreased to reduce thread count.

Granian currently lacks a documented mode where it does not fork a child worker at all; thus one cannot truly obtain a single OS process using Granian.  However, by ensuring only one worker is ever active and by managing the PID file correctly, the overall process count can be kept at two (supervisor and worker) with a modest thread pool.

## 3 Audit existing management scripts and PID handling

EggPool writes its PID file inside the FastAPI lifespan context, i.e. within the worker process.  The CLI `stop` and `restart` functions read this PID file and send signals to that PID.  If Granian spawns a supervisor plus worker, the PID file will reflect the worker.  Killing the worker causes the supervisor to respawn a new worker, and if `eggpool serve` is subsequently invoked again, multiple workers accumulate.  The systemd unit installed by `eggpool deploy systemd` is of type `simple`, which means systemd considers the main process to be the supervisor; stopping or restarting the service via systemd will properly shut down both supervisor and worker.  However, the CLI’s own lifecycle management is misaligned with this model.

To correct this misalignment, the PID file should be written by the top‑level process rather than the ASGI app.  When launching EggPool from the CLI outside of systemd, the process that calls `Granian(...).serve()` knows its own PID and can write it to the PID file before invoking the server.  Alternatively, the server could write the parent process ID to the file instead of its own.  This ensures that `eggpool stop` will kill the parent process, which in turn terminates its worker, preventing orphaned supervisors.

## 4 Proposed changes to the codebase

1. **Write the PID file from the parent process.** Modify the `runtime.start_server()` helper to determine its own PID via `os.getpid()` and write it to `~/.eggpool/eggpool.pid` before calling `server.serve()`.  Remove the lifespan handler that writes the PID inside the FastAPI app.  This change ensures that the PID file always refers to the supervisor process, aligning the CLI `stop` and `restart` commands with the actual process tree.

2. **Validate worker count and thread settings.** Ensure that the CLI’s `serve` command passes both `workers=1` and `threads=1` to Granian when running in “SBC” mode.  Expose a command‑line option or configuration file entry for `max_threads` so that operators on more capable hardware can increase concurrency.  With `threads=1`, Granian will still spawn a few internal threads for its runtime, but the number will be significantly reduced.

3. **Prevent duplicate instances.** Before launching a new server, check whether a process is already bound to the configured host and port.  The CLI can perform an HTTP `GET` to the `/v1/healthz` endpoint of the existing server; if a healthy response is received, it should refuse to start a second instance and instruct the user to stop the running server first.

4. **Simplify restart logic.** Given the corrected PID file, implement `eggpool restart` by sending a `SIGTERM` to the supervisor PID and then spawning a fresh server after ensuring the process has exited.  Avoid trying to manage the worker directly.

5. **Update documentation.** Document in the readme and deployment guides that Granian will always create one supervisor and one worker.  Explain that monitoring tools may show multiple threads but that memory usage is shared.  Provide instructions for running with minimal thread counts on low‑resource platforms.

These changes should be implemented in a new branch and thoroughly tested on x86_64 and Raspberry Pi platforms to ensure that the server properly shuts down and restarts without leaving orphaned processes.

## 5 Testing and verification

After applying the changes, perform the following verifications on a Raspberry Pi or similar device:

* Start EggPool via the CLI and observe the output of `ps -eLf` and `pstree -p`.  There should be exactly two PIDs related to EggPool (parent and child) and a limited number of threads (depending on the `threads` setting).

* Use `eggpool stop` and `eggpool restart` to ensure that the supervisor and worker both terminate cleanly and that only a single instance is running after restart.

* Install the systemd unit with `eggpool deploy systemd --install` and verify that `systemctl start eggpool` creates only the expected processes and that `systemctl stop eggpool` removes all of them.  Check that the PID file, if used, reflects the correct PID and is removed on shutdown.

* Run a high‑concurrency load test (e.g., using `wrk` or `autocannon`) with dozens of concurrent WebSocket sessions to confirm that the single process with an asynchronous event loop can handle the expected throughput without deadlocks or memory growth.

## 6 Fallback and alternative approaches

If after investigation it turns out that Granian cannot be configured to run without a supervisor/worker split and the two‑process model still consumes too much memory on the target hardware, consider the following alternatives:

* **Switch to Uvicorn or Hypercorn.** Uvicorn’s pure‑Python event loop can run in a single process without additional workers.  On very constrained hardware this may reduce memory usage at the cost of some performance.  Both servers support asynchronous FastAPI applications and can be integrated with the existing `runtime.start_server()` logic.

* **Use systemd socket activation.** Configure the systemd unit as `Type=notify` with socket activation so that the server only starts when the first connection arrives.  This prevents wasted resources when the server is idle.

* **Leverage cgroups or `systemd` memory limits.** To guard against runaway memory usage from threads, set `MemoryMax` and `TasksMax` in the systemd service unit.  While this does not change the process model, it provides safety on SBCs.

These alternatives should be evaluated if the primary plan does not meet resource constraints or if Granian’s architecture proves inflexible.

## 7 Conclusion

This plan outlines a path to ensure that EggPool runs with a single visible process (plus minimal threads) and does not accumulate orphaned workers when managed by its CLI or systemd.  By writing the PID file from the supervisor process, limiting the thread pool, preventing duplicate launches, and providing clear documentation, the server can operate reliably on devices with limited resources.  Thorough testing on Raspberry Pi hardware will confirm the effectiveness of these changes and identify any remaining issues.
