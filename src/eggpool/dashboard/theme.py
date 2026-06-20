"""Halloy TOML theme to dashboard CSS variable translation layer.

Parses halloy IRC theme files and translates their color definitions into
CSS custom properties for the dashboard. The halloy theme format uses
sections like [general], [text], [buffer], [buttons.*] which map
naturally to dashboard UI elements.
"""

from __future__ import annotations

import colorsys
import tomllib
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert '#RRGGBB' or '#RRGGBBAA' to (r, g, b)."""
    h = hex_color.lstrip("#")
    if len(h) == 8:
        h = h[:6]
    if len(h) != 6:
        raise ValueError(f"Invalid hex color: {hex_color}")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convert (r, g, b) to '#rrggbb'."""
    return f"#{r:02x}{g:02x}{b:02x}"


def _adjust_lightness(hex_color: str, factor: float) -> str:
    """Adjust the lightness of a hex color. factor > 1 = lighter, < 1 = darker."""
    r, g, b = _hex_to_rgb(hex_color)
    h, lightness, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
    lightness = max(0.0, min(1.0, lightness * factor))
    r2, g2, b2 = colorsys.hls_to_rgb(h, lightness, s)
    return _rgb_to_hex(int(r2 * 255), int(g2 * 255), int(b2 * 255))


def _mix_with(hex_color: str, target: str, ratio: float) -> str:
    """Mix hex_color toward target by ratio (0.0 = no change, 1.0 = fully target)."""
    r1, g1, b1 = _hex_to_rgb(hex_color)
    r2, g2, b2 = _hex_to_rgb(target)
    r = int(r1 + (r2 - r1) * ratio)
    g = int(g1 + (g2 - g1) * ratio)
    b = int(b1 + (b2 - b1) * ratio)
    return _rgb_to_hex(r, g, b)


# ---------------------------------------------------------------------------
# Halloy TOML structure models
# ---------------------------------------------------------------------------


class HalloyGeneral(BaseModel):
    model_config = ConfigDict(extra="ignore")

    background: str = "#1e1e2e"
    border: str = "#45475a"
    horizontal_rule: str = "#313244"
    unread_indicator: str = "#cba6f7"


class HalloyText(BaseModel):
    model_config = ConfigDict(extra="ignore")

    primary: str = "#cdd6f4"
    secondary: str = "#a6adc8"
    tertiary: str = "#cba6f7"
    success: str = "#a6e3a1"
    error: str = "#f38ba8"


class HalloyButtons(BaseModel):
    model_config = ConfigDict(extra="ignore")

    background: str = "#1e1e2e"
    background_hover: str = "#181825"
    background_selected: str = "#313244"
    background_selected_hover: str = "#45475a"


class HalloyBuffer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str = "#fab387"
    background: str = "#1e1e2e"
    background_text_input: str = "#181825"
    background_title_bar: str = "#181825"
    border: str = "#11111b"
    border_selected: str = "#b4befe"
    code: str = "#b4befe"
    highlight: str = "#45475a"
    nickname: str = "#89dceb"
    selection: str = "#313244"
    timestamp: str = "#bac2de"
    topic: str = "#7f849c"
    url: str = "#89b4fa"


def _str_dict(raw: dict[str, Any] | None) -> dict[str, str]:
    """Convert a dict with Any values to dict[str, str]."""
    if not raw:
        return {}
    result: dict[str, str] = {}
    for key, val in raw.items():
        result[str(key)] = str(val)
    return result


def _extract_buttons(raw_buttons: Any) -> dict[str, HalloyButtons]:
    """Extract button sections from raw TOML data."""
    if not isinstance(raw_buttons, dict):
        return {}
    typed = cast("dict[str, Any]", raw_buttons)
    buttons_map: dict[str, HalloyButtons] = {}
    for section_name, section_val in typed.items():
        if isinstance(section_val, dict):
            btn_fields = _str_dict(cast("dict[str, Any]", section_val))
            buttons_map[str(section_name)] = HalloyButtons(**btn_fields)
    return buttons_map


