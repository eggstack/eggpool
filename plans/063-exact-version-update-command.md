# Plan 063 — Exact-Version Update Command

Date: 2026-07-31
Status: ready for implementation
Parent roadmap: `plans/058-durable-convergence-exact-update-sbc-hotpath-roadmap.md`
Planning baseline: `3e4f41ff6efed4a1a69a9bb0a6147891d0b2b2b3`

## Purpose

Extend `eggpool update` with one optional exact-version argument while preserving the current bare-command behavior.

Required user-facing forms:

```text
eggpool update
eggpool update 0.6.4
eggpool update v0.6.5
```

The two explicit forms must normalize to the same target, verify that the exact EggPool release exists, and install that release. A requested older version is a deliberate downgrade and must not be blocked by the current "latest is newer" comparison. A nonexistent release must produce a clear error without invoking an installer or restarting the server.

This is a small CLI feature. It does not require a new updater service, release index cache, dependency resolver, `packaging` runtime dependency, GitHub API client, background task, or test matrix across operating systems.

## Current behavior

`eggpool update` currently:

1. performs a fresh PyPI latest-version lookup through `async_check_for_update()`;
2. prints current and latest versions;
3. returns "Already up to date" unless latest is newer;
4. chooses an install command based on pip, pipx, uv-tool, source checkout, or `--from-source`;
5. runs the installer;
6. prints the installed version;
7. restarts the server when it was running.

This bare path must continue to work as it does now, including the second fresh PyPI lookup used to avoid a stale "already up to date" result.

## Scope

Primary files:

- `src/eggpool/cli_full.py`
- `src/eggpool/update_checker.py`
- existing update-checker unit tests
- one existing CLI runner/update command test file, or the closest existing CLI capability file
- CLI help/documentation only where the command syntax is listed

Potential small supporting change:

- one private update-command builder in `cli_full.py` to keep install-method branching testable.

## Explicitly out of scope

- automatic rollback after a failed package install;
- a self-update daemon;
- signed release metadata beyond the trust already delegated to PyPI/TLS/package tooling;
- release-channel support;
- semantic version ranges such as `>=0.6,<0.7`;
- wildcard targets;
- GitHub release discovery;
- a local release cache;
- modifying PyPI publishing;
- a new version-parsing dependency;
- full subprocess execution tests for every installer;
- live PyPI calls in CI.

## Command contract

### Bare command

```text
eggpool update
```

- Performs the existing live latest-version lookup.
- Updates only when latest is newer.
- Prints `Already up to date.` when current is equal to or newer than the latest returned release.
- Retains `--check` and `--from-source` behavior unless this plan explicitly adjusts command construction for consistency.

### Exact target

```text
eggpool update 0.6.4
eggpool update v0.6.4
```

- Strips one optional leading ASCII `v` or `V`.
- Validates the remaining version as one complete version string in the project's supported PEP 440 subset.
- Performs an exact-release PyPI lookup.
- Uses the canonical version returned by PyPI as the install target after confirming it is equivalent to the requested normalized value.
- Installs the target whether it is newer or older than the current version.
- Returns without installing when the exact target is already installed.
- Fails clearly when the release does not exist.

## Phase A — Add and normalize the optional argument

### Click surface

Add one optional argument before the existing options are consumed:

```python
@click.argument("version", required=False)
```

Use a descriptive internal name such as `requested_version`.

### Required normalization helper

Add one small private helper, preferably in `update_checker.py` because HTTP exact-release lookup and version parsing belong together:

```python
def normalize_requested_version(raw: str) -> str:
    ...
```

Required behavior:

1. Trim surrounding whitespace.
2. Remove exactly one leading `v` or `V`.
3. Reject an empty result.
4. Require a full-string match, not the current prefix-only comparison parser behavior.
5. Accept the same simple release/prerelease/post/dev forms already supported by EggPool's internal PEP 440 subset.
6. Reject ranges, commas, URL fragments, shell syntax, path separators, whitespace inside the version, and trailing junk.
7. Return the normalized version without a leading `v`.
8. Raise a narrow `ValueError` or `UpdateCheckError` with a user-readable reason.

Do not add `packaging` solely for this command. The project publishes simple versions and already has a local ordering parser.

### CLI error behavior

Invalid input should print a concise message such as:

```text
Error: invalid EggPool version 'vfoo'. Expected a version such as 0.6.5 or v0.6.5.
```

Exit non-zero before any network or subprocess call.

