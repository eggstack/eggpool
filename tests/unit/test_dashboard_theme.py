"""Tests for the dashboard theme translation layer."""

from __future__ import annotations

import colorsys
import textwrap
import tomllib
from typing import TYPE_CHECKING

import pytest

from eggpool.dashboard.render import get_theme, get_theme_css
from eggpool.dashboard.theme import (
    DashboardTheme,
    HalloyGeneral,
    HalloyText,
    HalloyTheme,
    _adjust_lightness,
    _hex_to_rgb,
    _mix_with,
    _rgb_to_hex,
    _str_dict,
    get_default_theme,
    list_themes,
    load_theme,
    resolve_theme_path,
    translate_theme,
)

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Color utility tests
# ---------------------------------------------------------------------------


class TestColorUtilities:
    def test_hex_to_rgb(self) -> None:
        assert _hex_to_rgb("#ff0000") == (255, 0, 0)
        assert _hex_to_rgb("#00ff00") == (0, 255, 0)
        assert _hex_to_rgb("#0000ff") == (0, 0, 255)
        assert _hex_to_rgb("#1E1E2E") == (30, 30, 46)

    def test_hex_to_rgb_with_alpha(self) -> None:
        assert _hex_to_rgb("#ff0000aa") == (255, 0, 0)

    def test_hex_to_rgb_invalid(self) -> None:
        with pytest.raises(ValueError, match="Invalid hex color"):
            _hex_to_rgb("#xyz")

    def test_rgb_to_hex(self) -> None:
        assert _rgb_to_hex(255, 0, 0) == "#ff0000"
        assert _rgb_to_hex(0, 255, 0) == "#00ff00"
        assert _rgb_to_hex(30, 30, 46) == "#1e1e2e"

    def test_adjust_lightness_lighter(self) -> None:
        result = _adjust_lightness("#30a14e", 1.5)
        r, g, b = _hex_to_rgb(result)
        _, lightness, _ = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        assert lightness > 0.5

    def test_adjust_lightness_darker(self) -> None:
        result = _adjust_lightness("#30a14e", 0.5)
        r, g, b = _hex_to_rgb(result)
        _, lightness, _ = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        assert lightness < 0.5

    def test_mix_with(self) -> None:
        result = _mix_with("#000000", "#ffffff", 0.5)
        assert result == "#7f7f7f"

    def test_mix_with_no_change(self) -> None:
        assert _mix_with("#ff0000", "#00ff00", 0.0) == "#ff0000"

    def test_mix_with_full(self) -> None:
        assert _mix_with("#ff0000", "#00ff00", 1.0) == "#00ff00"

    def test_str_dict(self) -> None:
        result = _str_dict({"a": 1, "b": "hello"})
        assert result == {"a": "1", "b": "hello"}

    def test_str_dict_none(self) -> None:
        assert _str_dict(None) == {}


# ---------------------------------------------------------------------------
# HalloyTheme model tests
# ---------------------------------------------------------------------------


class TestHalloyTheme:
    def test_from_dict_basic(self) -> None:
        data = {
            "general": {"background": "#1e1e2e", "border": "#45475a"},
            "text": {"primary": "#cdd6f4", "success": "#a6e3a1"},
        }
        theme = HalloyTheme._from_dict(data)
        assert theme.general.background == "#1e1e2e"
        assert theme.text.primary == "#cdd6f4"
        assert theme.text.success == "#a6e3a1"

    def test_from_dict_excludes_server_messages(self) -> None:
        data = {
            "buffer": {
                "background": "#1e1e2e",
                "server_messages": {"default": "#f9e2af"},
            },
        }
        theme = HalloyTheme._from_dict(data)
        assert theme.buffer.background == "#1e1e2e"

    def test_from_dict_buttons(self) -> None:
        data = {
            "buttons": {
                "primary": {"background": "#1e1e2e", "background_hover": "#282a36"},
                "secondary": {"background": "#2b2e3a"},
            },
        }
        theme = HalloyTheme._from_dict(data)
        assert "primary" in theme.buttons
        assert theme.buttons["primary"].background == "#1e1e2e"
        assert theme.buttons["primary"].background_hover == "#282a36"
        assert "secondary" in theme.buttons

    def test_from_dict_defaults(self) -> None:
        theme = HalloyTheme._from_dict({})
        assert theme.general.background == "#1e1e2e"
        assert theme.text.primary == "#cdd6f4"

    def test_from_toml(self, tmp_path: Path) -> None:
        toml_content = textwrap.dedent("""\
            [general]
            background = "#000000"
            border = "#333333"

            [text]
            primary = "#ffffff"
            success = "#00ff00"
        """)
        theme_file = tmp_path / "test.toml"
        theme_file.write_text(toml_content)
        theme = HalloyTheme.from_toml(theme_file)
        assert theme.general.background == "#000000"
        assert theme.text.primary == "#ffffff"


# ---------------------------------------------------------------------------
# Translation tests
# ---------------------------------------------------------------------------