def _extract_buffer(raw_buffer: Any) -> dict[str, str]:
    """Extract buffer fields, excluding server_messages."""
    if not isinstance(raw_buffer, dict):
        return {}
    typed = cast("dict[str, Any]", raw_buffer)
    cleaned: dict[str, str] = {}
    for key, val in typed.items():
        if key != "server_messages":
            cleaned[str(key)] = str(val)
    return cleaned


class HalloyTheme(BaseModel):
    """Parsed halloy TOML theme file."""

    model_config = ConfigDict(extra="ignore")

    general: HalloyGeneral = Field(default_factory=HalloyGeneral)
    text: HalloyText = Field(default_factory=HalloyText)
    buttons: dict[str, HalloyButtons] = Field(default_factory=dict)
    buffer: HalloyBuffer = Field(default_factory=HalloyBuffer)

    @classmethod
    def from_toml(cls, path: str | Path | Traversable) -> HalloyTheme:
        """Load a halloy theme from a TOML file."""
        if isinstance(path, Path):
            with path.open("rb") as f:
                raw = tomllib.load(f)
            return cls._from_dict(raw)

        if isinstance(path, Traversable):
            with path.open("rb") as f:
                raw = tomllib.load(f)
            return cls._from_dict(raw)

        p = Path(path)
        with p.open("rb") as f:
            raw = tomllib.load(f)
        return cls._from_dict(raw)

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> HalloyTheme:
        """Construct from a parsed TOML dict, handling nested buffer.server_messages."""
        return cls(
            general=HalloyGeneral(**_str_dict(data.get("general"))),
            text=HalloyText(**_str_dict(data.get("text"))),
            buttons=_extract_buttons(data.get("buttons")),
            buffer=HalloyBuffer(**_extract_buffer(data.get("buffer"))),
        )


# ---------------------------------------------------------------------------
# Dashboard CSS theme (translated from halloy)
# ---------------------------------------------------------------------------


class DashboardTheme(BaseModel):
    """CSS custom properties derived from a halloy theme."""

    model_config = ConfigDict(extra="forbid")

    # Page
    page_bg: str = "#f6f7f9"
    page_text: str = "#1f2328"
    page_border: str = "#d0d7de"

    # Topbar
    topbar_bg: str = "#1f2328"
    topbar_text: str = "#f6f7f9"
    topbar_border: str = "#2d333b"

    # Navigation
    nav_text: str = "#c9d1d9"
    nav_hover_bg: str = "#2d333b"
    nav_active_bg: str = "#444c56"
    nav_active_text: str = "#ffffff"

    # Cards and panels
    card_bg: str = "#ffffff"
    card_border: str = "#d0d7de"

    # Tables
    table_header_bg: str = "#f6f8fa"
    table_header_text: str = "#57606a"
    table_border: str = "#eaeef2"

    # Text emphasis
    text_muted: str = "#57606a"
    text_secondary: str = "#6e7681"

    # Status / semantic
    color_success: str = "#1a7f37"
    color_error: str = "#cf222e"
    color_warning: str = "#9a6700"
    color_info: str = "#0969da"

    # Buttons
    button_primary_bg: str = "#1f2328"
    button_primary_text: str = "#ffffff"

    # Event tags
    tag_default_bg: str = "#ddf4ff"
    tag_default_text: str = "#0969da"
    tag_success_bg: str = "#dafbe1"
    tag_success_text: str = "#1a7f37"
    tag_warning_bg: str = "#fff8c5"
    tag_warning_text: str = "#9a6700"
    tag_error_bg: str = "#ffebe9"
    tag_error_text: str = "#cf222e"

    # Heatmap (5-level scale)
    heatmap_0: str = "#ebedf0"
    heatmap_1: str = "#9be9a8"
    heatmap_2: str = "#40c463"
    heatmap_3: str = "#30a14e"
    heatmap_4: str = "#216e39"

    # Heatmap text
    heatmap_label_text: str = "#57606a"

    def to_css_variables(self) -> str:
        """Generate a CSS :root { ... } block with all theme variables."""
        props = [
            ("--page-bg", self.page_bg),
            ("--page-text", self.page_text),
            ("--page-border", self.page_border),
            ("--topbar-bg", self.topbar_bg),
            ("--topbar-text", self.topbar_text),
            ("--topbar-border", self.topbar_border),
            ("--nav-text", self.nav_text),
            ("--nav-hover-bg", self.nav_hover_bg),
            ("--nav-active-bg", self.nav_active_bg),
            ("--nav-active-text", self.nav_active_text),
            ("--card-bg", self.card_bg),
            ("--card-border", self.card_border),
            ("--table-header-bg", self.table_header_bg),
            ("--table-header-text", self.table_header_text),
            ("--table-border", self.table_border),
            ("--text-muted", self.text_muted),
            ("--text-secondary", self.text_secondary),
            ("--color-success", self.color_success),
            ("--color-error", self.color_error),
            ("--color-warning", self.color_warning),
            ("--color-info", self.color_info),
            ("--button-primary-bg", self.button_primary_bg),
            ("--button-primary-text", self.button_primary_text),
            ("--tag-default-bg", self.tag_default_bg),
            ("--tag-default-text", self.tag_default_text),
            ("--tag-success-bg", self.tag_success_bg),
            ("--tag-success-text", self.tag_success_text),
            ("--tag-warning-bg", self.tag_warning_bg),
            ("--tag-warning-text", self.tag_warning_text),
            ("--tag-error-bg", self.tag_error_bg),
            ("--tag-error-text", self.tag_error_text),
            ("--heatmap-0", self.heatmap_0),
            ("--heatmap-1", self.heatmap_1),
            ("--heatmap-2", self.heatmap_2),
            ("--heatmap-3", self.heatmap_3),
            ("--heatmap-4", self.heatmap_4),
            ("--heatmap-label-text", self.heatmap_label_text),
        ]
        lines = [f"  {name}: {value};" for name, value in props]
        return ":root {\n" + "\n".join(lines) + "\n}"

    def heatmap_colors(self) -> list[str]:
        """Return the 5-level heatmap color scale."""
        return [
            self.heatmap_0,
            self.heatmap_1,
            self.heatmap_2,
            self.heatmap_3,
            self.heatmap_4,
        ]


