const DEFAULT_THEME: &str = "Cyber Red";
const MAX_THEME_NAME_BYTES: usize = 128;

pub(super) const THEME_NAMES: &[&str] = &[
    "default",
    "Booberry",
    "Catppuccin Latte",
    "Catppuccin Macchiato",
    "Catppuccin Mocha",
    "Cyber Red",
    "Cyberpunk",
    "Dark Green",
    "Discord",
    "Discord (80_ Saturation)",
    "Dracula",
    "Ferra Light",
    "Flexor Dark",
    "Gruvbox",
    "Halcyon Dark",
    "IntelliJ Light",
    "Kanagawa",
    "Macaw Dark",
    "Macaw Light",
    "Matrix",
    "Noctis Lilac",
    "Nord",
    "Nostromo Terminal",
    "One Dark",
    "Oxocarbon",
    "Rose Pine",
    "Rose Pine Dawn",
    "Rose Pine Moon",
    "Solarized Dark",
    "Sonokai",
    "Tokyo Night Storm",
    "VESPER",
    "Zenburn",
    "acton",
    "bam",
    "base16-atelier-forest-light",
    "berlin",
    "black but with important highlights",
    "broc",
    "cork",
    "ferra",
    "forest",
    "lisbon",
    "midnight",
    "oslo",
    "plum",
    "portland",
    "sunset",
    "tofino",
    "vanimo",
    "vik",
];

pub(super) fn selected_theme(configured: &str) -> &str {
    if configured.len() <= MAX_THEME_NAME_BYTES && THEME_NAMES.contains(&configured) {
        configured
    } else {
        DEFAULT_THEME
    }
}

pub(super) fn parse_theme_rgb(color: &str) -> Option<(f64, f64, f64)> {
    let color = color.strip_prefix('#')?;
    let color = if color.len() == 8 {
        &color[..6]
    } else if color.len() == 6 {
        color
    } else {
        return None;
    };
    Some((
        u8::from_str_radix(&color[0..2], 16).ok()? as f64 / 255.0,
        u8::from_str_radix(&color[2..4], 16).ok()? as f64 / 255.0,
        u8::from_str_radix(&color[4..6], 16).ok()? as f64 / 255.0,
    ))
}

pub(super) fn theme_lightness(color: &str) -> Option<f64> {
    let (red, green, blue) = parse_theme_rgb(color)?;
    Some((red.max(green).max(blue) + red.min(green).min(blue)) / 2.0)
}

pub(super) fn mix_theme_colors(base: &str, target: &str, ratio: f64) -> Option<String> {
    let channels = |color: &str| -> Option<(i32, i32, i32)> {
        let color = color.strip_prefix('#')?;
        let color = if color.len() == 8 { &color[..6] } else { color };
        if color.len() != 6 {
            return None;
        }
        Some((
            i32::from_str_radix(&color[0..2], 16).ok()?,
            i32::from_str_radix(&color[2..4], 16).ok()?,
            i32::from_str_radix(&color[4..6], 16).ok()?,
        ))
    };
    let (base_red, base_green, base_blue) = channels(base)?;
    let (target_red, target_green, target_blue) = channels(target)?;
    let channel = |base: i32, target: i32| (base as f64 + (target - base) as f64 * ratio) as u8;
    Some(format!(
        "#{:02x}{:02x}{:02x}",
        channel(base_red, target_red),
        channel(base_green, target_green),
        channel(base_blue, target_blue),
    ))
}

