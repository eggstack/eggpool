# ruff: noqa: F405
from __future__ import annotations

from ._shared import *  # noqa: F403


def _fetch(url: str, *, headers: dict[str, str] | None = None) -> HttpResult:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return HttpResult(
            status=cast("int", response.status),
            headers={key.casefold(): value for key, value in response.headers.items()},
            body=response.read().decode("utf-8", errors="replace"),
        )


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_tcp(port: int, process: subprocess.Popen[bytes], name: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                return
        except OSError:
            time.sleep(0.05)
    diagnostic = ""
    if process.poll() is not None and process.stderr is not None:
        diagnostic = process.stderr.read().decode("utf-8", errors="replace")[-500:]
    raise QualificationError(f"{name} on port {port} did not start: {diagnostic}")


def _wait_for_operational_event(
    database_path: Path, process: subprocess.Popen[bytes], event_type: str, name: str
) -> None:
    """Wait until startup recovery is in the completed one-second query window."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if database_path.exists():
            try:
                with sqlite3.connect(database_path, timeout=0.2) as connection:
                    row = connection.execute(
                        "SELECT 1 FROM operational_events WHERE event_type=? "
                        "AND occurred_at < datetime('now') LIMIT 1",
                        (event_type,),
                    ).fetchone()
                if row is not None:
                    return
            except sqlite3.Error:
                # Startup may still be applying migrations or opening SQLite.
                pass
        time.sleep(0.05)
    diagnostic = ""
    if process.poll() is not None and process.stderr is not None:
        diagnostic = process.stderr.read().decode("utf-8", errors="replace")[-500:]
    raise QualificationError(
        f"{name} did not persist {event_type!r} before dashboard reads: {diagnostic}"
    )


__all__ = ["_fetch", "_port", "_wait_for_operational_event", "_wait_for_tcp"]