# ---------------------------------------------------------------------------
# Translation: halloy -> dashboard CSS variables
# ---------------------------------------------------------------------------


def translate_theme(halloy: HalloyTheme) -> DashboardTheme:
    """Translate a halloy theme into dashboard CSS variables."""
    bg = halloy.general.background
    text_primary = halloy.text.primary
    text_secondary = halloy.text.secondary
    text_success = halloy.text.success
    text_error = halloy.text.error
    buf = halloy.buffer

    # Determine if this is a light or dark theme by checking background lightness
    bg_r, bg_g, bg_b = _hex_to_rgb(bg)
    _, bg_lightness, _ = colorsys.rgb_to_hls(bg_r / 255, bg_g / 255, bg_b / 255)
    is_dark = bg_lightness < 0.5

    # Page background: use halloy's general.background (dark themes) or
    # buffer.background (light themes where general.background is the chrome)
    page_bg = buf.background if is_dark else halloy.general.background

    # Topbar: use halloy's dark chrome colors
    topbar_bg = halloy.general.background
    topbar_text = text_primary
    topbar_border = halloy.general.border

    # Navigation: derived from buffer title bar
    nav_text = text_secondary
    nav_hover_bg = buf.highlight
    nav_active_bg = (
        halloy.buttons.get("primary", HalloyButtons()).background_selected
        or buf.background_title_bar
    )
    nav_active_text = text_primary

    # Cards and panels: use buffer background (the content area)
    card_bg = buf.background
    card_border = halloy.general.border

    # Tables
    table_header_bg = buf.background_title_bar
    table_header_text = text_secondary
    table_border = halloy.general.horizontal_rule

    # Text emphasis
    text_muted = buf.topic if buf.topic != text_primary else text_secondary
    text_secondary_color = text_secondary

    # Status colors map directly
    color_success = text_success
    color_error = text_error

    # Warning: use buffer action color (typically amber/orange)
    color_warning = buf.action

    # Info: use halloy's url or tertiary color
    color_info = buf.url

    # Buttons
    primary_btn = halloy.buttons.get("primary", HalloyButtons())
    button_primary_bg = (
        primary_btn.background_selected
        or primary_btn.background
        or halloy.general.background
    )
    button_primary_text = text_primary

    # Event tags: derive from theme colors with alpha blending
    tag_default_bg = _mix_with(page_bg, color_info, 0.15)
    tag_default_text = color_info
    tag_success_bg = _mix_with(page_bg, color_success, 0.15)
    tag_success_text = color_success
    tag_warning_bg = _mix_with(page_bg, color_warning, 0.15)
    tag_warning_text = color_warning
    tag_error_bg = _mix_with(page_bg, color_error, 0.15)
    tag_error_text = color_error

    # Heatmap: derive 5-level scale from success color
    heatmap_0 = _mix_with(page_bg, text_primary, 0.06)
    heatmap_1 = _mix_with(page_bg, text_success, 0.35)
    heatmap_2 = text_success
    heatmap_3 = _adjust_lightness(text_success, 0.7)
    heatmap_4 = _adjust_lightness(text_success, 0.45)

    # Heatmap label text
    heatmap_label_text = text_muted

    return DashboardTheme(
        page_bg=page_bg,
        page_text=text_primary,
        page_border=halloy.general.border,
        topbar_bg=topbar_bg,
        topbar_text=topbar_text,
        topbar_border=topbar_border,
        nav_text=nav_text,
        nav_hover_bg=nav_hover_bg,
        nav_active_bg=nav_active_bg,
        nav_active_text=nav_active_text,
        card_bg=card_bg,
        card_border=card_border,
        table_header_bg=table_header_bg,
        table_header_text=table_header_text,
        table_border=table_border,
        text_muted=text_muted,
        text_secondary=text_secondary_color,
        color_success=color_success,
        color_error=color_error,
        color_warning=color_warning,
        color_info=color_info,
        button_primary_bg=button_primary_bg,
        button_primary_text=button_primary_text,
        tag_default_bg=tag_default_bg,
        tag_default_text=tag_default_text,
        tag_success_bg=tag_success_bg,
        tag_success_text=tag_success_text,
        tag_warning_bg=tag_warning_bg,
        tag_warning_text=tag_warning_text,
        tag_error_bg=tag_error_bg,
        tag_error_text=tag_error_text,
        heatmap_0=heatmap_0,
        heatmap_1=heatmap_1,
        heatmap_2=heatmap_2,
        heatmap_3=heatmap_3,
        heatmap_4=heatmap_4,
        heatmap_label_text=heatmap_label_text,
    )


