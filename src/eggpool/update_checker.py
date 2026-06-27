"""Periodic PyPI update checker used by the dashboard.

The :class:`UpdateChecker` is the single source of truth for "is there a
newer eggpool release available?" — both the dashboard footer indicator
and the CLI ``eggpool update`` command resolve their PyPI lookup through
this module so the two paths cannot drift.

State is held in a small, immutable :class:`UpdateInfo` dataclass so
readers can grab a snapshot without taking a lock.  Mutating writes are
serialized through an ``asyncio.Lock`` so the periodic background task
and any synchronous ``snapshot()`` call from a request handler can
co-exist without tearing.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import logging
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


PYPI_URL = "https://pypi.org/pypi/eggpool/json"
_CHECK_TIMEOUT_S = 15.0
_DEFAULT_CHECK_INTERVAL_S = 24 * 60 * 60
_VERSION_RE = re.compile(
    r"^\s*v?"
    r"(?P<release>\d+(?:\.\d+)*)"
    r"(?:[-_.]?(?P<pre_l>a|b|rc)[-_.]?(?P<pre_n>\d*))?"
    r"(?:[-_.]?post[-_.]?(?P<post_n>\d*))?"
    r"(?:[-_.]?dev[-_.]?(?P<dev_n>\d*))?",
    re.IGNORECASE,
)
_PRE_RELEASE_RANK = {"a": 1, "b": 2, "rc": 3}


class UpdateCheckError(RuntimeError):
    """Raised when the PyPI lookup fails or returns an unparseable body."""


@dataclass(frozen=True)
class UpdateInfo:
    """Immutable snapshot of the latest update check.

    ``update_available`` is ``True`` only when both ``current_version``
    and ``latest_version`` are known and differ.  When the periodic
    check has never completed or the last attempt failed,
    ``update_available`` is ``False`` and ``last_check_error`` carries
    the failure reason — the dashboard renders nothing in either case,
    matching the "no indicator unless there is an update" contract.
    """

    current_version: str = ""
    latest_version: str = ""
    update_available: bool = False
    install_method: str = "unknown"
    update_command: str = "eggpool update"
    last_check_at: float = 0.0
    last_check_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe dict representation for the API endpoint."""
        return asdict(self)