pub(super) fn adjust_theme_lightness(color: &str, factor: f64) -> Option<String> {
    let (red, green, blue) = parse_theme_rgb(color)?;
    let max = red.max(green).max(blue);
    let min = red.min(green).min(blue);
    let lightness = (max + min) / 2.0;
    let delta = max - min;
    if delta == 0.0 {
        let channel = (lightness * factor).clamp(0.0, 1.0);
        return Some(format!(
            "#{:02x}{:02x}{:02x}",
            (channel * 255.0) as u8,
            (channel * 255.0) as u8,
            (channel * 255.0) as u8,
        ));
    }

    let saturation = if lightness <= 0.5 {
        delta / (max + min)
    } else {
        delta / (2.0 - max - min)
    };
    let hue = if max == red {
        ((green - blue) / delta + if green < blue { 6.0 } else { 0.0 }) / 6.0
    } else if max == green {
        ((blue - red) / delta + 2.0) / 6.0
    } else {
        ((red - green) / delta + 4.0) / 6.0
    };
    let lightness = (lightness * factor).clamp(0.0, 1.0);
    let q = if lightness < 0.5 {
        lightness * (1.0 + saturation)
    } else {
        lightness + saturation - lightness * saturation
    };
    let p = 2.0 * lightness - q;
    let hue_to_rgb = |mut hue: f64| {
        if hue < 0.0 {
            hue += 1.0;
        } else if hue > 1.0 {
            hue -= 1.0;
        }
        if hue < 1.0 / 6.0 {
            p + (q - p) * 6.0 * hue
        } else if hue < 0.5 {
            q
        } else if hue < 2.0 / 3.0 {
            p + (q - p) * (2.0 / 3.0 - hue) * 6.0
        } else {
            p
        }
    };
    Some(format!(
        "#{:02x}{:02x}{:02x}",
        (hue_to_rgb(hue + 1.0 / 3.0) * 255.0) as u8,
        (hue_to_rgb(hue) * 255.0) as u8,
        (hue_to_rgb(hue - 1.0 / 3.0) * 255.0) as u8,
    ))
}

pub(super) fn theme_heatmap_colors(name: &str) -> [String; 5] {
    let Some(bytes) = theme_bytes(name) else {
        return [
            "#ebedf0".to_owned(),
            "#9be9a8".to_owned(),
            "#40c463".to_owned(),
            "#30a14e".to_owned(),
            "#216e39".to_owned(),
        ];
    };
    let value = std::str::from_utf8(bytes)
        .unwrap_or("")
        .parse::<toml::Value>()
        .unwrap_or_else(|_| toml::Value::Table(Default::default()));
    let background = theme_value(&value, &["general", "background"], "#1e1e2e");
    let primary = theme_value(&value, &["text", "primary"], "#cdd6f4");
    let success = theme_value(&value, &["text", "success"], "#a6e3a1");
    let page_background = if parse_theme_rgb(background)
        .is_some_and(|(r, g, b)| (r.max(g).max(b) + r.min(g).min(b)) / 2.0 < 0.5)
    {
        theme_value(&value, &["buffer", "background"], background)
    } else {
        background
    };
    let mix = |base: &str, target: &str, ratio: f64| {
        let (Some((r1, g1, b1)), Some((r2, g2, b2))) =
            (parse_theme_rgb(base), parse_theme_rgb(target))
        else {
            return base.to_owned();
        };
        format!(
            "#{:02x}{:02x}{:02x}",
            (r1 * 255.0 + (r2 - r1) * 255.0 * ratio) as u8,
            (g1 * 255.0 + (g2 - g1) * 255.0 * ratio) as u8,
            (b1 * 255.0 + (b2 - b1) * 255.0 * ratio) as u8,
        )
    };
    let adjust = |color: &str, factor: f64| {
        let Some((r, g, b)) = parse_theme_rgb(color) else {
            return color.to_owned();
        };
        let max = r.max(g).max(b);
        let min = r.min(g).min(b);
        let mut lightness = (max + min) / 2.0;
        let delta = max - min;
        if delta == 0.0 {
            lightness = (lightness * factor).clamp(0.0, 1.0);
            let channel = (lightness * 255.0) as u8;
            return format!("#{channel:02x}{channel:02x}{channel:02x}");
        }
        let saturation = if lightness > 0.5 {
            delta / (2.0 - max - min)
        } else {
            delta / (max + min)
        };
        let hue = if max == r {
            (g - b) / delta + if g < b { 6.0 } else { 0.0 }
        } else if max == g {
            (b - r) / delta + 2.0
        } else {
            (r - g) / delta + 4.0
        } / 6.0;
        lightness = (lightness * factor).clamp(0.0, 1.0);
        let q = if lightness < 0.5 {
            lightness * (1.0 + saturation)
        } else {
            lightness + saturation - lightness * saturation
        };
        let p = 2.0 * lightness - q;
        let hue_channel = |mut t: f64| {
            if t < 0.0 {
                t += 1.0;
            }
            if t > 1.0 {
                t -= 1.0;
            }
            if t < 1.0 / 6.0 {
                p + (q - p) * 6.0 * t
            } else if t < 1.0 / 2.0 {
                q
            } else if t < 2.0 / 3.0 {
                p + (q - p) * (2.0 / 3.0 - t) * 6.0
            } else {
                p
            }
        };
        format!(
            "#{:02x}{:02x}{:02x}",
            (hue_channel(hue + 1.0 / 3.0) * 255.0) as u8,
            (hue_channel(hue) * 255.0) as u8,
            (hue_channel(hue - 1.0 / 3.0) * 255.0) as u8,
        )
    };
    [
        mix(page_background, primary, 0.06),
        mix(page_background, success, 0.35),
        success.to_owned(),
        adjust(success, 0.7),
        adjust(success, 0.45),
    ]
}