def load_theme(theme_path: str | Path | Traversable) -> DashboardTheme:
    """Load a halloy TOML theme and translate it to dashboard CSS variables."""
    halloy = HalloyTheme.from_toml(theme_path)
    return translate_theme(halloy)


def get_default_theme() -> DashboardTheme:
    """Return the default (GitHub Primer-inspired) theme."""
    return DashboardTheme()


def list_themes(themes_dir: str | Path | None = None) -> list[str]:
    """List available theme names, merging bundled and user themes.

    When themes_dir is set, user-provided themes take precedence over
    bundled themes with the same name.  If themes_dir is None, only
    bundled themes are returned.
    """
    from eggpool.dashboard._resources import bundled_themes_dir

    bundled = bundled_themes_dir()
    bundled_names: set[str] = {
        Path(f.name).stem
        for f in bundled.iterdir()
        if f.is_file() and f.name.endswith(".toml")
    }

    if themes_dir is None:
        return sorted(bundled_names)

    user = Path(themes_dir)
    if not user.is_dir():
        return sorted(bundled_names)
    user_names = {f.stem for f in user.iterdir() if f.suffix == ".toml" and f.is_file()}
    return sorted(bundled_names | user_names)


def resolve_theme_path(
    theme_name: str, themes_dir: str | Path | None = None
) -> Traversable | None:
    """Resolve a theme name to its .toml file path.

    When themes_dir is set, the user directory is checked first (allowing
    overrides), falling back to the bundled themes directory.  Returns
    ``None`` when no matching file is found.
    """
    from eggpool.dashboard._resources import bundled_themes_dir

    if themes_dir is not None:
        user = Path(themes_dir) / f"{theme_name}.toml"
        if user.is_file():
            return user

    bundled = bundled_themes_dir().joinpath(f"{theme_name}.toml")
    if bundled.is_file():
        return bundled
    return None
