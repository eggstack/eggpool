# ruff: noqa: F405
from __future__ import annotations

from ._shared import *  # noqa: F403


class _ProjectionParser(html.parser.HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.heading_parts: list[str] = []
        self.headings: list[str] = []
        self.nav_paths: list[str] = []
        self.active_nav: str | None = None
        self.form_stack: list[dict[str, Any]] = []
        self.forms: list[tuple[str, str, tuple[str, ...]]] = []
        self.ids: list[str] = []
        self.asset_paths: list[str] = []
        self.internal_links: list[str] = []
        self.unsafe_links: list[str] = []
        self.text: list[str] = []
        self.cards: list[dict[str, Any]] = []
        self.card_stack: list[dict[str, Any]] = []
        self.card_captures: list[tuple[str, str]] = []
        self.tables: list[dict[str, Any]] = []
        self.table_rows: list[dict[str, Any]] = []
        self.table_cells: list[list[str]] = []
        self.status_captures: list[tuple[str, list[str]]] = []
        self.status_messages: list[str] = []
        self._title_depth = 0
        self._title_seen = False
        self._heading_depth = 0
        self.root = DomNode("#document", (), ())
        self.node_stack: list[
            tuple[str, list[DomNode | str], tuple[tuple[str, str], ...]]
        ] = [("#document", [], ())]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        normalized_attrs = tuple(
            sorted(
                (
                    key,
                    " ".join(sorted(set((value or "").split())))
                    if key == "class"
                    else value or "",
                )
                for key, value in attrs
            )
        )
        node_children: list[DomNode | str] = []
        self.node_stack[-1][1].append(DomNode(tag, normalized_attrs, ()))
        # Keep a mutable child list on the stack and rebuild immutable nodes
        # when a parent closes; this retains every element, attribute and text.
        if tag not in VOID_ELEMENTS:
            self.node_stack.append((tag, node_children, normalized_attrs))
        if attributes.get("id"):
            self.ids.append(attributes["id"])
        if tag == "title" and not self._title_seen:
            self._title_depth = 1
        if tag in {"h1", "h2", "h3"}:
            self._heading_depth = 1
        if tag == "form":
            self.form_stack.append(
                {
                    "method": attributes.get("method", "get").lower(),
                    "action": attributes.get("action", ""),
                    "inputs": [],
                }
            )
        if (
            tag in {"div", "section", "article"}
            and "card" in attributes.get("class", "").split()
        ):
            self.card_stack.append({"heading": [], "metric": [], "close_tag": tag})
        if self.card_stack and tag == "h3":
            self.card_captures.append(("h3", "heading"))
        if (
            self.card_stack
            and tag == "p"
            and "metric" in attributes.get("class", "").split()
        ):
            self.card_captures.append(("p", "metric"))
        if tag == "table":
            self.tables.append({"headers": [], "rows": []})
        if self.tables and tag == "tr":
            self.table_rows.append({"cells": [], "header": False})
        if self.table_rows and tag in {"th", "td"}:
            self.table_cells.append([])
            self.table_rows[-1]["header"] = self.table_rows[-1]["header"] or tag == "th"
        if tag in {"p", "div", "section", "span"} and (
            "status" in attributes.get("class", "").split()
            or "empty" in attributes.get("class", "").split()
            or "empty-state" in attributes.get("class", "").split()
            or attributes.get("role") == "status"
        ):
            self.status_captures.append((tag, []))
        if self.form_stack and tag in {"input", "select", "textarea", "button"}:
            name = attributes.get("name", "")
            input_type = attributes.get("type", tag)
            self.form_stack[-1]["inputs"].append(f"{input_type}:{name}")
        if tag == "a" and attributes.get("href"):
            self._record_link(attributes["href"], attributes.get("class", ""))
        if tag in {"link", "script"}:
            resource = attributes.get("href") or attributes.get("src")
            if resource and resource.startswith("/static/"):
                self.asset_paths.append(urllib.parse.urlsplit(resource).path)

    def handle_endtag(self, tag: str) -> None:
        if len(self.node_stack) > 1 and self.node_stack[-1][0] == tag:
            node_tag, children, attrs = self.node_stack.pop()
            node = DomNode(node_tag, attrs, tuple(children))
            parent_children = self.node_stack[-1][1]
            if parent_children and isinstance(parent_children[-1], DomNode):
                parent_children[-1] = node
        if tag == "title":
            self._title_depth = 0
            self._title_seen = True
        if tag in {"h1", "h2", "h3"}:
            self.headings.append(_collapse_text(" ".join(self.heading_parts)))
            self.heading_parts.clear()
            self._heading_depth = 0
        if self.card_captures and tag == self.card_captures[-1][0]:
            self.card_captures.pop()
        if tag in {"th", "td"} and self.table_cells and self.table_rows:
            cell = _collapse_text(" ".join(self.table_cells.pop()))
            self.table_rows[-1]["cells"].append(cell)
        if tag == "tr" and self.table_rows:
            row = self.table_rows.pop()
            if self.tables:
                if row["header"]:
                    self.tables[-1]["headers"] = row["cells"]
                else:
                    self.tables[-1]["rows"].append(row["cells"])
        if tag == "table" and self.tables:
            # The active table is finalized when its end tag arrives. Nested
            # tables are not expected in the dashboard, but the stack remains
            # deterministic if a future page introduces one.
            table = self.tables[-1]
            table.setdefault("complete", True)
        if self.status_captures and tag == self.status_captures[-1][0]:
            _tag, parts = self.status_captures.pop()
            value = _collapse_text(" ".join(parts))
            if value:
                self.status_messages.append(value)
        if (
            tag in {"div", "section", "article"}
            and self.card_stack
            and self.card_stack[-1].get("close_tag") == tag
        ):
            # Only close a card element when it owns the matching class. The
            # parser has no parent tree, so card boundaries are tracked by a
            # sentinel added in handle_starttag.
            card = self.card_stack.pop()
            heading = _collapse_text(" ".join(card["heading"]))
            metric = _collapse_text(" ".join(card["metric"]))
            if heading:
                self.cards.append({"heading": heading, "metric": metric})
        if tag == "form" and self.form_stack:
            form = self.form_stack.pop()
            self.forms.append(
                (
                    str(form["method"]),
                    str(form["action"]),
                    tuple(sorted(form["inputs"])),
                )
            )

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.node_stack[-1][1].append(_collapse_text(data))
        if self._title_depth:
            self.title_parts.append(data)
        if self._heading_depth:
            self.heading_parts.append(data)
        if data.strip():
            self.text.append(_collapse_text(data))
        if self.card_captures and self.card_stack:
            self.card_stack[-1][self.card_captures[-1][1]].append(data)
        if self.table_cells:
            self.table_cells[-1].append(data)
        if self.status_captures:
            self.status_captures[-1][1].append(data)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = tuple(
            sorted(
                (
                    key,
                    " ".join(sorted(set((value or "").split())))
                    if key == "class"
                    else value or "",
                )
                for key, value in attrs
            )
        )
        self.node_stack[-1][1].append(DomNode(tag, attributes, ()))

    def _record_link(self, href: str, classes: str) -> None:
        parsed = urllib.parse.urlsplit(href)
        if parsed.scheme or parsed.netloc:
            self.unsafe_links.append(href)
            return
        if not href.startswith("/"):
            return
        path = parsed.path or "/"
        if path.startswith("/static/"):
            self.asset_paths.append(path)
        else:
            self.internal_links.append(path)
            if "active" in classes.split():
                self.active_nav = path


def _collapse_text(value: str) -> str:
    collapsed = " ".join(value.split())
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", collapsed):
        return "<volatile:local-timestamp>"
    return collapsed


def project_html(body: str) -> HtmlProjection:
    parser = _ProjectionParser()
    parser.feed(body)
    counts: dict[str, int] = {}
    for identifier in parser.ids:
        counts[identifier] = counts.get(identifier, 0) + 1
    while len(parser.node_stack) > 1:
        tag, children, attrs = parser.node_stack.pop()
        node = DomNode(tag, attrs, tuple(children))
        parser.node_stack[-1][1][-1] = node
    root_tag, root_children, root_attrs = parser.node_stack[0]
    tree = DomNode(root_tag, root_attrs, tuple(root_children))
    return HtmlProjection(
        title=_collapse_text(" ".join(parser.title_parts)),
        headings=tuple(item for item in parser.headings if item),
        nav_paths=tuple(dict.fromkeys(parser.internal_links)),
        active_nav=parser.active_nav,
        form_signatures=tuple(parser.forms),
        ids=tuple(sorted(parser.ids)),
        asset_paths=tuple(sorted(set(parser.asset_paths))),
        internal_links=tuple(sorted(set(parser.internal_links))),
        unsafe_links=tuple(parser.unsafe_links),
        duplicate_ids=tuple(sorted(key for key, count in counts.items() if count > 1)),
        text=tuple(parser.text),
        cards=tuple(
            (str(card["heading"]), str(card["metric"]))
            for card in parser.cards
            if card["heading"]
        ),
        tables=tuple(
            TableProjection(
                headers=tuple(str(header) for header in table["headers"]),
                rows=tuple(tuple(str(cell) for cell in row) for row in table["rows"]),
            )
            for table in parser.tables
            if table["headers"]
        ),
        status_messages=tuple(dict.fromkeys(parser.status_messages)),
        tree=tree,
    )


def project_json(body: str) -> str:
    """Canonicalize JSON objects while preserving array and displayed value order."""
    try:
        value = _normalize_volatile(json.loads(body))
    except json.JSONDecodeError as error:
        raise QualificationError("dashboard API returned invalid JSON") from error
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compare_json_projection(expected: str, actual: str, route: str) -> None:
    expected_value = project_json(expected)
    actual_value = project_json(actual)
    if expected_value != actual_value:
        raise AssertionError(f"dashboard JSON projection differs for {route}")


def compare_api_response(expected: HttpResult, actual: HttpResult, route: str) -> None:
    if expected.status != actual.status:
        raise AssertionError(
            f"dashboard API status differs for {route}: "
            f"{expected.status}/{actual.status}"
        )
    expected_type = expected.headers.get("content-type", "").split(";", 1)[0].casefold()
    actual_type = actual.headers.get("content-type", "").split(";", 1)[0].casefold()
    if expected_type != actual_type:
        raise AssertionError(f"dashboard API content type differs for {route}")
    if expected.status < 400 and expected_type == "application/json":
        compare_json_projection(expected.body, actual.body, route)


def _normalize_volatile(value: Any) -> Any:
    if isinstance(value, dict):
        value_dict = cast("dict[str, Any]", value)
        return {key: _normalize_volatile(item) for key, item in value_dict.items()}
    if isinstance(value, list):
        value_list = cast("list[Any]", value)
        return [_normalize_volatile(item) for item in value_list]
    if isinstance(value, str) and re.fullmatch(
        r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value
    ):
        return "<volatile:local-timestamp>"
    return value


def _normalize_runtime_projection(projection: dict[str, Any]) -> dict[str, Any]:
    volatile_cards = {
        "Server PID",
        "Uptime",
        "Python",
        "RSS memory",
        "Open FDs",
        "Active threads",
        "Load average",
        "Database",
        "WAL",
    }
    cards = cast("list[list[str]]", projection.get("cards", []))
    volatile_values: dict[str, str] = {}
    for card in cards:
        if card and card[0] in volatile_cards and len(card) > 1:
            placeholder = f"<volatile:{card[0].casefold().replace(' ', '-')}>"
            if card[1] != "—":
                volatile_values[card[1]] = placeholder
            card[1] = placeholder
    projection["text"] = [
        volatile_values.get(str(item), _normalize_runtime_text(str(item)))
        for item in cast("list[Any]", projection.get("text", []))
    ]
    projection["tables"] = _normalize_runtime_tables(
        cast("list[dict[str, Any]]", projection.get("tables", []))
    )
    projection["tree"] = _normalize_runtime_tree(
        cast("dict[str, Any]", projection["tree"])
    )
    return projection


def _normalize_runtime_tree(
    node: dict[str, Any], *, metric: bool = False
) -> dict[str, Any]:
    attrs = cast("list[list[str]]", node.get("attributes", []))
    is_metric = metric or any(
        key == "class" and "metric" in value.split() for key, value in attrs
    )
    children = cast("list[Any]", node.get("children", []))
    normalized: list[Any] = []
    for child in children:
        if isinstance(child, dict):
            normalized.append(
                _normalize_runtime_tree(cast("dict[str, Any]", child), metric=is_metric)
            )
        elif isinstance(child, str):
            normalized.append(
                "<volatile:runtime-metric>"
                if is_metric
                else _normalize_runtime_text(child)
            )
        else:
            normalized.append(child)
    node["children"] = normalized
    return node


def _normalize_runtime_tables(tables: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for table in tables:
        headers = cast("list[str]", table.get("headers", []))
        rows = cast("list[list[str]]", table.get("rows", []))
        for row in rows:
            for index, header in enumerate(headers):
                if index < len(row) and header == "Next run":
                    row[index] = "<volatile:countdown>"
                elif index < len(row):
                    row[index] = _normalize_runtime_text(row[index])
    return tables


def _normalize_runtime_text(value: str) -> str:
    if re.fullmatch(r"\d{5,}", value):
        return "<volatile:process-id>"
    value = re.sub(r"PPID \d+", "PPID <volatile:process-id>", value)
    value = re.sub(r"/[^\s]*/dashboard\.sqlite3", "<volatile:database-path>", value)
    value = re.sub(r"\bin \d+[smhd](?:\d+[smhd])?\b", "<volatile:countdown>", value)
    if re.fullmatch(r"\d+(?:\.\d+)? MB", value):
        return "<volatile:runtime-size>"
    if re.fullmatch(r"\d+\.\d+", value):
        return "<volatile:load-average>"
    if re.fullmatch(r"\d+\.\d+/core · \d+ CPUs", value):
        return "<volatile:load-per-core>"
    if re.fullmatch(r"\d+\.\d+\.\d+", value):
        return "<volatile:python-version>"
    if any(name in value for name in ("macOS-", "Linux-", "Windows-")):
        return "<volatile:host-platform>"
    return value


def compare_dom_projection(
    expected: HtmlProjection, actual: HtmlProjection, route: str
) -> None:
    """Compare the complete canonical tree, retaining all meaningful DOM facts."""
    expected_tree = expected.tree
    actual_tree = actual.tree
    if route == "/runtime":
        expected_tree = _normalize_runtime_dom_tree(expected_tree)
        actual_tree = _normalize_runtime_dom_tree(actual_tree)
    if expected_tree != actual_tree:
        difference = _first_tree_difference(expected_tree, actual_tree)
        raise AssertionError(f"complete DOM tree differs for {route} at {difference}")
    if expected.duplicate_ids or actual.duplicate_ids:
        raise AssertionError(f"duplicate IDs in oracle/candidate for {route}")
    if expected.unsafe_links or actual.unsafe_links:
        raise AssertionError(f"unsafe links in oracle/candidate for {route}")


def _normalize_runtime_dom_tree(
    node: DomNode | str, *, metric: bool = False
) -> DomNode | str:
    """Apply the bounded Runtime volatility rules used for frozen captures."""
    if isinstance(node, str):
        return "<volatile:runtime-metric>" if metric else _normalize_runtime_text(node)
    is_metric = metric or any(
        key == "class" and "metric" in value.split() for key, value in node.attributes
    )
    return DomNode(
        node.tag,
        node.attributes,
        tuple(
            _normalize_runtime_dom_tree(child, metric=is_metric)
            for child in node.children
        ),
    )


def _page_shell_tree(node: DomNode | str) -> DomNode | str:
    if isinstance(node, str):
        return node
    if node.tag == "main" and dict(node.attributes).get("id") == "dashboard-content":
        return DomNode(node.tag, node.attributes, ())
    return DomNode(
        node.tag,
        node.attributes,
        tuple(_page_shell_tree(child) for child in node.children),
    )


def compare_shared_shell_projection(
    expected: HtmlProjection, actual: HtmlProjection, route: str
) -> None:
    """Compare the shell while leaving page-owned content to its milestone."""
    expected_tree = _page_shell_tree(expected.tree)
    actual_tree = _page_shell_tree(actual.tree)
    if expected_tree != actual_tree:
        difference = _first_tree_difference(expected_tree, actual_tree)
        raise AssertionError(f"shared shell differs for {route} at {difference}")
    if expected.duplicate_ids or actual.duplicate_ids:
        raise AssertionError(f"duplicate IDs in oracle/candidate shell for {route}")
    if expected.unsafe_links or actual.unsafe_links:
        raise AssertionError(f"unsafe links in oracle/candidate shell for {route}")


def _first_tree_difference(expected: DomNode | str, actual: DomNode | str) -> str:
    def visit(left: DomNode | str, right: DomNode | str, path: str) -> str | None:
        if isinstance(left, str) or isinstance(right, str):
            return (
                None
                if left == right
                else (
                    f"{path} (text lengths {len(str(left))}/{len(str(right))}: "
                    f"{left!r} != {right!r})"
                    if "/footer[" in path
                    else f"{path} (text lengths {len(str(left))}/{len(str(right))})"
                )
            )
        if left.tag != right.tag:
            return f"{path} (element type)"
        if left.attributes != right.attributes:
            left_attributes = dict(left.attributes)
            right_attributes = dict(right.attributes)
            changed = sorted(
                key
                for key in left_attributes.keys() | right_attributes.keys()
                if left_attributes.get(key) != right_attributes.get(key)
            )
            return f"{path} (attributes: {', '.join(changed)})"
        if len(left.children) != len(right.children):
            left_tags = [
                child.tag if isinstance(child, DomNode) else "#text"
                for child in left.children
            ]
            right_tags = [
                child.tag if isinstance(child, DomNode) else "#text"
                for child in right.children
            ]
            return f"{path} (children: {left_tags} != {right_tags})"
        for index, (left_child, right_child) in enumerate(
            zip(left.children, right.children, strict=True)
        ):
            difference = visit(left_child, right_child, f"{path}/{left.tag}[{index}]")
            if difference is not None:
                return difference
        return None

    return visit(expected, actual, "$") or "unknown structural difference"


__all__ = [
    "_ProjectionParser",
    "_collapse_text",
    "_first_tree_difference",
    "_normalize_runtime_dom_tree",
    "_normalize_runtime_projection",
    "_normalize_runtime_tables",
    "_normalize_runtime_text",
    "_normalize_runtime_tree",
    "_normalize_volatile",
    "_page_shell_tree",
    "compare_api_response",
    "compare_dom_projection",
    "compare_json_projection",
    "compare_shared_shell_projection",
    "project_html",
    "project_json",
]