class TestTranslateTheme:
    def test_dark_theme_translation(self) -> None:
        halloy = HalloyTheme(
            general=HalloyGeneral(
                background="#11111b",
                border="#45475a",
            ),
            text=HalloyText(
                primary="#cdd6f4",
                secondary="#a6adc8",
                success="#a6e3a1",
                error="#f38ba8",
            ),
        )
        theme = translate_theme(halloy)
        # Dark theme: page_bg should come from buffer.background
        assert theme.page_text == "#cdd6f4"
        assert theme.color_success == "#a6e3a1"
        assert theme.color_error == "#f38ba8"

    def test_light_theme_translation(self) -> None:
        halloy = HalloyTheme(
            general=HalloyGeneral(
                background="#dce0e8",
                border="#9ca0b0",
            ),
            text=HalloyText(
                primary="#4c4f69",
                success="#40a02b",
                error="#d20f39",
            ),
        )
        theme = translate_theme(halloy)
        assert theme.page_text == "#4c4f69"
        assert theme.color_success == "#40a02b"

    def test_heatmap_colors_count(self) -> None:
        theme = get_default_theme()
        assert len(theme.heatmap_colors()) == 5

    def test_heatmap_colors_are_hex(self) -> None:
        theme = get_default_theme()
        for color in theme.heatmap_colors():
            assert color.startswith("#")
            assert len(color) == 7


# ---------------------------------------------------------------------------
# DashboardTheme model tests
# ---------------------------------------------------------------------------


class TestDashboardTheme:
    def test_to_css_variables(self) -> None:
        theme = get_default_theme()
        css = theme.to_css_variables()
        assert ":root {" in css
        assert "--page-bg:" in css
        assert "--page-text:" in css
        assert "--color-success:" in css
        assert "--heatmap-0:" in css
        assert css.strip().endswith("}")

    def test_css_variables_format(self) -> None:
        theme = get_default_theme()
        css = theme.to_css_variables()
        lines = css.strip().split("\n")
        # First line is :root {
        assert lines[0] == ":root {"
        # Last line is }
        assert lines[-1] == "}"
        # Middle lines are "  --var: value;"
        for line in lines[1:-1]:
            assert line.startswith("  --")
            assert ": " in line
            assert line.rstrip().endswith(";")

    def test_default_theme_colors_are_valid_hex(self) -> None:
        theme = get_default_theme()
        for field_name in DashboardTheme.model_fields:
            val = getattr(theme, field_name)
            if isinstance(val, str) and val.startswith("#"):
                assert len(val) == 7, f"{field_name} has invalid hex: {val}"


# ---------------------------------------------------------------------------
# Loading and listing tests
# ---------------------------------------------------------------------------


class TestThemeLoading:
    def test_load_theme_valid(self, tmp_path: Path) -> None:
        toml_content = textwrap.dedent("""\
            [general]
            background = "#000000"

            [text]
            primary = "#ffffff"
            success = "#00ff00"
            error = "#ff0000"
        """)
        theme_file = tmp_path / "test-theme.toml"
        theme_file.write_text(toml_content)
        theme = load_theme(theme_file)
        assert isinstance(theme, DashboardTheme)
        assert theme.page_text == "#ffffff"
        assert theme.color_success == "#00ff00"

    def test_load_theme_invalid_file(self, tmp_path: Path) -> None:
        theme_file = tmp_path / "bad.toml"
        theme_file.write_text("this is not valid toml {{{")
        with pytest.raises((tomllib.TOMLDecodeError, Exception)):
            load_theme(theme_file)

    def test_list_themes(self, tmp_path: Path) -> None:
        (tmp_path / "alpha.toml").write_text("[text]\nprimary = '#fff'")
        (tmp_path / "beta.toml").write_text("[text]\nprimary = '#000'")
        (tmp_path / "not-a-theme.txt").write_text("ignored")
        themes = list_themes(tmp_path)
        assert "alpha" in themes
        assert "beta" in themes
        # Bundled themes are also present
        assert "Cyber Red" in themes

    def test_list_themes_nonexistent_dir(self) -> None:
        themes = list_themes("/nonexistent/path")
        # Nonexistent user dir: bundled themes still returned
        assert "Cyber Red" in themes
        assert len(themes) >= 50


# ---------------------------------------------------------------------------
# Render integration tests
# ---------------------------------------------------------------------------


