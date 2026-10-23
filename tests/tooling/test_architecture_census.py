"""Guards that keep the documented architecture censuses aligned with the code.

The guards parse source and documentation as text only. They never build the
Rust workspace, never reach the network, and never write to the repository.

Each guard is scoped to one designated region of one designated document so
unrelated prose cannot make it fail:

* the CLI census guard reads only the review index's ``command tree (...)``
  sentence;
* the migration census guard reads only the ``v1-vN`` census statements;
* the module map guard reads only the ``## Layout`` section of the agent
  workflow index;
* the route topology guard reads only the review index's ``Route topology:``
  paragraph;
* the wire ownership guard reads only the review index's wire section;
* the schema-census guard reads only the ``schema-N`` references in the
  architecture documents.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[2]
OVERVIEW = ROOT / "architecture" / "overview.md"
AGENTS = ROOT / "AGENTS.md"
ARCHITECTURE_DIR = ROOT / "architecture"
CLI_SOURCE = ROOT / "rust" / "src" / "cli.rs"
MIGRATION_DIR = ROOT / "rust" / "assets" / "db" / "migrations"
RUNTIME_SRC = ROOT / "rust" / "src"
SERVER_MOD = ROOT / "rust" / "src" / "server" / "mod.rs"
WIRE_DIR = ROOT / "rust" / "src" / "wire"

# The one architecture document that IS a historical schema snapshot. Its
# census describes the schema that was current when it ran, so a version
# reference inside it is a fact about the past rather than a claim about the
# present.
SCHEMA_INVESTIGATION = "persistence-control-projection-investigation.md"

# A schema version stated in prose, as `schema-54` or `schema 54`.
SCHEMA_VERSION_RE = re.compile(r"schema[-\s](\d+)", re.IGNORECASE)

# The sanctioned way an architecture document scopes a historical schema
# reference. A document that uses this phrase may name an older version.
HISTORICAL_SCOPE_PHRASE = "when the investigation ran"

# The review index states the CLI census in exactly one sentence.
CLI_CENSUS_RE = re.compile(r"command tree \((\d+) commands: ([^)]*)\)")

# The migration chain census is stated as `v1-vN` with an en dash, an em dash,
# or a hyphen. A bare `/v1/...` path never matches because the second `v` is
# required.
MIGRATION_CENSUS_RE = re.compile(r"v1\s*[\u2013\u2014-]\s*v(\d+)")

# A Clap variant name in kebab case, matching clap's default rename rule.
KEBAB_RE_1 = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
KEBAB_RE_2 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")

# `.route("...")` registrations, including the multi-line form.
ROUTE_RE = re.compile(r'\.route\(\s*"([^"]+)"')

# A backticked route path inside the review index's route topology paragraph.
ROUTE_TOKEN_RE = re.compile(r"`(/[^`\s]*)`")

# Verbs that credit a module with owning an implementation.
OWNERSHIP_VERB_RE = re.compile(
    r"\b(?:captures?|implements?|owns|accepts?|defines?|encodes?|manages?"
    r"|performs?|provides?|holds?|admits?)\b"
)

# A root wire facade file name such as `ir.rs`.
FACADE_FILE_RE = re.compile(r"(?<![\w])([a-z_]+)\.rs")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _kebab(name: str) -> str:
    """Return clap's default kebab-case rename of a variant name."""
    text = KEBAB_RE_1.sub("-", name)
    text = KEBAB_RE_2.sub("-", text)
    return text.lower()


def _cli_command_names(source: str) -> list[str]:
    """Return the effective Clap command names of the top-level Command enum."""
    lines = source.splitlines()
    starts = [
        index
        for index, line in enumerate(lines)
        if line.startswith("pub enum Command {")
    ]
    assert len(starts) == 1, "expected exactly one top-level `Command` enum"
    body: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if line == "}":
            break
        body.append(line)
    else:
        raise AssertionError("the `Command` enum is not closed")

    names: list[str] = []
    explicit: str | None = None
    for line in body:
        attribute = re.search(r'#\[command\([^)]*name\s*=\s*"([^"]+)"', line)
        if attribute is not None:
            explicit = attribute.group(1)
            continue
        variant = re.match(r"^    ([A-Z][A-Za-z0-9]*)\s*(?:\(|\{|,|$)", line)
        if variant is not None:
            names.append(explicit if explicit is not None else _kebab(variant.group(1)))
            explicit = None
    return names


