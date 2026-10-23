pub(super) const DEFAULT_THEME: &str = "Cyber Red";
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

/// WCAG AA for normal text. A status pill is text, so its foreground has to
/// clear this against the surface it sits on — the theme's card background.
pub(super) const PILL_MINIMUM_CONTRAST: f64 = 4.5;

/// The derivation aims slightly above the floor so that rounding a channel to
/// an integer cannot land a served value below it.
const PILL_CONTRAST_MARGIN: f64 = 0.05;

/// The status-pill roles, ordered from the least prominent (a placeholder) to
/// the most prominent (a state an operator has to act on).
///
/// The order is also the lightness ladder. A theme whose semantic tokens
/// coincide — several bundled themes give `text.success`, `text.warning` and
/// `text.error` the same colour — would otherwise collapse every pill onto
/// one colour, so each role is placed on its own rung of the ladder as well.
const PILL_ROLES: [(&str, f64); 6] = [
    ("neutral", 0.00),
    ("success", 0.18),
    ("info", 0.36),
    ("warning", 0.54),
    ("sparse", 0.72),
    ("error", 0.90),
];

/// The relative luminance of a theme colour (WCAG 2.x).
pub(super) fn theme_relative_luminance(color: &str) -> Option<f64> {
    let (red, green, blue) = parse_theme_rgb(color)?;
    let channel = |value: f64| {
        if value <= 0.03928 {
            value / 12.92
        } else {
            ((value + 0.055) / 1.055).powf(2.4)
        }
    };
    Some(0.2126 * channel(red) + 0.7152 * channel(green) + 0.0722 * channel(blue))
}

/// Scales a colour's channels toward black. The hue and the saturation of the
/// colour survive the scale, which a lightness-only rewrite would not.
fn theme_darken_color(color: &str, factor: f64) -> Option<String> {
    let (red, green, blue) = parse_theme_rgb(color)?;
    let channel = |value: f64| (value * factor * 255.0).round().clamp(0.0, 255.0) as u8;
    Some(format!(
        "#{:02x}{:02x}{:02x}",
        channel(red),
        channel(green),
        channel(blue)
    ))
}

/// Moves a colour's channels toward white.
fn theme_lighten_color(color: &str, amount: f64) -> Option<String> {
    let (red, green, blue) = parse_theme_rgb(color)?;
    let channel = |value: f64| {
        (value * 255.0 + (255.0 - value * 255.0) * amount)
            .round()
            .clamp(0.0, 255.0) as u8
    };
    Some(format!(
        "#{:02x}{:02x}{:02x}",
        channel(red),
        channel(green),
        channel(blue)
    ))
}

/// The foreground for one status-pill role.
///
/// The base is the theme's own semantic colour. It is moved to a lightness
/// that clears `PILL_MINIMUM_CONTRAST` against the theme's card background —
/// the surface the pill text actually sits on — and then placed on the role's
/// rung of the lightness ladder so two roles never collapse onto one colour.
pub(super) fn theme_pill_color(base: &str, card_background: &str, ladder: f64) -> Option<String> {
    let card_luminance = theme_relative_luminance(card_background)?;
    let floor = PILL_MINIMUM_CONTRAST + PILL_CONTRAST_MARGIN;
    // A light card needs a dark pill and a dark card needs a light one; the
    // luminance bounds below are the two ends of the band that clears the
    // floor on that side.
    let target = if card_luminance >= 0.175 {
        let edge = (card_luminance + 0.05) / floor - 0.05;
        (edge * (1.0 - ladder)).max(0.0)
    } else {
        let low = floor * (card_luminance + 0.05) - 0.05;
        (low + (1.0 - low) * ladder).min(1.0)
    };
    let base_luminance = theme_relative_luminance(base)?;
    if target < base_luminance {
        // Darken until the luminance reaches the target. Darkening only ever
        // increases the contrast against a light card.
        let mut low = 0.0_f64;
        let mut high = 1.0_f64;
        for _ in 0..48 {
            let mid = (low + high) / 2.0;
            let candidate = theme_darken_color(base, mid)?;
            if theme_relative_luminance(&candidate)? > target {
                high = mid;
            } else {
                low = mid;
            }
        }
        theme_darken_color(base, high)
    } else {
        // Lighten until the luminance reaches the target. Lightening only ever
        // increases the contrast against a dark card.
        let mut low = 0.0_f64;
        let mut high = 1.0_f64;
        for _ in 0..48 {
            let mid = (low + high) / 2.0;
            let candidate = theme_lighten_color(base, mid)?;
            if theme_relative_luminance(&candidate)? < target {
                low = mid;
            } else {
                high = mid;
            }
        }
        theme_lighten_color(base, high)
    }
}