pub(super) fn theme_variables(name: &str) -> String {
    let Some(bytes) = theme_bytes(name) else {
        return String::new();
    };
    let value = std::str::from_utf8(bytes)
        .unwrap_or("")
        .parse::<toml::Value>()
        .unwrap_or_else(|_| toml::Value::Table(Default::default()));
    let get = |path: &[&str], fallback: &'static str| theme_value(&value, path, fallback);
    let general_background = get(&["general", "background"], "#1e1e2e");
    let text_primary = get(&["text", "primary"], "#cdd6f4");
    let text_secondary = get(&["text", "secondary"], "#a6adc8");
    let text_success = get(&["text", "success"], "#a6e3a1");
    let text_error = get(&["text", "error"], "#f38ba8");
    let buffer_background = get(&["buffer", "background"], "#1e1e2e");
    let buffer_title = get(&["buffer", "background_title_bar"], "#181825");
    let buffer_url = get(&["buffer", "url"], "#89b4fa");
    let buffer_action = get(&["buffer", "action"], "#fab387");
    let page_background = if theme_lightness(general_background).is_some_and(|value| value < 0.5) {
        buffer_background.to_owned()
    } else {
        general_background.to_owned()
    };
    let info = buffer_url;
    let warning = buffer_action;
    let primary_button = get(&["buttons", "primary", "background_selected"], "#313244");
    let primary_button = if primary_button.is_empty() {
        get(&["buffer", "background_title_bar"], "#181825")
    } else {
        primary_button
    };
    let muted = {
        let topic = get(&["buffer", "topic"], "#7f849c");
        if topic == text_primary {
            text_secondary
        } else {
            topic
        }
    };
    let page_border = get(&["general", "border"], "#45475a");
    let card_background = buffer_background;
    let button_primary_background = {
        let selected = get(&["buttons", "primary", "background_selected"], "");
        if !selected.is_empty() {
            selected
        } else {
            let background = get(&["buttons", "primary", "background"], "");
            if background.is_empty() {
                general_background
            } else {
                background
            }
        }
    };
    let values = [
        ("--page-bg", page_background.to_owned()),
        ("--page-text", text_primary.to_owned()),
        ("--page-border", page_border.to_owned()),
        ("--topbar-bg", general_background.to_owned()),
        ("--topbar-text", text_primary.to_owned()),
        ("--topbar-border", page_border.to_owned()),
        ("--nav-text", text_secondary.to_owned()),
        (
            "--nav-hover-bg",
            get(&["buffer", "highlight"], "#45475a").to_owned(),
        ),
        ("--nav-active-bg", primary_button.to_owned()),
        ("--nav-active-text", text_primary.to_owned()),
        ("--card-bg", card_background.to_owned()),
        ("--card-border", page_border.to_owned()),
        ("--table-header-bg", buffer_title.to_owned()),
        ("--table-header-text", text_secondary.to_owned()),
        (
            "--table-border",
            get(&["general", "horizontal_rule"], "#313244").to_owned(),
        ),
        ("--text-muted", muted.to_owned()),
        ("--text-secondary", text_secondary.to_owned()),
        ("--color-success", text_success.to_owned()),
        ("--color-error", text_error.to_owned()),
        ("--color-warning", warning.to_owned()),
        ("--color-info", info.to_owned()),
        ("--button-primary-bg", button_primary_background.to_owned()),
        ("--button-primary-text", text_primary.to_owned()),
        (
            "--chip-bg",
            mix_theme_colors(&page_background, text_primary, 0.06)
                .unwrap_or_else(|| "#313244".to_owned()),
        ),
        (
            "--chip-border",
            mix_theme_colors(&page_background, text_primary, 0.14)
                .unwrap_or_else(|| "#45475a".to_owned()),
        ),
        ("--button-bg", card_background.to_owned()),
        (
            "--button-border",
            mix_theme_colors(card_background, text_primary, 0.18)
                .unwrap_or_else(|| page_border.to_owned()),
        ),
        (
            "--button-bg-hover",
            mix_theme_colors(card_background, text_primary, 0.08)
                .unwrap_or_else(|| card_background.to_owned()),
        ),
        (
            "--button-bg-active",
            mix_theme_colors(card_background, info, 0.20)
                .unwrap_or_else(|| card_background.to_owned()),
        ),
        ("--link-color", info.to_owned()),
        (
            "--link-color-hover",
            adjust_theme_lightness(info, 0.85).unwrap_or_else(|| info.to_owned()),
        ),
        ("--accent-color", info.to_owned()),
        (
            "--tag-default-bg",
            mix_theme_colors(&page_background, info, 0.15)
                .unwrap_or_else(|| page_background.to_owned()),
        ),
        ("--tag-default-text", info.to_owned()),
        (
            "--tag-success-bg",
            mix_theme_colors(&page_background, text_success, 0.15)
                .unwrap_or_else(|| page_background.to_owned()),
        ),
        ("--tag-success-text", text_success.to_owned()),
        (
            "--tag-warning-bg",
            mix_theme_colors(&page_background, warning, 0.15)
                .unwrap_or_else(|| page_background.to_owned()),
        ),
        ("--tag-warning-text", warning.to_owned()),
        (
            "--tag-error-bg",
            mix_theme_colors(&page_background, text_error, 0.15)
                .unwrap_or_else(|| page_background.to_owned()),
        ),
        ("--tag-error-text", text_error.to_owned()),
        (
            "--heatmap-0",
            mix_theme_colors(&page_background, text_primary, 0.06)
                .unwrap_or_else(|| page_background.to_owned()),
        ),
        (
            "--heatmap-1",
            mix_theme_colors(&page_background, text_success, 0.35)
                .unwrap_or_else(|| page_background.to_owned()),
        ),
        ("--heatmap-2", text_success.to_owned()),
        (
            "--heatmap-3",
            adjust_theme_lightness(text_success, 0.7).unwrap_or_else(|| text_success.to_owned()),
        ),
        (
            "--heatmap-4",
            adjust_theme_lightness(text_success, 0.45).unwrap_or_else(|| text_success.to_owned()),
        ),
        ("--heatmap-label-text", muted.to_owned()),
    ];
    let declarations = values
        .iter()
        .map(|(property, color)| format!("  {property}: {color};"))
        .collect::<Vec<_>>()
        .join("\n");
    format!(":root {{\n{declarations}\n}}")
}