def _documented_cli_commands(text: str) -> tuple[int, list[str]]:
    """Return the CLI census stated in the review index."""
    matches = CLI_CENSUS_RE.findall(text)
    assert len(matches) == 1, (
        "the review index must state the CLI census exactly once; "
        f"found {len(matches)} matches"
    )
    count_text, listing = matches[0]
    listed = [item.strip().strip("`") for item in listing.split(",")]
    return int(count_text), listed


def _migration_versions() -> list[int]:
    """Return the sorted migration versions shipped in the migration directory."""
    versions: list[int] = []
    for path in sorted(MIGRATION_DIR.glob("*.sql")):
        match = re.match(r"^(\d+)_", path.name)
        assert match is not None, f"unexpected migration filename: {path.name}"
        versions.append(int(match.group(1)))
    assert versions, "no migrations found"
    return sorted(versions)


def _markdown_section(text: str, heading: str) -> str:
    """Return the body of one `##` section of a markdown document."""
    pattern = re.compile(
        rf"^## {re.escape(heading)}\s*$(.*?)(?=^## |\Z)",
        re.MULTILINE | re.DOTALL,
    )
    matches = pattern.findall(text)
    assert len(matches) == 1, f"expected exactly one `## {heading}` section"
    return matches[0]


def _top_level_runtime_modules() -> list[str]:
    """Return the top-level module names under `rust/src/`."""
    modules: list[str] = []
    for path in sorted(RUNTIME_SRC.iterdir()):
        if path.name.startswith("."):
            continue
        if path.is_dir():
            modules.append(f"{path.name}/")
        elif path.suffix == ".rs":
            modules.append(path.name)
    return modules


def _registered_routes() -> list[str]:
    """Return every route path registered by the Axum router."""
    return ROUTE_RE.findall(_read(SERVER_MOD))


def _route_topology_paragraph(text: str) -> str:
    """Return the review index's route topology paragraph."""
    matches = re.findall(
        r"^Route topology[^\n]*:.*?(?=\n\n)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert len(matches) == 1, (
        "the review index must state exactly one route topology paragraph; "
        f"found {len(matches)}"
    )
    return matches[0]


