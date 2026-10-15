# On-Prem User Access Milestone 011 — Closure Status

Status: closed

Source implementation plan:

- `plans/implementation/on-prem-user-access/011-admin-dashboard-browser-parity-corrective.md`

Source subsystem roadmap:

- `plans/subsystems/on-prem-user-access-roadmap.md`

Repository baseline reviewed: `b0e8715f` (M007 conditional closure)

## 1. Executive finding

Both browser gates the plan required are now executable and green.

The frozen Python oracle was reconstructed and ran against the current Rust
dashboard. It reports **54 parity gaps, identical to the 54 at the pre-M011
baseline**: zero new and zero resolved. Every one of those gaps is the known
accepted historical difference — the Python oracle predates the on-prem work, so
it cannot contain the four on-prem navigation entries, and its empty-state
`/api/*` authorization differs from the Rust access model. M011 introduces no
regression on any existing page.

The on-prem admin/member pages have no Python counterpart, so they are owned by
EggPool and qualified against a Rust-owned approved reference. That gate drives
a real browser through both roles and froze **70 observations** that now pass in
compare mode with **22 screenshots** captured.

Driving the real browser found six genuine defects, including one that made
**browser sign-in impossible in production**. See section 3.

## 2. Requirement-to-evidence matrix

| Requirement | Evidence | Result | Notes |
|---|---|---|---|
| Historical-page Python-vs-Rust parity preserved | `qualification_dashboard_parity.py --skip-build` with the pinned oracle worktree | pass | 54 gaps at head and 54 at baseline `b0e8715f`; 0 new, 0 resolved. Comparison run at `/tmp/parity-hist.json` versus `/tmp/parity-base-run.json`. |
| Frozen oracle is runnable in this environment | `git worktree add --detach <EGGPOOL_DASHBOARD_ORACLE_ROOT> c23a7096`, x86_64 venv, package metadata installed with `--no-deps` | pass | Oracle must be a clean worktree at `ORACLE_COMMIT` **and** be installed into its venv; `PYTHONPATH` alone is insufficient because the app reads `importlib.metadata.version("eggpool")`. |
| New on-prem pages qualified by a Rust-owned reference | `--on-prem --screenshot-dir ...` | pass | 70 observations, 22 captures; `tests/fixtures/dashboard-on-prem-browser/reference.json` SHA-256 `9c770066560f334a1837442754f0cbf2321d12e197ef2150bba1f6ae600c94be`. |
| Administrator and member journeys | same gate | pass | Bootstrap → sign-in → profile create → member create → suspend/reactivate → override → budget → one-time token → forced password change → member pages. |
| Negative role matrix | same gate | pass | Anonymous, member, and personal agent credentials are each refused on every administrator route with `401`/`Administrator access required.` |
| Mouse and keyboard interaction | `Input.dispatchMouseEvent` sign-in, focus/label probes | pass | Administrator sign-in is performed with page scripting disabled and a real mouse click, proving the form needs no JavaScript. |
| Viewport, HiDPI, and theme coverage | same gate | pass | `1280x800`, `390x844`, `1280x800@2x`, and all four bundled themes on every admin and member page. |
| Hard versus estimated budget labelling | `admin.budget_labels`, `member.budget_labelling` | pass | Request and concurrency ceilings are labelled `(hard)`; token and microdollar ceilings stay `(estimated)`. |
| No token or credential leakage | DOM scan of every admin/member page | pass | Planted provider account key, provider header value, and all three run passwords are absent from every snapshot; one-time plaintext appears only on its reveal screen. |
| Restart durability | `restart.*` observations | pass | Browser session, profiles, and member policy state all survive a bounded SIGTERM and restart. |
| Workspace gates | `fmt --check`, `clippy --workspace --all-targets -D warnings`, `check`/`clippy --no-default-features`, serial `--workspace --all-targets` | pass | 967 tests passed, 0 failed, 1 ignored. |
| Tooling gates | `ruff format --check`, `ruff check`, `pyright`, `pytest tests/tooling/` | pass | 175 passed, 1 skipped. The installer harness passes 78/78 when run directly. |

## 3. Defects found and fixed by this gate

These were reproduced in a real browser and are now covered by the frozen
reference or by Rust tests.

1. **Browser sign-in was impossible in production (security/behavioural).**
   `server::auth::same_origin` required exactly one of `Origin` or `Referer`
   and returned `false` when both were present. Chrome sends both on a same-origin
   form POST, so every real browser received `403 Cross-origin sign-in is not
   allowed.` on `/login`. The check now validates every provenance header that is
   present, still fails closed on duplicated headers, userinfo authorities, and
   cross-site disagreement, and additionally rejects userinfo in an `Origin`.
   Covered by `same_origin_accepts_the_header_set_a_browser_form_post_sends`,
   `same_origin_rejects_disagreeing_or_duplicated_provenance`, and the HTTP-level
   `browser_sign_in_accepts_the_origin_and_referer_pair_a_real_browser_sends`.