pub(super) fn theme_bytes(name: &str) -> Option<&'static [u8]> {
    Some(match name {
        "Booberry" => include_bytes!("../../../assets/dashboard/themes/Booberry.toml"),
        "Catppuccin Latte" => {
            include_bytes!("../../../assets/dashboard/themes/Catppuccin Latte.toml")
        }
        "Catppuccin Macchiato" => {
            include_bytes!("../../../assets/dashboard/themes/Catppuccin Macchiato.toml")
        }
        "Catppuccin Mocha" => {
            include_bytes!("../../../assets/dashboard/themes/Catppuccin Mocha.toml")
        }
        "Cyber Red" => include_bytes!("../../../assets/dashboard/themes/Cyber Red.toml"),
        "Cyberpunk" => include_bytes!("../../../assets/dashboard/themes/Cyberpunk.toml"),
        "Dark Green" => include_bytes!("../../../assets/dashboard/themes/Dark Green.toml"),
        "Discord (80_ Saturation)" => {
            include_bytes!("../../../assets/dashboard/themes/Discord (80_ Saturation).toml")
        }
        "Discord" => include_bytes!("../../../assets/dashboard/themes/Discord.toml"),
        "Dracula" => include_bytes!("../../../assets/dashboard/themes/Dracula.toml"),
        "Ferra Light" => include_bytes!("../../../assets/dashboard/themes/Ferra Light.toml"),
        "Flexor Dark" => include_bytes!("../../../assets/dashboard/themes/Flexor Dark.toml"),
        "Gruvbox" => include_bytes!("../../../assets/dashboard/themes/Gruvbox.toml"),
        "Halcyon Dark" => include_bytes!("../../../assets/dashboard/themes/Halcyon Dark.toml"),
        "IntelliJ Light" => include_bytes!("../../../assets/dashboard/themes/IntelliJ Light.toml"),
        "Kanagawa" => include_bytes!("../../../assets/dashboard/themes/Kanagawa.toml"),
        "Macaw Dark" => include_bytes!("../../../assets/dashboard/themes/Macaw Dark.toml"),
        "Macaw Light" => include_bytes!("../../../assets/dashboard/themes/Macaw Light.toml"),
        "Matrix" => include_bytes!("../../../assets/dashboard/themes/Matrix.toml"),
        "Noctis Lilac" => include_bytes!("../../../assets/dashboard/themes/Noctis Lilac.toml"),
        "Nord" => include_bytes!("../../../assets/dashboard/themes/Nord.toml"),
        "Nostromo Terminal" => {
            include_bytes!("../../../assets/dashboard/themes/Nostromo Terminal.toml")
        }
        "One Dark" => include_bytes!("../../../assets/dashboard/themes/One Dark.toml"),
        "Oxocarbon" => include_bytes!("../../../assets/dashboard/themes/Oxocarbon.toml"),
        "Rose Pine Dawn" => include_bytes!("../../../assets/dashboard/themes/Rose Pine Dawn.toml"),
        "Rose Pine Moon" => include_bytes!("../../../assets/dashboard/themes/Rose Pine Moon.toml"),
        "Rose Pine" => include_bytes!("../../../assets/dashboard/themes/Rose Pine.toml"),
        "Solarized Dark" => include_bytes!("../../../assets/dashboard/themes/Solarized Dark.toml"),
        "Sonokai" => include_bytes!("../../../assets/dashboard/themes/Sonokai.toml"),
        "Tokyo Night Storm" => {
            include_bytes!("../../../assets/dashboard/themes/Tokyo Night Storm.toml")
        }
        "VESPER" => include_bytes!("../../../assets/dashboard/themes/VESPER.toml"),
        "Zenburn" => include_bytes!("../../../assets/dashboard/themes/Zenburn.toml"),
        "acton" => include_bytes!("../../../assets/dashboard/themes/acton.toml"),
        "bam" => include_bytes!("../../../assets/dashboard/themes/bam.toml"),
        "base16-atelier-forest-light" => {
            include_bytes!("../../../assets/dashboard/themes/base16-atelier-forest-light.toml")
        }
        "berlin" => include_bytes!("../../../assets/dashboard/themes/berlin.toml"),
        "black but with important highlights" => {
            include_bytes!(
                "../../../assets/dashboard/themes/black but with important highlights.toml"
            )
        }
        "broc" => include_bytes!("../../../assets/dashboard/themes/broc.toml"),
        "cork" => include_bytes!("../../../assets/dashboard/themes/cork.toml"),
        "ferra" => include_bytes!("../../../assets/dashboard/themes/ferra.toml"),
        "forest" => include_bytes!("../../../assets/dashboard/themes/forest.toml"),
        "lisbon" => include_bytes!("../../../assets/dashboard/themes/lisbon.toml"),
        "midnight" => include_bytes!("../../../assets/dashboard/themes/midnight.toml"),
        "oslo" => include_bytes!("../../../assets/dashboard/themes/oslo.toml"),
        "plum" => include_bytes!("../../../assets/dashboard/themes/plum.toml"),
        "portland" => include_bytes!("../../../assets/dashboard/themes/portland.toml"),
        "sunset" => include_bytes!("../../../assets/dashboard/themes/sunset.toml"),
        "tofino" => include_bytes!("../../../assets/dashboard/themes/tofino.toml"),
        "vanimo" => include_bytes!("../../../assets/dashboard/themes/vanimo.toml"),
        "vik" => include_bytes!("../../../assets/dashboard/themes/vik.toml"),
        _ => return None,
    })
}

pub(super) fn theme_value<'a>(value: &'a toml::Value, path: &[&str], fallback: &'a str) -> &'a str {
    path.iter()
        .try_fold(value, |current, key| current.get(*key))
        .and_then(toml::Value::as_str)
        .unwrap_or(fallback)
}