def _wire_section(text: str) -> str:
    """Return the review index's wire/transcoding section."""
    matches = re.findall(
        r"^### \d+\. Wire codecs and transcoding\s*$(.*?)(?=^### |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert len(matches) == 1, "expected exactly one wire section in the review index"
    return matches[0]


def _wire_facade_modules() -> list[str]:
    """Return the root wire modules that only re-export the neutral kernel."""
    facades: list[str] = []
    for path in sorted(WIRE_DIR.glob("*.rs")):
        source = _read(path)
        if "pub use eggpool_wire::" not in source:
            continue
        body = [
            line.strip()
            for line in source.splitlines()
            if line.strip() and not line.strip().startswith("//")
        ]
        if body and all(line.startswith("pub use ") for line in body):
            facades.append(path.stem)
    return facades


def test_review_index_cli_census_matches_the_command_tree() -> None:
    """The review index's CLI count and list must match `rust/src/cli.rs`."""
    documented_count, documented = _documented_cli_commands(_read(OVERVIEW))
    actual = _cli_command_names(_read(CLI_SOURCE))

    assert documented, "the review index must list the CLI commands"
    assert len(documented) == len(set(documented)), (
        f"the review index repeats a CLI command: {documented}"
    )
    assert len(actual) == len(set(actual)), f"the command tree repeats a name: {actual}"
    assert sorted(documented) == sorted(actual), (
        "the review index CLI census does not match the command tree; "
        f"missing from the index: {sorted(set(actual) - set(documented))}; "
        f"documented but absent from the tree: {sorted(set(documented) - set(actual))}"
    )
    assert documented_count == len(documented), (
        f"the review index states {documented_count} commands but lists "
        f"{len(documented)}"
    )


def test_architecture_documents_state_the_current_schema_version() -> None:
    """A schema version named in prose must be the current one.

    A document that describes live behavior may not name an older schema as
    if it were the present one. The single sanctioned exception is a document
    that scopes the reference as the schema current when the investigation
    ran, and the investigation record itself, which is a historical snapshot.
    """
    versions = _migration_versions()
    assert versions, "the migration directory must not be empty"
    current = versions[-1]

    for document in sorted(ARCHITECTURE_DIR.glob("*.md")):
        if document.name == SCHEMA_INVESTIGATION:
            continue
        text = _read(document)
        if HISTORICAL_SCOPE_PHRASE in text:
            continue
        stated = {int(value) for value in SCHEMA_VERSION_RE.findall(text)}
        stale = sorted(version for version in stated if version != current)
        assert not stale, (
            f"{document.name} names schema version(s) {stale} while the "
            f"migration chain is current at {current}; a live-behaviour "
            "document must not present an older schema as the present one"
        )


def test_migration_census_matches_the_migration_directory() -> None:
    """The stated migration chain must match `rust/assets/db/migrations/`."""
    versions = _migration_versions()
    assert versions == list(range(1, len(versions) + 1)), (
        f"the migration chain must be contiguous from version 1; found {versions}"
    )
    highest = versions[-1]

    overview_matches = MIGRATION_CENSUS_RE.findall(_read(OVERVIEW))
    assert overview_matches, (
        "the review index must state the migration chain census as `v1-vN`"
    )
    assert {int(value) for value in overview_matches} == {highest}, (
        "the review index migration census does not match the migration "
        f"directory (highest version {highest}); "
        f"stated: {sorted(set(overview_matches))}"
    )

    layout = _markdown_section(_read(AGENTS), "Layout")
    agents_matches = MIGRATION_CENSUS_RE.findall(layout)
    assert agents_matches, (
        "the agent workflow index's layout section must state the migration "
        "chain census as `v1-vN`"
    )
    assert {int(value) for value in agents_matches} == {highest}, (
        "the agent workflow index migration census does not match the "
        f"migration directory (highest version {highest}); "
        f"stated: {sorted(set(agents_matches))}"
    )


def test_workflow_index_names_every_top_level_runtime_module() -> None:
    """Every top-level `rust/src/` module must be named in the layout section."""
    layout = _markdown_section(_read(AGENTS), "Layout")
    modules = _top_level_runtime_modules()
    assert modules, "no runtime modules found under rust/src/"

    missing = [module for module in modules if module not in layout]
    assert not missing, (
        "the agent workflow index's layout section does not name these "
        f"top-level runtime modules: {missing}"
    )


def test_route_topology_covers_every_registered_route() -> None:
    """Every registered route must be listed in the review index's topology."""
    registered = _registered_routes()
    assert registered, "no routes found in the Axum router"

    paragraph = _route_topology_paragraph(_read(OVERVIEW))
    documented = set(ROUTE_TOKEN_RE.findall(paragraph))
    assert documented, "the route topology paragraph lists no routes"

    def covered(path: str) -> bool:
        # A token covers a route when it is that exact route, or when it is an
        # explicit path-prefix group such as `/static/`. The root route `/` is
        # an exact route, never a prefix group.
        return any(
            path == token
            or (token.endswith("/") and len(token) > 1 and path.startswith(token))
            for token in documented
        )

    missing = sorted(path for path in registered if not covered(path))
    assert not missing, (
        "the review index's route topology does not cover these registered "
        f"routes: {missing}"
    )
    unknown = sorted(
        token
        for token in documented
        if token not in registered
        and not any(path.startswith(token) for path in registered)
    )
    assert not unknown, (
        f"the review index's route topology documents unregistered routes: {unknown}"
    )


def test_wire_kernel_ownership_is_not_credited_to_a_root_facade() -> None:
    """No root wire facade may be credited with kernel implementation ownership."""
    section = _wire_section(_read(OVERVIEW))
    facades = _wire_facade_modules()
    assert facades, "no root wire facades found"

    for sentence in re.split(r"(?<=[.!?])\s+", section):
        mentioned = sorted(
            {
                match.group(1)
                for match in FACADE_FILE_RE.finditer(sentence)
                if match.group(1) in facades
            }
        )
        if not mentioned:
            continue
        verb = OWNERSHIP_VERB_RE.search(sentence)
        assert verb is None, (
            "the review index credits the root wire facade(s) "
            f"{mentioned} with implementation ownership via '{verb.group(0)}'; "
            "the neutral kernel crate owns the canonical semantics"
        )

    assert "rust/crates/eggpool-wire/" in section, (
        "the review index must name the neutral kernel crate as the wire owner"
    )
    assert "wire::runtime.rs" in section and "wire::adapters.rs" in section, (
        "the review index must name the EggPool-owned wire seam and join"
    )