pub(super) fn parse_theme_rgb(color: &str) -> Option<(f64, f64, f64)> {
    let color = color.strip_prefix('#')?;
    if !color.is_ascii() {
        return None;
    }
    let color = if color.len() == 8 {
        color.get(..6)?
    } else if color.len() == 6 {
        color
    } else {
        return None;
    };
    Some((
        u8::from_str_radix(color.get(0..2)?, 16).ok()? as f64 / 255.0,
        u8::from_str_radix(color.get(2..4)?, 16).ok()? as f64 / 255.0,
        u8::from_str_radix(color.get(4..6)?, 16).ok()? as f64 / 255.0,
    ))
}

pub(super) fn theme_lightness(color: &str) -> Option<f64> {
    let (red, green, blue) = parse_theme_rgb(color)?;
    Some((red.max(green).max(blue) + red.min(green).min(blue)) / 2.0)
}

pub(super) fn mix_theme_colors(base: &str, target: &str, ratio: f64) -> Option<String> {
    let channels = |color: &str| -> Option<(i32, i32, i32)> {
        let color = color.strip_prefix('#')?;
        if !color.is_ascii() {
            return None;
        }
        let color = if color.len() == 8 {
            color.get(..6)?
        } else {
            color
        };
        if color.len() != 6 {
            return None;
        }
        Some((
            i32::from_str_radix(color.get(0..2)?, 16).ok()?,
            i32::from_str_radix(color.get(2..4)?, 16).ok()?,
            i32::from_str_radix(color.get(4..6)?, 16).ok()?,
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
    // Page and card surfaces must be opaque. `body` paints its gradient via
    // `background-image` with no `background-color` behind it, and the card
    // fill is what separates a panel from the watermark behind it, so a
    // theme-supplied alpha channel here (e.g. plum's `#27273DE2`) let the
    // canvas through. Drop the alpha rather than trusting it.
    let page_background = opaque_hex(
        if theme_lightness(general_background).is_some_and(|value| value < 0.5) {
            buffer_background
        } else {
            general_background
        },
    );
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
    let card_background = opaque_hex(buffer_background);
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
    // A status pill is text, so its foreground has to clear the AA floor
    // against the card background it sits on. The themes' own semantic tokens
    // are not contrast-safe on their own — several bundled themes give
    // `text.success` a mid tone on a near-white card — so each pill colour is
    // derived from the theme's token and moved to a lightness that clears the
    // floor. `sparse` has no token of its own: it sits between the warning and
    // the error hue.
    let pill_sparse_base =
        mix_theme_colors(warning, text_error, 0.5).unwrap_or_else(|| warning.to_owned());
    let pill_bases = [
        ("neutral", muted),
        ("success", text_success),
        ("info", info),
        ("warning", warning),
        ("sparse", pill_sparse_base.as_str()),
        ("error", text_error),
    ];
    let pill_values = PILL_ROLES
        .iter()
        .zip(pill_bases.iter())
        .map(|((role, ladder), (_, base))| {
            let color = theme_pill_color(base, &card_background, *ladder)
                .unwrap_or_else(|| base.to_string());
            (format!("--pill-{role}"), color)
        })
        .collect::<Vec<_>>();
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
            mix_theme_colors(&card_background, text_primary, 0.18)
                .unwrap_or_else(|| page_border.to_owned()),
        ),
        (
            "--button-bg-hover",
            mix_theme_colors(&card_background, text_primary, 0.08)
                .unwrap_or_else(|| card_background.to_owned()),
        ),
        (
            "--button-bg-active",
            mix_theme_colors(&card_background, info, 0.20)
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
        .chain(
            pill_values
                .iter()
                .map(|(property, color)| format!("  {property}: {color};")),
        )
        .collect::<Vec<_>>()
        .join("\n");
    format!(":root {{\n{declarations}\n}}")
}

pub(super) fn theme_bytes(name: &str) -> Option<&'static [u8]> {
    Some(match name {
        // `default` is the first entry in `THEME_NAMES` and the value the theme
        // picker offers, but it has no TOML of its own. Alias it to the same
        // theme the config path falls back to so selecting it renders a fully
        // styled dashboard instead of an unstyled one.
        "default" => theme_bytes(DEFAULT_THEME)?,
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

/// Strip an 8-digit hex alpha channel, yielding the fully opaque colour. Any
/// other shape (6-digit hex, non-hex, empty) is returned untouched so the
/// existing `theme_value` fallbacks still apply.
fn opaque_hex(color: &str) -> String {
    let trimmed = color.strip_prefix('#').unwrap_or(color);
    if trimmed.len() == 8 && trimmed.chars().all(|c| c.is_ascii_hexdigit()) {
        color
            .get(..7)
            .map(str::to_owned)
            .unwrap_or_else(|| color.to_owned())
    } else {
        color.to_owned()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The WCAG contrast ratio between two theme colours.
    fn theme_contrast_ratio(first: &str, second: &str) -> Option<f64> {
        let first = theme_relative_luminance(first)?;
        let second = theme_relative_luminance(second)?;
        let (high, low) = if first > second {
            (first, second)
        } else {
            (second, first)
        };
        Some((high + 0.05) / (low + 0.05))
    }

    /// Reads one `--name: value;` declaration out of a served theme stylesheet.
    fn theme_css_variable(css: &str, name: &str) -> Option<String> {
        let prefix = format!("{name}: ");
        let start = css.find(&prefix)? + prefix.len();
        let rest = &css[start..];
        let end = rest.find(';')?;
        Some(rest[..end].trim().to_owned())
    }

    /// The pill variables the stylesheet reads, in the order the guard checks
    /// them.
    const PILL_VARIABLES: [&str; 6] = [
        "--pill-neutral",
        "--pill-success",
        "--pill-info",
        "--pill-warning",
        "--pill-sparse",
        "--pill-error",
    ];

    /// Every pill foreground that misses the floor against the card background
    /// the server serves for this theme.
    ///
    /// The guard reads the served stylesheet rather than a separate table so a
    /// theme change cannot silently invalidate it: the values it measures are
    /// exactly the values the browser resolves.
    fn pill_contrast_violations(css: &str) -> Vec<String> {
        let Some(card_background) = theme_css_variable(css, "--card-bg") else {
            return vec!["--card-bg is missing".to_owned()];
        };
        let mut violations = Vec::new();
        for variable in PILL_VARIABLES {
            let Some(foreground) = theme_css_variable(css, variable) else {
                violations.push(format!("{variable} is missing"));
                continue;
            };
            let Some(ratio) = theme_contrast_ratio(&foreground, &card_background) else {
                violations.push(format!("{variable} is not a colour"));
                continue;
            };
            if ratio < PILL_MINIMUM_CONTRAST {
                violations.push(format!(
                    "{variable} {foreground} on {card_background} is {ratio:.2}:1"
                ));
            }
        }
        violations
    }

    /// The Euclidean distance between two colours, in 0-255 channel units.
    fn pill_color_distance(first: &str, second: &str) -> Option<f64> {
        let (first, second) = (parse_theme_rgb(first)?, parse_theme_rgb(second)?);
        let distance = (first.0 - second.0).powi(2)
            + (first.1 - second.1).powi(2)
            + (first.2 - second.2).powi(2);
        Some(distance.sqrt() * 255.0)
    }

    /// Two pills that render the same colour are not distinguishable, so the
    /// guard refuses a theme whose roles collide.
    const PILL_MINIMUM_DISTANCE: f64 = 12.0;

    fn pill_distinguishability_violations(css: &str) -> Vec<String> {
        let mut violations = Vec::new();
        let mut colors: Vec<(&str, String)> = Vec::new();
        for variable in PILL_VARIABLES {
            if let Some(value) = theme_css_variable(css, variable) {
                colors.push((variable, value));
            }
        }
        for (index, (first_name, first)) in colors.iter().enumerate() {
            for (second_name, second) in colors.iter().skip(index + 1) {
                let Some(distance) = pill_color_distance(first, second) else {
                    violations.push(format!("{first_name} or {second_name} is not a colour"));
                    continue;
                };
                if distance < PILL_MINIMUM_DISTANCE {
                    violations.push(format!(
                        "{first_name} {first} and {second_name} {second} are {distance:.1} apart"
                    ));
                }
            }
        }
        violations
    }

    #[test]
    fn every_bundled_theme_serves_contrast_safe_pill_foregrounds() {
        for name in THEME_NAMES {
            let css = theme_variables(name);
            assert_eq!(
                pill_contrast_violations(&css),
                Vec::<String>::new(),
                "theme {name:?} serves a pill that misses the contrast floor"
            );
        }
    }

    #[test]
    fn every_bundled_theme_keeps_its_pill_roles_distinguishable() {
        for name in THEME_NAMES {
            let css = theme_variables(name);
            assert_eq!(
                pill_distinguishability_violations(&css),
                Vec::<String>::new(),
                "theme {name:?} collapses two pill roles onto one colour"
            );
        }
    }

    #[test]
    fn the_contrast_guard_rejects_a_deliberate_regression() {
        // A guard that cannot fail proves nothing, so a deliberately unreadable
        // pill colour has to be rejected.
        let css = theme_variables(DEFAULT_THEME);
        assert_eq!(pill_contrast_violations(&css), Vec::<String>::new());
        let card_background = theme_css_variable(&css, "--card-bg").expect("card background");
        let success = theme_css_variable(&css, "--pill-success").expect("success pill");
        let regressed = css.replace(
            &format!("--pill-success: {success};"),
            &format!("--pill-success: {card_background};"),
        );
        let violations = pill_contrast_violations(&regressed);
        assert!(
            !violations.is_empty(),
            "the guard accepted a pill foreground that matches the card background"
        );
    }

    #[test]
    fn the_distinguishability_guard_rejects_a_collapsed_role() {
        let css = theme_variables(DEFAULT_THEME);
        let success = theme_css_variable(&css, "--pill-success").expect("success pill");
        let warning = theme_css_variable(&css, "--pill-warning").expect("warning pill");
        let collapsed = css.replace(
            &format!("--pill-warning: {warning};"),
            &format!("--pill-warning: {success};"),
        );
        let violations = pill_distinguishability_violations(&collapsed);
        assert!(
            !violations.is_empty(),
            "the guard accepted two pill roles that render the same colour"
        );
    }

    #[test]
    fn the_pill_palette_is_derived_from_the_theme_not_from_a_fixed_table() {
        // Two themes with different card backgrounds must not produce the same
        // pill foregrounds: the colours are derived, not copied.
        let light = theme_variables("Catppuccin Latte");
        let dark = theme_variables("Nord");
        assert_ne!(
            theme_css_variable(&light, "--pill-success"),
            theme_css_variable(&dark, "--pill-success")
        );
    }
}