2. **Top navigation overflowed at 1280px.** The `min-width: 1280px` tier forced
   `flex-wrap: nowrap` using a measurement taken with thirteen links; the
   on-prem navigation has sixteen. The document scrolled 212px past a 1280x800
   viewport. The tier now starts at 1560px, above the 1520px the group actually
   needs, and the 1280–1559 range keeps the inherited wrap. `nav.topnav` also
   gained `min-width: 0; max-width: 100%` so a long menu cannot widen the bar.
3. **Member usage pages rendered as bare fragments.** `render_usage_admin` and
   `render_my_usage` returned a `<section>` with no document, stylesheet, or
   viewport declaration, so `/usage` was unstyled and non-responsive for both
   roles. Both now render through a shared `on_prem_page` shell with
   `.table-scroll` regions.
4. **Missing viewport declaration.** `/admin/audit`, `/my-account/models`, and
   the account error page declared no viewport.
5. **Unbounded tables.** The audit and personal-credential tables were not
   wrapped in the keyboard-reachable `.table-scroll` region used everywhere else.
6. **Theme selector had no accessible name**, and the admin budget form did not
   distinguish hard ceilings from estimates.

Two harness defects were also corrected: the overflow probe used
`window.scrollX`, which misreports under `html { overflow-x: hidden }` — it now
compares `scrollWidth` against `clientWidth` and names the offending elements;
and the no-JS proof previously disabled scripts and then submitted through
scripted `requestSubmit`, which proved nothing.

## 4. Production and qualification artifact manifest

- `tests/fixtures/dashboard-on-prem-browser/reference.json` — approved on-prem
  browser reference, SHA-256 `9c770066560f334a1837442754f0cbf2321d12e197ef2150bba1f6ae600c94be`.
- Candidate used by every gate run: `rust/target/debug/eggpool`; SHA-256
  `88246ba001dc950da9551ea8acc05e7207244698e2784bc0ee8cd33a5a11d3e4`.
- Screenshot evidence: 22 captures, aggregate SHA-256
  `30aa3cb7b18cdf0b7f88e694a28e4de63cc53e204c1054ea5622abc448718ad5`. Captures
  are held outside the repository; the reference records their keys, pixel
  dimensions, and per-image SHA-256.
- Historical parity reports: `/tmp/parity-hist.json` (head) and
  `/tmp/parity-base-run.json` (baseline `b0e8715f`).
- Host: macOS on Apple Silicon, heavily loaded development machine. The oracle
  venv is x86_64 because the repository tooling runs under Rosetta; it must match
  the tooling interpreter's architecture or its native wheels fail to load.
- No capture contains a credential, prompt, request body, or private provider
  data; sentinel markers are fixture-local constants.

## 5. Known limitations carried forward

- The 54 historical parity gaps remain open and are unchanged. They are the
  accepted on-prem-vs-Python differences plus the Rust empty-state API access
  model; this milestone does not claim them as parity.
- The shared navigation still offers members links to administrator-only pages
  (`/models`, `/latency`), which answer `Administrator access required.` with no
  chrome. Filtering the navigation per role is a design change outside this
  milestone's bounded defect scope and is recorded here rather than silently
  changed.
- `tests/tooling/test_installer.py::test_quick_installer_harness_passes` exceeds
  its 180s budget on this host. The harness itself passes 78/78 when invoked
  directly, and it does the same at baseline `b0e8715f`, so this is host timing
  rather than a regression. It belongs to the deployment plan, not M011.
- Physical SBC, small-team capacity, and hard USD/token guarantees remain
  unqualified. M011 makes no such claim.

## 6. Unblock audit

Searched `plans/implementation/on-prem-user-access/`, the subsystem dependency
graph, and the ready/active/blocked registry tables.

- **M012** hard-depends on accepted M008–M011 closures. M011 is now accepted, but
  M008 remains ready rather than closed and M009/M010 remain blocked on it, so
  **M012 stays blocked**. No promotion is authorized.
- **M009/M010** are unaffected; their blockers are budget-state contracts, not
  browser evidence.
- **M008** is independent and stays ready.
- M006's recorded condition that browser parity captures were outstanding is
  satisfied additively by this closure. Per the plan, the immutable M006 record
  is not retroactively altered; this record supplies the corrective proof.

No other plan in the registry declares the admin/member dashboard surface as a
dependency.

## 7. Reproduction

```bash
# historical pages against the frozen Python oracle
EGGPOOL_DASHBOARD_ORACLE_ROOT=<worktree at c23a7096> \
EGGPOOL_DASHBOARD_ORACLE_PYTHON=<oracle venv>/bin/python \
uv run python scripts/qualification_dashboard_parity.py --skip-build

# on-prem admin/member pages against the approved Rust-owned reference
uv run python scripts/qualification_dashboard_parity.py --skip-build --on-prem \
  --screenshot-dir /tmp/on-prem-shots
```

Re-freeze the on-prem reference only after reviewing the reported diff:

```bash
uv run python scripts/qualification_dashboard_parity.py --on-prem \
  --write-on-prem-reference
```