### Acceptance criteria

- `0.6.4` normalizes to `0.6.4`.
- `v0.6.4` and `V0.6.4` normalize to `0.6.4`.
- Leading/trailing whitespace is handled by the helper if passed directly.
- Empty, `v`, version ranges, URLs, and trailing junk are rejected.
- Bare invocation passes `None` and follows the existing path unchanged.

## Phase B — Add exact PyPI release lookup

### Preferred endpoint

Use PyPI's exact release metadata endpoint:

```text
https://pypi.org/pypi/eggpool/{version}/json
```

Keep URL construction internal and use the validated normalized version. No general URL escaping utility is required beyond safe validated input.

### Suggested helper

```python
def check_exact_release(
    requested_version: str,
    *,
    package_name: str = "eggpool",
    timeout_s: float = _CHECK_TIMEOUT_S,
    http_get: Callable[..., httpx.Response] | None = None,
) -> tuple[str, str]:
    """Return (canonical_version, error_message)."""
```

Exact name/shape may vary, but the CLI needs to distinguish:

- release exists;
- release does not exist (HTTP 404);
- network/PyPI failure;
- malformed response;
- returned version mismatch.

### Required behavior

1. Send the same no-cache headers used by the current CLI update check.
2. One exact-release request is sufficient. Do not perform the latest-path second request because the endpoint itself identifies the requested release.
3. HTTP 404 produces a specific missing-release result.
4. Other HTTP failures produce a PyPI/network error, not "version does not exist."
5. Parse `info.version` using the existing response validation style.
6. Confirm the returned canonical version is equivalent to the requested normalized target.
7. Return the canonical PyPI version for command construction and post-install verification.
8. Do not consult cached dashboard `UpdateInfo` for exact requests.

### User-facing errors

Missing release:

```text
Error: EggPool version 0.6.99 does not exist on PyPI.
```

Other lookup failure:

```text
Error checking EggPool version 0.6.4: <bounded reason>
```

Do not silently fall back to latest.

### Acceptance criteria

- Exact release response returns its canonical version.
- HTTP 404 is reported as nonexistent version.
- HTTP 5xx/network failure is reported as lookup failure.
- Malformed/mismatched metadata fails closed.
- No installer or restart is attempted on any lookup failure.

## Phase C — Split latest and exact command flow

### Required flow

At the top of `update()`:

1. Resolve current installed version once.
2. If no requested version:
   - call existing `async_check_for_update()`;
   - preserve all current latest comparison/output behavior.
3. If a requested version exists:
   - normalize it;
   - check exact release existence;
   - print current and requested/canonical target;
   - compare for equality only to decide whether work is needed;
   - do not require the target to be newer.
4. `--check` with exact target:
   - verifies existence;
   - reports whether it is already installed or available for install;
   - performs no subprocess or restart.
5. Keep installer selection in one private helper so the latest and exact paths do not duplicate branching.

### Suggested output

Exact target, different version:

```text
Current version:   0.6.5
Requested version: 0.6.4
Updating from 0.6.5 to 0.6.4...
```

Exact target, already installed:

```text
Current version:   0.6.4
Requested version: 0.6.4
Requested version is already installed.
```

The word "Updating" may remain for a downgrade; no separate downgrade framework is needed. An optional parenthetical `downgrade` label is acceptable but not required.

### Acceptance criteria

- Latest path still uses `is_newer_version()`.
- Exact path does not use `is_newer_version()` as an install gate.
- Exact older target proceeds to installer construction.
- Exact equal target performs no install.
- `--check` exact performs lookup only.

## Phase D — Build exact installer commands

Create or extend one private helper that accepts:

- install method;
- target version;
- whether the source/Git path is forced;
- repository name;
- source checkout root when relevant.

### PyPI target commands

For exact package targets, use an explicit requirement everywhere:

```text
eggpool==<canonical_version>
```

Preferred commands:

- `uv-tool`: `uv tool install --force eggpool==<version>`
- `pipx`: `pipx install --force eggpool==<version>`
- `pip`: `<python> -m pip install eggpool==<version>`

The exact requirement permits deliberate downgrade. Do not use a latest-only `pipx upgrade eggpool` command for explicit targets.

### `--from-source` exact target

For pipx, uv-tool, and pip installations, pin the existing Git target to the normalized tag:

```text
git+https://github.com/eggstack/eggpool.git@v<version>
```