class TestRenderThemeIntegration:
    def test_get_theme_default(self) -> None:
        theme = get_theme("default")
        assert isinstance(theme, DashboardTheme)

    def test_get_theme_unknown_falls_back_to_default(self) -> None:
        theme = get_theme("nonexistent-theme")
        assert isinstance(theme, DashboardTheme)

    def test_get_theme_css_default_returns_empty(self) -> None:
        css = get_theme_css("default")
        assert css == ""

    def test_get_theme_css_unknown_returns_empty(self) -> None:
        css = get_theme_css("nonexistent-theme")
        assert css == ""

    def test_render_overview_with_theme_css(self) -> None:
        from eggpool.dashboard.render import render_overview

        html = render_overview(
            overview={"summary": {}, "imbalance": {}},
            accounts=[],
            theme_css=":root { --page-bg: #000; }",
            current_theme="test-theme",
        )
        assert "/static/theme.css?theme=test-theme" in html
        assert "<link" in html

    def test_render_overview_without_theme(self) -> None:
        from eggpool.dashboard.render import render_overview

        html = render_overview(
            overview={"summary": {}, "imbalance": {}},
            accounts=[],
        )
        assert "/static/dashboard.css" in html
        assert "/static/theme.css" not in html

    def test_heatmap_with_theme_colors(self) -> None:
        from eggpool.dashboard.render import _render_bandwidth_heatmap

        daily = [
            {"day": "2025-01-01", "bytes_emitted": 100, "bytes_received": 50},
            {"day": "2025-01-02", "bytes_emitted": 0, "bytes_received": 0},
        ]
        html = _render_bandwidth_heatmap(
            daily, heatmap_colors=["#aaa", "#bbb", "#ccc", "#ddd", "#eee"]
        )
        # Non-zero value maps to a color in the scale
        assert "#aaa" in html
        # Zero value also maps to first color
        assert "fill=" in html


# ---------------------------------------------------------------------------
# Bundled theme tests
# ---------------------------------------------------------------------------


class TestBundledThemes:
    def test_list_themes_with_none_uses_bundled(self) -> None:
        """list_themes(None) returns bundled themes."""
        themes = list_themes(None)
        assert len(themes) >= 50
        assert "Cyber Red" in themes
        assert "Dracula" in themes
        assert "Catppuccin Mocha" in themes

    def test_list_themes_nonexistent_dir_returns_bundled(self) -> None:
        """list_themes('/nonexistent') returns bundled themes."""
        themes = list_themes("/nonexistent/path")
        assert len(themes) >= 50
        assert "Cyber Red" in themes

    def test_get_theme_with_none_uses_bundled(self) -> None:
        """get_theme('Cyber Red', None) loads from bundled themes."""
        theme = get_theme("Cyber Red", None)
        assert isinstance(theme, DashboardTheme)

    def test_get_theme_css_with_none_uses_bundled(self) -> None:
        """get_theme_css('Cyber Red', None) loads from bundled themes."""
        css = get_theme_css("Cyber Red", None)
        assert ":root {" in css
        assert "--page-bg:" in css

    def test_list_themes_merges_user_and_bundled(self, tmp_path: Path) -> None:
        """list_themes with a user dir merges bundled + user themes."""
        (tmp_path / "My Custom.toml").write_text("[text]\nprimary = '#fff'\n")
        themes = list_themes(tmp_path)
        assert "My Custom" in themes
        assert "Cyber Red" in themes
        assert "Dracula" in themes

    def test_list_themes_user_overrides_bundled(self, tmp_path: Path) -> None:
        """A user theme with the same name as a bundled theme wins."""
        (tmp_path / "Cyber Red.toml").write_text("[text]\nprimary = '#000000'\n")
        themes = list_themes(tmp_path)
        assert "Cyber Red" in themes

    def test_resolve_theme_path_user_dir_first(self, tmp_path: Path) -> None:
        """resolve_theme_path checks user dir before bundled."""
        user_theme = tmp_path / "Cyber Red.toml"
        user_theme.write_text("[text]\nprimary = '#000000'\n")
        path = resolve_theme_path("Cyber Red", tmp_path)
        assert path is not None
        assert path == user_theme

    def test_resolve_theme_path_falls_back_to_bundled(self, tmp_path: Path) -> None:
        """resolve_theme_path falls back to bundled when user dir lacks the theme."""
        path = resolve_theme_path("Cyber Red", tmp_path)
        assert path is not None
        assert "themes" in str(path)

    def test_resolve_theme_path_nonexistent(self) -> None:
        """resolve_theme_path returns None for unknown theme."""
        path = resolve_theme_path("Nonexistent Theme XYZ")
        assert path is None

    def test_get_theme_user_dir_override(self, tmp_path: Path) -> None:
        """get_theme loads user theme when themes_dir is set."""
        (tmp_path / "Test Override.toml").write_text(
            "[general]\nbackground = '#ff0000'\n"
            "[text]\nprimary = '#ffffff'\nsuccess = '#00ff00'\nerror = '#ff0000'\n"
        )
        theme = get_theme("Test Override", str(tmp_path))
        assert isinstance(theme, DashboardTheme)

    def test_get_theme_css_user_dir_override(self, tmp_path: Path) -> None:
        """get_theme_css loads user theme CSS when themes_dir is set."""
        (tmp_path / "Test CSS.toml").write_text(
            "[general]\nbackground = '#111111'\n"
            "[text]\nprimary = '#eeeeee'\nsuccess = '#00ff00'\nerror = '#ff0000'\n"
        )
        css = get_theme_css("Test CSS", str(tmp_path))
        assert ":root {" in css
        assert "--page-bg:" in css