@dataclass
class UpdateChecker:
    """Periodic PyPI update checker.

    The class is intentionally light: it holds the latest
    :class:`UpdateInfo` snapshot, exposes :meth:`snapshot` for readers,
    and drives the :meth:`check_once` and :meth:`run_periodic` coroutines
    used by the lifespan-managed background task.

    The default ``check_interval_s`` of 86400 (24h) keeps PyPI traffic
    well under their anonymous rate limit while still surfacing new
    releases within a day.  Tests override it via the constructor.

    When ``_client`` is provided (an ``httpx.AsyncClient``), the
    periodic checker reuses it instead of constructing a fresh
    connection per check.  This avoids repeated DNS lookups, TCP
    handshakes, and TLS negotiations on SBC / Pi-hole deployments.
    """

    package_name: str = "eggpool"
    check_interval_s: float = _DEFAULT_CHECK_INTERVAL_S
    _http_get: Callable[..., httpx.Response] | None = None
    _client: httpx.AsyncClient | None = None
    _version_lookup: Callable[[str], str] | None = None
    _install_method_lookup: Callable[[], str] | None = None
    _info: UpdateInfo = field(default_factory=UpdateInfo)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def check_once(self) -> UpdateInfo:
        """Perform a single PyPI lookup and update internal state.

        Network failures are swallowed and recorded in
        ``last_check_error``; the returned snapshot always reflects the
        latest state so callers do not need a try/except.  Concurrent
        callers serialize through ``_lock`` to avoid duplicate PyPI
        hits.
        """
        async with self._lock:
            current_version = self._resolve_current_version()
            install_method = self._resolve_install_method()
            latest_version, error = await self._fetch_latest_version()
            if error:
                info = UpdateInfo(
                    current_version=current_version,
                    latest_version=self._info.latest_version,
                    update_available=self._is_newer(
                        current_version, self._info.latest_version
                    ),
                    install_method=install_method,
                    update_command=self._build_update_command(install_method),
                    last_check_at=asyncio.get_event_loop().time(),
                    last_check_error=error,
                )
            else:
                info = UpdateInfo(
                    current_version=current_version,
                    latest_version=latest_version,
                    update_available=self._is_newer(current_version, latest_version),
                    install_method=install_method,
                    update_command=self._build_update_command(install_method),
                    last_check_at=asyncio.get_event_loop().time(),
                    last_check_error="",
                )
            self._info = info
            return info

    async def run_periodic(self) -> None:
        """Run :meth:`check_once` forever, sleeping ``check_interval_s`` between.

        Designed for the lifespan-managed ``TaskSupervisor``: each call
        blocks until cancelled.  Failures are logged and swallowed so a
        single bad check never kills the loop — the supervisor will
        restart it but that defeats the purpose of a daily probe.
        """
        # Run an initial check immediately so a freshly-started server
        # surfaces the latest state on the very first page render.
        try:
            await self.check_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — best-effort probe
            logger.warning("Initial update check failed: %s", exc)
        while True:
            try:
                await asyncio.sleep(self.check_interval_s)
            except asyncio.CancelledError:
                raise
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — best-effort probe
                logger.warning("Periodic update check failed: %s", exc)

    def snapshot(self) -> UpdateInfo:
        """Return the most recent :class:`UpdateInfo` snapshot.

        Returns a fresh dataclass via :func:`dataclasses.replace` so
        callers can mutate the returned value without leaking state
        back into the checker.  ``UpdateInfo`` is ``frozen=True`` so
        attribute writes should normally raise, but ``object.__setattr__``
        bypasses the freeze — the copy guarantees clean isolation.
        """
        return replace(self._info)

    # -- Internals ---------------------------------------------------------

    def _resolve_current_version(self) -> str:
        """Resolve the installed eggpool version.

        Falls back to ``"0.0.0"`` when ``importlib.metadata`` cannot
        find the distribution — happens in editable source checkouts
        without a built dist-info.
        """
        lookup = self._version_lookup or _default_version_lookup
        try:
            return lookup(self.package_name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not resolve installed version: %s", exc)
            return "0.0.0"

    def _resolve_install_method(self) -> str:
        """Resolve the install method via the shared helper.

        Tests inject a stub via :attr:`_install_method_lookup`; the CLI
        keeps the canonical implementation in ``cli_full``.
        """
        lookup = self._install_method_lookup or _default_install_method
        try:
            return lookup()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not resolve install method: %s", exc)
            return "unknown"

    async def _fetch_latest_version(self) -> tuple[str, str]:
        """Hit PyPI and return ``(latest_version, error_message)``.

        One of the two is always empty.  A failure path returns the
        previous latest_version (so callers can still detect an update
        that was found on an earlier successful check) and an error
        string for diagnostics.

        When ``_client`` is provided, uses the shared async client
        directly (no thread offload).  Falls back to synchronous
        ``httpx.get`` via ``asyncio.to_thread`` otherwise.
        """
        try:
            if self._client is not None:
                response = await self._client.get(PYPI_URL, timeout=_CHECK_TIMEOUT_S)
            else:
                response = await asyncio.to_thread(self._http_get_sync)
        except Exception as exc:  # noqa: BLE001
            return self._info.latest_version, f"pypi: {exc}"
        if response is None:
            return self._info.latest_version, "pypi: empty response"
        try:
            latest = _latest_version_from_response(response)
        except UpdateCheckError as exc:
            return self._info.latest_version, f"pypi: {exc}"
        return latest, ""

    def _http_get_sync(self) -> httpx.Response | None:
        """Synchronous PyPI GET — runs inside ``asyncio.to_thread``."""
        get = self._http_get
        if get is not None:
            return get(PYPI_URL, timeout=_CHECK_TIMEOUT_S, follow_redirects=True)
        return httpx.get(PYPI_URL, timeout=_CHECK_TIMEOUT_S, follow_redirects=True)

    @staticmethod
    def _is_newer(current: str, latest: str) -> bool:
        """Return True when ``latest`` is strictly newer than ``current``.

        Empty inputs are treated as unknown — never raises "update
        available" on incomplete data.  Comparison is lexicographic on
        PEP 440 versions, which matches ``packaging.version.Version``
        ordering for the simple ``X.Y.Z`` tags the project publishes.
        """
        if not current or not latest:
            return False
        return _pep440_key(latest) > _pep440_key(current)

    @staticmethod
    def _build_update_command(install_method: str) -> str:
        """Return the user-facing update command.

        The dashboard surfaces a copy-pasteable string regardless of
        install method so operators do not have to remember whether
        they installed via pip, pipx, uv-tool, or source.  The CLI's
        ``eggpool update`` re-detects the method at runtime and is
        always the safe choice.  ``install_method`` is accepted for
        forward compatibility with a future install-aware variant.
        """
        del install_method  # Reserved for a future install-aware variant
        return "eggpool update"


# -- Module-level helpers (test seam + shared install-method probe) ---------


def _default_version_lookup(package_name: str) -> str:
    """Return the installed distribution version for *package_name*."""
    return importlib.metadata.version(package_name)


def _default_install_method() -> str:
    """Detect how eggpool was installed: ``pipx``, ``uv-tool``, ``source``, ``pip``.

    Mirrors the canonical implementation in ``cli_full`` so both the
    CLI and the background checker produce the same answer without
    duplicating the heuristic.  Kept here so the module is self
    contained and importable without dragging in Click.
    """
    in_venv = hasattr(sys, "real_prefix") or (
        hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix
    )

    if in_venv:
        eggpool_exe = shutil.which("eggpool")
        candidates: list[Path] = []
        if eggpool_exe is not None:
            candidates.append(Path(eggpool_exe).resolve())
        candidates.append(Path(sys.executable).resolve())

        for exe in candidates:
            parts = exe.parts
            if "uv" in parts and "tools" in parts:
                return "uv-tool"
            if "pipx" in parts and ("venvs" in parts or "shared" in parts):
                return "pipx"
        return "pip"

    cli_path = Path(__file__).resolve()
    if (cli_path.parent.parent.parent / "pyproject.toml").exists():
        return "source"
    return "pip"


def _pep440_key(version: str) -> tuple[int, ...]:
    """Best-effort PEP 440 ordering key for the project's release tags.

    The project publishes simple ``X.Y.Z`` releases, but local checkouts
    and PyPI may still expose common prerelease or postrelease suffixes.
    This helper deliberately handles only the subset needed here without
    adding a runtime dependency on ``packaging``:

    ``.dev`` < ``a`` < ``b`` < ``rc`` < final < ``.post``.
    """
    match = _VERSION_RE.match(version)
    if match is None:
        return (0, 0, 0, 0, 4, 0)

    release = [int(part) for part in match.group("release").split(".")]
    while len(release) > 1 and release[-1] == 0:
        release.pop()
    release = release[:4]
    release.extend([0] * (4 - len(release)))

    if match.group("dev_n") is not None:
        rank = 0
        suffix_number = int(match.group("dev_n") or "0")
    elif match.group("pre_l") is not None:
        rank = _PRE_RELEASE_RANK[match.group("pre_l").lower()]
        suffix_number = int(match.group("pre_n") or "0")
    elif match.group("post_n") is not None:
        rank = 5
        suffix_number = int(match.group("post_n") or "0")
    else:
        rank = 4
        suffix_number = 0

    return (*release, rank, suffix_number)


def _latest_version_from_response(response: httpx.Response) -> str:
    """Extract the PyPI ``info.version`` field from a successful response."""
    try:
        response.raise_for_status()
        data_raw: object = response.json()
    except Exception as exc:  # noqa: BLE001
        raise UpdateCheckError(str(exc)) from exc

    if not isinstance(data_raw, dict):
        raise UpdateCheckError("invalid response")
    data = cast("dict[str, object]", data_raw)
    info = data.get("info")
    if not isinstance(info, dict):
        raise UpdateCheckError("empty version")
    info_data = cast("dict[str, object]", info)
    latest = str(info_data.get("version") or "")
    if not latest:
        raise UpdateCheckError("empty version")
    return latest


def async_check_for_update(
    *,
    package_name: str = "eggpool",
    timeout_s: float = _CHECK_TIMEOUT_S,
) -> tuple[str, str, str]:
    """Convenience helper for one-shot CLI use.

    Returns ``(current_version, latest_version, error_message)`` — at
    least one of the three will be empty on success and on failure.
    Kept module-public so :mod:`cli_full` can call it without
    instantiating an :class:`UpdateChecker`.

    This is a synchronous helper for CLI paths only.  Background checks
    go through :class:`UpdateChecker` which reuses the shared async
    client from the :class:`OutboundClientManager`.
    """
    try:
        current = importlib.metadata.version(package_name)
    except Exception as exc:  # noqa: BLE001
        return "", "", f"version: {exc}"
    try:
        response = httpx.get(PYPI_URL, timeout=timeout_s, follow_redirects=True)
        latest = _latest_version_from_response(response)
    except Exception as exc:  # noqa: BLE001
        return current, "", f"pypi: {exc}"
    return current, latest, ""


# Indirection for ``cli_full`` so the refactor stays small.
def schedule_check(checker: UpdateChecker) -> Callable[[], object]:
    """Return a no-arg coroutine factory for the periodic check loop.

    Kept as a thin alias because ``checker.run_periodic`` is a bound
    method and access through this function is easier to mock in tests.
    """
    return checker.run_periodic


__all__ = [
    "PYPI_URL",
    "UpdateCheckError",
    "UpdateChecker",
    "UpdateInfo",
    "async_check_for_update",
    "schedule_check",
]