Do not perform a second GitHub API existence lookup. PyPI exact existence is the requested product-release check; a missing Git tag remains an installer failure with stderr.

### Source checkout behavior

A local source checkout cannot safely be moved to an arbitrary tag without deciding how to handle a dirty tree, branch state, and local changes. Do not hide that complexity inside this narrow command.

Required behavior for `method == "source"` with an explicit target:

1. Verify the exact PyPI release first.
2. Refuse before modifying the checkout.
3. Print an actionable message:

```text
Error: exact-version update cannot safely modify a source checkout.
  Run `git checkout v0.6.4 && uv sync --no-dev`, or use a pipx/uv-tool install.
```

Bare `eggpool update` for source checkout retains its existing behavior. A separate source-checkout updater would require its own explicit design and is not part of this plan.

### Acceptance criteria

- uv-tool and pipx explicit targets include `--force` and `eggpool==version`.
- pip explicit target uses an exact requirement and permits downgrade.
- `--from-source` exact target uses `@v<version>` for supported installed methods.
- Source checkout explicit target fails clearly without running git checkout/reset.
- Bare latest command construction remains unchanged.

## Phase E — Verify exact installation before restart

### Required changes

1. After a successful installer subprocess, resolve installed `eggpool` version again.
2. Latest path may preserve current reporting, but should still fail if version lookup is impossible after a supposedly successful install when practical.
3. Exact path must require the installed version to equal the canonical target.
4. If exact verification fails:
   - print expected and observed versions;
   - exit non-zero;
   - do not restart the server.
5. Restart only after successful exact verification.
6. Preserve the current behavior of reporting when the server is not running.
7. Do not attempt automatic rollback to the previous package version. Package-manager stderr and exact verification are sufficient for this local tool.

### Acceptance criteria

- Successful exact install prints the target as installed.
- Mismatched installed version exits non-zero.
- Restart is not called after installer failure or version mismatch.
- Restart behavior after verified install is unchanged.

## Phase F — Help and focused verification

### Help/documentation

Update the command docstring/help to state:

- no argument updates to latest;
- optional `VERSION` installs an exact published version;
- leading `v` is accepted;
- exact source-checkout targeting is refused with manual guidance.

Do not add a release-management guide or separate updater document unless the command is already documented in an existing CLI reference.

### Test budget

Add or modify no more than six focused cases across existing update-checker and CLI files.

Required coverage:

1. Normalization accepts `0.6.4` and `v0.6.4`, preferably parameterized, and rejects malformed/range input.
2. Exact-release 200 returns canonical version; 404 returns the missing-release error.
3. Bare `eggpool update` still follows the latest path and prints `Already up to date.` when appropriate.
4. Explicit older version constructs an exact installer command rather than returning up to date.
5. Missing exact version invokes no subprocess and no restart.
6. Post-install mismatch invokes no restart and exits non-zero.

Command-builder assertions may parameterize pip, pipx, and uv-tool without executing those binaries. Run one representative CLI path through Click's test runner. Do not make live PyPI calls.

## Implementation sequence

Recommended commits:

1. version normalization and exact PyPI helper;
2. optional CLI argument and split latest/exact flow;
3. exact command builder, source-checkout error, and install verification;
4. focused tests/help and plan closure.

## Plan acceptance criteria

- [ ] `eggpool update` retains current latest-version behavior.
- [ ] `eggpool update 0.6.4` and `eggpool update v0.6.4` resolve to the same target.
- [ ] Input is validated as one complete supported version string.
- [ ] Exact target existence is checked through PyPI before installation.
- [ ] HTTP 404 produces a clear nonexistent-version error.
- [ ] Exact target never falls back silently to latest.
- [ ] An older exact target is installed as a deliberate downgrade.
- [ ] An equal exact target performs no installer or restart.
- [ ] pip, pipx, and uv-tool commands are pinned to `eggpool==<version>`.
- [ ] Exact `--from-source` commands pin `@v<version>` for supported installed methods.
- [ ] Source checkout exact targeting fails safely with manual guidance and does not modify git state.
- [ ] Installed version is verified against the exact target before restart.
- [ ] No new dependency, updater service, release cache, GitHub API client, CI job, or installer matrix is introduced.

## Definition of done

The plan is complete when bare update still targets latest, exact versions with or without a leading `v` are verified and installed or clearly rejected, deliberate downgrade works for package-managed installs, post-install version is exact before restart, and focused regressions plus the existing smoke suite pass.