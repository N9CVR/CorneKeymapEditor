#!/usr/bin/env python3
"""Corne Keymap Editor - a small, dependency-free Tkinter editor for ZMK keymaps."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from math import ceil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

APP_NAME = "Corne Keymap Editor"
VERSION = "1.7.0"
KEY_COUNT = 42
ROW_SLICES = (slice(0, 12), slice(12, 24), slice(24, 36), slice(36, 42))
CORNE_SPLIT_COLUMN = 6
CORNE_MAIN_GAP_PX = 48
CORNE_DIALOG_GAP_PX = 34


class KeymapError(ValueError):
    pass


def corne_grid_position(index: int) -> tuple[int, int]:
    """Return the GUI row/column for a standard 42-key Corne position."""
    if index < 0 or index >= KEY_COUNT:
        raise ValueError(f"Corne position must be between 0 and {KEY_COUNT - 1}.")
    if index < 36:
        row, within = divmod(index, 12)
        return row, within if within < 6 else within + 1
    return 3, (3, 4, 5, 7, 8, 9)[index - 36]


def configure_corne_grid(keyboard, gap_pixels: int):
    """Configure an unlabeled spacer column between the Corne halves."""
    for column in range(13):
        if column == CORNE_SPLIT_COLUMN:
            keyboard.columnconfigure(column, weight=0, minsize=gap_pixels)
        else:
            keyboard.columnconfigure(column, weight=1, minsize=0)


@dataclass
class NodeSpan:
    name: str
    start: int
    open_brace: int
    close_brace: int
    end: int


@dataclass
class Layer:
    name: str
    original_name: Optional[str]
    display_name: str
    original_display_name: Optional[str]
    display_name_span: Optional[tuple[int, int]]
    bindings: list[str]
    original_bindings: list[str]
    binding_body_span: Optional[tuple[int, int]]
    comment_span: Optional[tuple[int, int]]
    binding_indent: str
    node_start: Optional[int]
    node_open_brace: Optional[int]


@dataclass
class Combo:
    name: str
    positions: list[int] = field(default_factory=list)
    binding: str = "&kp ESC"
    timeout_ms: int = 50
    layers: list[int] = field(default_factory=list)
    require_prior_idle_ms: Optional[int] = None
    slow_release: bool = False


@dataclass
class ConditionalLayer:
    name: str
    if_layers: list[int] = field(default_factory=list)
    then_layer: int = 0


SAFE_LAYER_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,30}$")


def normalize_layer_identifier(display_name: object, fallback_index: int = 0) -> str:
    """Create a conservative Zephyr-safe node identifier from a visible name."""
    value = "" if display_name is None else str(display_name).strip()
    # Preserve the user's visible text separately; the source identifier uses
    # a deliberately conservative lowercase C-identifier subset.
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"[^a-z0-9_]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("_")
    if not value:
        value = f"layer_{fallback_index}"
    if value[0].isdigit():
        value = "layer_" + value
    return value[:31].rstrip("_") or f"layer_{fallback_index}"


def unique_layer_identifier(display_name: object, used: set[str], fallback_index: int = 0) -> str:
    base = normalize_layer_identifier(display_name, fallback_index)
    candidate = base
    suffix = 2
    while candidate in used:
        marker = f"_{suffix}"
        candidate = base[: 31 - len(marker)].rstrip("_") + marker
        suffix += 1
    return candidate


def escape_dts_string(value: object) -> str:
    text = "" if value is None else str(value)
    text = "".join(character for character in text if character >= " " and character != "\x7f")
    return text.replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True)
class ShortcutWarning:
    source: str
    shortcut: str
    message: str
    layer_index: Optional[int] = None
    key_position: Optional[int] = None
    combo_index: Optional[int] = None


def settings_path() -> Path:
    override = os.environ.get("CORNE_KEYMAP_EDITOR_SETTINGS")
    if override:
        return Path(override).expanduser()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "corne-keymap-editor" / "settings.json"


def load_editor_settings(path: Optional[Path] = None) -> dict[str, str]:
    target = path or settings_path()
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return {str(key): str(value) for key, value in data.items() if isinstance(value, (str, int, float, bool))}


def save_editor_settings(settings: dict[str, str], path: Optional[Path] = None):
    target = path or settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(target)


def native_file_dialog(
    mode: str,
    title: str,
    initial_dir: Path,
    initial_file: str = "",
    filters: Optional[list[tuple[str, str]]] = None,
) -> tuple[bool, Optional[Path]]:
    """Use Cinnamon's GTK file chooser through Zenity when available.

    Returns (handled, path). handled=True/path=None means the user cancelled;
    handled=False means the caller should use Tk's portable fallback dialog.
    """
    if os.environ.get("CORNE_KEYMAP_EDITOR_DISABLE_NATIVE_DIALOGS") == "1":
        return False, None
    zenity = shutil.which("zenity")
    if not zenity:
        return False, None
    directory = initial_dir.expanduser()
    if not directory.is_dir():
        directory = Path.home()
    selected = directory / initial_file if initial_file else directory
    initial = str(selected)
    if not initial_file and not initial.endswith(os.sep):
        initial += os.sep
    command = [zenity, "--file-selection", "--modal", f"--title={title}", f"--filename={initial}"]
    if mode == "save":
        command.extend(["--save", "--confirm-overwrite"])
    for label, pattern in filters or []:
        command.append(f"--file-filter={label} | {pattern}")
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.SubprocessError):
        return False, None
    if result.returncode == 0:
        value = result.stdout.rstrip("\r\n")
        return True, Path(value) if value else None
    if result.returncode in (1, 5):
        return True, None
    return False, None


def _gsettings_value(schema: str, key: str) -> str:
    try:
        result = subprocess.run(
            ["gsettings", "get", schema, key],
            check=False,
            capture_output=True,
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    value = result.stdout.strip()
    # Some GLib versions include an explicit type annotation, for example
    # "@s 'Noto Sans 10'". Strip it before unquoting the string value.
    value = re.sub(r"^@(?:s|ms)\s+", "", value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value


PANGO_STYLE_WORDS = {
    "thin", "ultralight", "ultra-light", "light", "semilight", "semi-light",
    "book", "normal", "regular", "medium", "semibold", "semi-bold",
    "demibold", "demi-bold", "bold", "ultrabold", "ultra-bold", "heavy",
    "black", "roman", "italic", "oblique", "condensed", "semicondensed",
    "semi-condensed", "expanded",
}


def normalize_cinnamon_font(value: object) -> dict[str, object]:
    """Convert a Pango/Cinnamon font description to safe Tk font arguments.

    Tk's compact font-description string is not compatible with every Pango
    description. In particular, passing a multiword family through option_add
    can make Tcl interpret a numeric size as a style. Returning separate scalar
    arguments avoids that parser entirely.
    """
    raw = "" if value is None else str(value)
    raw = raw.replace("\x00", " ").strip()
    raw = re.sub(r"^@(?:s|ms)\s+", "", raw).strip()
    while len(raw) >= 2 and (raw[0], raw[-1]) in (("'", "'"), ('"', '"'), ("{", "}")):
        raw = raw[1:-1].strip()
    raw = re.sub(r"\s+", " ", raw)

    size = 10
    size_match = re.search(r"(?:^|\s)(\d+(?:\.\d+)?)\s*$", raw)
    if size_match:
        try:
            size = round(float(size_match.group(1)))
        except ValueError:
            size = 10
        raw = raw[: size_match.start()].strip()
    size = min(32, max(6, size))

    words = raw.split()
    styles: list[str] = []
    while words and words[-1].lower() in PANGO_STYLE_WORDS:
        styles.insert(0, words.pop().lower())
    family = " ".join(words).strip()
    if not family or re.fullmatch(r"[\d.]+", family):
        family = "Sans"
    # Keep only printable characters. Family is passed as a Tcl argument, not
    # parsed as Tcl source, so spaces and punctuation are safe.
    family = "".join(character for character in family if character.isprintable()).strip() or "Sans"

    weight_words = set(styles)
    weight = "bold" if weight_words.intersection({
        "semibold", "semi-bold", "demibold", "demi-bold", "bold",
        "ultrabold", "ultra-bold", "heavy", "black",
    }) else "normal"
    slant = "italic" if weight_words.intersection({"italic", "oblique"}) else "roman"
    return {"family": family, "size": size, "weight": weight, "slant": slant}


def cinnamon_appearance(values: Optional[dict[str, str]] = None) -> dict[str, str]:
    """Return a Cinnamon-aware Tk palette, with safe fallbacks elsewhere."""
    supplied = values or {}

    def value(key: str, schema: str, setting: str) -> str:
        supplied_value = supplied.get(key, "")
        if supplied_value not in (None, ""):
            return str(supplied_value)
        return _gsettings_value(schema, setting)

    gtk_theme = value("gtk_theme", "org.cinnamon.desktop.interface", "gtk-theme")
    font_name = value("font_name", "org.cinnamon.desktop.interface", "font-name") or "Sans 10"
    font = normalize_cinnamon_font(font_name)
    color_scheme = value("color_scheme", "org.cinnamon.desktop.interface", "color-scheme")
    portal_scheme = value("portal_scheme", "org.x.apps.portal", "color-scheme")
    dark = (
        "dark" in gtk_theme.lower()
        or "prefer-dark" in color_scheme.lower()
        or portal_scheme.strip() == "1"
    )

    theme_lower = gtk_theme.lower()
    accent = "#4a90d9"
    for name, color in (
        ("aqua", "#2aa198"), ("blue", "#4a90d9"), ("brown", "#9b7653"),
        ("grey", "#7c8793"), ("orange", "#d77a22"), ("pink", "#c75d9b"),
        ("purple", "#8b6bbd"), ("red", "#c95757"), ("teal", "#269e9e"),
        ("yellow", "#c59a18"),
    ):
        if name in theme_lower:
            accent = color
            break

    if dark:
        palette = {
            "background": "#2b2b2b", "panel": "#343434", "field": "#242424",
            "foreground": "#eeeeee", "muted": "#b8b8b8", "border": "#555555",
            "button": "#3d3d3d", "selected": "#365f7d", "warning_bg": "#6b4a22",
            "warning_selected": "#8a5f27", "warning_fg": "#ffd28a",
        }
    else:
        palette = {
            "background": "#f4f4f4", "panel": "#ffffff", "field": "#ffffff",
            "foreground": "#202020", "muted": "#666666", "border": "#b8b8b8",
            "button": "#f4f4f4", "selected": "#cfe8ff", "warning_bg": "#ffe8c7",
            "warning_selected": "#ffd39a", "warning_fg": "#8a4300",
        }
    palette.update({
        "dark": "1" if dark else "0", "accent": accent,
        "font": f'{font["family"]} {font["size"]}',
        "font_family": str(font["family"]), "font_size": str(font["size"]),
        "font_weight": str(font["weight"]), "font_slant": str(font["slant"]),
        "gtk_theme": gtk_theme,
    })
    return palette


def copy_combo(combo: Combo) -> Combo:
    return Combo(
        name=combo.name,
        positions=list(combo.positions),
        binding=combo.binding,
        timeout_ms=combo.timeout_ms,
        layers=list(combo.layers),
        require_prior_idle_ms=combo.require_prior_idle_ms,
        slow_release=combo.slow_release,
    )


def copy_conditional(item: ConditionalLayer) -> ConditionalLayer:
    return ConditionalLayer(item.name, list(item.if_layers), item.then_layer)


def _discover_layer_names(text: str) -> list[str]:
    """Discover layer order using braces only, even when angle-bracket syntax is damaged."""
    try:
        keymap_node = find_named_node(text, "keymap")
        if keymap_node is None:
            return []
        names = []
        for node in direct_child_nodes(text, keymap_node):
            body = text[node.open_brace + 1 : node.close_brace]
            if re.search(r"\bbindings\s*=", body):
                names.append(node.name)
        return names
    except KeymapError:
        return []


def _layer_reference(value: str, layer_names: list[str]) -> Optional[int]:
    value = value.strip().strip('"\'')
    value = re.sub(r"^[<\[(]+|[>\])]+$", "", value).strip()
    display = re.fullmatch(r"(-?\d+)\s*:\s*[A-Za-z_][A-Za-z0-9_-]*", value)
    if display:
        return int(display.group(1))
    parenthesized = re.fullmatch(r"(-?\d+)\s*\([^)]*\)", value)
    if parenthesized:
        return int(parenthesized.group(1))
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if value in layer_names:
        return layer_names.index(value)
    return None


def _parse_repair_list(value: str, layer_names: list[str], positions_only: bool = False) -> Optional[list[int]]:
    value = value.strip().strip("<>").strip().strip("[]").strip()
    value = re.sub(r"(-?\d+)\s*:\s*[A-Za-z_][A-Za-z0-9_-]*", r"\1", value)
    value = re.sub(r"(-?\d+)\s*\([^)]*\)", r"\1", value)
    tokens = [token for token in re.split(r"[\s,+]+", value) if token]
    if not tokens:
        return []
    result: list[int] = []
    for token in tokens:
        if re.fullmatch(r"-?\d+", token):
            result.append(int(token))
        elif not positions_only and token in layer_names:
            result.append(layer_names.index(token))
        else:
            return None
    return result


def is_orphaned_layout_fragment(line: str) -> bool:
    """Return true for uncommented remnants of the generated ASCII keymap."""
    stripped = line.strip()
    if not stripped or stripped.startswith(("//", "/*", "*")):
        return False
    # Valid devicetree syntax never contains three or more literal table-border
    # pipes on one source line. This also catches fragments truncated at either
    # edge by the older overlapping-edit bug.
    return stripped.count("|") >= 3


def repair_keymap_text(text: str) -> tuple[str, list[str]]:
    """Repair known malformed activation syntax emitted or accepted by version 1.2.0.

    Only activation expressions are changed. Layer bodies, ordinary bindings, comments,
    readable layouts, combo options, and unrelated devicetree source are left intact.
    """
    notes: list[str] = []

    # Some older rename operations inserted display-name at the same source
    # offset where the readable-layout comment was being replaced. The
    # overlapping edits could leave one fragment of the ASCII layout without
    # its leading "//", e.g. "| ENT |   | SPC | ... |". Devicetree reports
    # that line as "expected node name, property name, or '}'". Remove only
    # lines that are unmistakably orphaned layout rows. A valid devicetree
    # statement never begins and ends with an ASCII table border.
    repaired_layout_lines: list[str] = []
    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        candidate = line.rstrip("\r\n")
        if is_orphaned_layout_fragment(candidate):
            notes.append(
                f"Line {line_number}: removed orphaned human-readable layout fragment left by an older layer rename"
            )
            continue
        repaired_layout_lines.append(line)
    text = "".join(repaired_layout_lines)

    # Older editor builds emitted a non-schema `label = "...";` property in
    # zmk,keymap layer nodes in addition to display-name. ZMK supports
    # display-name, bindings, and sensor-bindings for those children; preserve
    # display-name and remove the obsolete label property during recovery.
    try:
        legacy_keymap = find_named_node(text, "keymap")
        legacy_layer_spans = direct_child_nodes(text, legacy_keymap) if legacy_keymap else []
    except KeymapError:
        legacy_layer_spans = []
    legacy_replacements: list[tuple[int, int, str, int]] = []
    for layer_span in legacy_layer_spans:
        body = text[layer_span.open_brace + 1 : layer_span.close_brace]
        for match in re.finditer(r'(?m)^[ \t]*label\s*=\s*"(?:\\.|[^"\\])*"\s*;[ \t]*(?:\n|$)', body):
            start = layer_span.open_brace + 1 + match.start()
            end = layer_span.open_brace + 1 + match.end()
            line_number = text.count("\n", 0, start) + 1
            legacy_replacements.append((start, end, "", line_number))
    for start, end, replacement, line_number in reversed(legacy_replacements):
        text = text[:start] + replacement + text[end:]
        notes.append(
            f"Line {line_number}: removed obsolete layer label property; display-name is preserved"
        )

    # Version 1.2.0 preselected positions 0 and 1 for every new generic combo.
    # Repair the exact paired brace pattern found in the affected generated file:
    # both named brace combos were left on Tab+Q instead of the symmetric inner
    # bottom-row pairs V+B and N+M.
    brace_specs = (
        ("L_Brace", "LBRC", "28 29", "V+B"),
        ("R_Brace", "RBRC", "30 31", "N+M"),
    )
    brace_matches: list[tuple[str, re.Match[str], str, str]] = []
    for name, output, positions, key_names in brace_specs:
        pattern = re.compile(
            rf"(?ms)(^[ \t]*{re.escape(name)}\s*\{{.*?\bkey-positions\s*=\s*<)0\s+1(>\s*;.*?\bbindings\s*=\s*<&kp\s+{output}>\s*;.*?^[ \t]*\}}\s*;)"
        )
        match = pattern.search(text)
        if match:
            brace_matches.append((name, match, positions, key_names))
    if len(brace_matches) == 2:
        for name, match, positions, key_names in reversed(brace_matches):
            line_number = text.count("\n", 0, match.start(1)) + 1
            text = text[: match.start(1)] + match.group(1) + positions + match.group(2) + text[match.end(2) :]
            notes.append(
                f"Line {line_number}: moved unintended {name} combo from Tab+Q to {key_names} (positions {positions})"
            )

    layer_names = _discover_layer_names(text)
    output_lines: list[str] = []
    in_block_comment = False

    def note(line_number: int, description: str):
        entry = f"Line {line_number}: {description}"
        if entry not in notes:
            notes.append(entry)

    def normalize_behavior(code: str, line_number: int) -> str:
        original = code
        code = re.sub(r"&bt\s+BTCLR\b", "&bt BT_CLR", code)
        code = re.sub(r"&bt\s+BTSEL\s+([0-9]+)\b", r"&bt BT_SEL \1", code)
        # Repair missing phandle marker only in an explicit bindings property.
        code = re.sub(
            r"(\bbindings\s*=\s*<\s*)(mo|tog|to)\b",
            lambda match: match.group(1) + "&" + match.group(2),
            code,
        )
        # Repair C/function and nested-cell notation: &mo(3), &mo <3>.
        code = re.sub(r"&(mo|tog|to)\s*\(\s*([^()]+?)\s*\)", r"&\1 \2", code)
        code = re.sub(r"&(mo|tog|to)\s*<\s*([^<>]+?)\s*>", r"&\1 \2", code)

        pattern = re.compile(
            r"&(mo|tog|to)\s+"
            r"(\"[^\"]+\"|'[^']+'|\[?\d+\]?\s*:\s*[A-Za-z_][A-Za-z0-9_-]*|"
            r"\[?\d+\]?\s*\([^)]*\)|\[?-?\d+\]?|[A-Za-z_][A-Za-z0-9_-]*)"
        )

        def replace_behavior(match: re.Match[str]) -> str:
            reference = _layer_reference(match.group(2), layer_names)
            if reference is None:
                return match.group(0)
            return f"&{match.group(1)} {reference}"

        code = pattern.sub(replace_behavior, code)
        # Repair a semicolon accidentally placed inside the binding cell.
        code = re.sub(r"(\bbindings\s*=\s*<&(?:mo|tog|to)\s+\d+)\s*;\s*>", r"\1>", code)
        # Repair a missing closing cell marker on a one-line activation binding.
        code = re.sub(r"(\bbindings\s*=\s*<&(?:mo|tog|to)\s+\d+)\s*;\s*$", r"\1>;", code)
        if code != original:
            if "BTCLR" in original or "BTSEL" in original:
                note(line_number, "repaired human-readable Bluetooth label used as ZMK source syntax")
            else:
                note(line_number, "repaired malformed layer-behavior binding")
        return code

    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        newline = "\n" if line.endswith("\n") else ""
        content = line[:-1] if newline else line
        if in_block_comment:
            output_lines.append(line)
            if "*/" in content:
                in_block_comment = False
            continue
        stripped = content.lstrip()
        if stripped.startswith("/*"):
            output_lines.append(line)
            if "*/" not in stripped[2:]:
                in_block_comment = True
            continue
        if stripped.startswith("//"):
            output_lines.append(line)
            continue

        comment_at = content.find("//")
        code = content if comment_at < 0 else content[:comment_at]
        comment = "" if comment_at < 0 else content[comment_at:]
        original_code = code

        property_match = re.search(r"\b(if-layers|then-layer|layers|key-positions)\s*=\s*(.*?)\s*;", code)
        if property_match:
            property_name, raw_value = property_match.groups()
            values = _parse_repair_list(raw_value, layer_names, positions_only=(property_name == "key-positions"))
            expected_count = 1 if property_name == "then-layer" else None
            if values is not None and (expected_count is None or len(values) == expected_count):
                canonical = f"{property_name} = <{' '.join(map(str, values))}>;"
                if property_match.group(0) != canonical:
                    code = code[: property_match.start()] + canonical + code[property_match.end() :]
                    note(line_number, f"normalized malformed {property_name} property")

        code = normalize_behavior(code, line_number)
        if code != original_code and not notes:
            note(line_number, "repaired activation syntax")
        output_lines.append(code + comment + newline)

    repaired = "".join(output_lines)
    return repaired, notes


def activation_syntax_issues(text: str) -> list[str]:
    """Return unresolved activation expressions that would not be valid DTS/ZMK syntax."""
    issues: list[str] = []
    in_block_comment = False
    for line_number, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip()
        if in_block_comment:
            if "*/" in line:
                in_block_comment = False
            continue
        if stripped.startswith("/*"):
            if "*/" not in stripped[2:]:
                in_block_comment = True
            continue
        if stripped.startswith("//"):
            continue
        code = line.split("//", 1)[0]
        bare = code.strip()
        if is_orphaned_layout_fragment(bare):
            issues.append(
                f"Line {line_number}: orphaned human-readable layout fragment is not valid devicetree syntax."
            )
        if re.match(r'^label\s*=\s*"', bare):
            issues.append(
                f"Line {line_number}: obsolete layer label property is not supported; use display-name."
            )
        for match in re.finditer(r"&(mo|tog|to)\b([^&;]*)", code):
            expression = normalize_binding(match.group(0)).rstrip("> ")
            if not re.fullmatch(r"&(mo|tog|to)\s+\d+", expression):
                issues.append(f'Line {line_number}: malformed layer behavior "{expression}".')
        for property_name in ("if-layers", "then-layer", "layers", "key-positions"):
            match = re.search(rf"\b{property_name}\s*=\s*([^;]+);", code)
            if match and not re.fullmatch(r"<\s*-?\d+(?:\s+-?\d+)*\s*>", match.group(1).strip()):
                issues.append(f"Line {line_number}: malformed {property_name} value.")
    return issues


def layer_behavior_error(binding: str) -> Optional[str]:
    binding = normalize_binding(binding)
    if re.match(r"&(mo|tog|to)\b", binding) and not re.fullmatch(r"&(mo|tog|to)\s+\d+", binding):
        return f'Layer behavior must use a numeric target, for example "&mo 2"; found "{binding}".'
    if re.fullmatch(r"&bt\s+BTCLR", binding):
        return 'Bluetooth clear must use the ZMK token "&bt BT_CLR", not the display label "BTCLR".'
    if re.fullmatch(r"&bt\s+BTSEL\s+\d+", binding):
        return 'Bluetooth profile selection must use "&bt BT_SEL N", not the display label "BTSEL".'
    return None


@dataclass
class KeymapDocument:
    source: str
    repair_notes: list[str]
    layers: list[Layer]
    combos: list[Combo]
    original_combos: list[Combo]
    conditional_layers: list[ConditionalLayer]
    original_conditional_layers: list[ConditionalLayer]
    keymap_node: NodeSpan
    combos_node: Optional[NodeSpan]
    conditional_layers_node: Optional[NodeSpan]

    @classmethod
    def from_text(cls, text: str, auto_repair: bool = True) -> "KeymapDocument":
        repair_notes: list[str] = []
        if auto_repair:
            text, repair_notes = repair_keymap_text(text)
        keymap_node = find_named_node(text, "keymap")
        if keymap_node is None:
            raise KeymapError('Could not find a "keymap { ... }" node.')

        layers: list[Layer] = []
        for node in direct_child_nodes(text, keymap_node):
            node_text = text[node.open_brace + 1 : node.close_brace]
            match = re.search(r"\bbindings\s*=\s*<(?P<body>.*?)>\s*;", node_text, re.S)
            if not match:
                continue
            body_start = node.open_brace + 1 + match.start("body")
            body_end = node.open_brace + 1 + match.end("body")
            bindings = parse_bindings(match.group("body"))
            if len(bindings) != KEY_COUNT:
                raise KeymapError(
                    f'Layer "{node.name}" has {len(bindings)} bindings; a Corne layer must have {KEY_COUNT}.'
                )
            line_start = text.rfind("\n", 0, body_start) + 1
            prop_line = text[line_start:body_start]
            indent_match = re.match(r"[ \t]*", prop_line)
            binding_indent = indent_match.group(0) if indent_match else ""

            prefix = text[node.open_brace + 1 : body_start]
            comment_match = None
            for candidate in re.finditer(
                r"(?m)^[ \t]*//\s*-{10,}[^\n]*\n(?:^[ \t]*//[^\n]*\n){4}", prefix
            ):
                comment_match = candidate
            comment_span = None
            if comment_match:
                comment_span = (
                    node.open_brace + 1 + comment_match.start(),
                    node.open_brace + 1 + comment_match.end(),
                )

            display_match = re.search(r'\bdisplay-name\s*=\s*"(?P<name>(?:\\.|[^"\\])*)"\s*;', node_text)
            display_name = node.name
            display_name_span = None
            if display_match:
                display_name = display_match.group("name")
                display_name_span = (
                    node.open_brace + 1 + display_match.start("name"),
                    node.open_brace + 1 + display_match.end("name"),
                )

            layers.append(
                Layer(
                    name=node.name,
                    original_name=node.name,
                    display_name=display_name,
                    original_display_name=display_name if display_match else None,
                    display_name_span=display_name_span,
                    bindings=bindings,
                    original_bindings=list(bindings),
                    binding_body_span=(body_start, body_end),
                    comment_span=comment_span,
                    binding_indent=binding_indent,
                    node_start=node.start,
                    node_open_brace=node.open_brace,
                )
            )

        if not layers:
            raise KeymapError("No layer nodes with bindings were found in the keymap node.")

        used_identifiers: set[str] = set()
        for layer_index, layer in enumerate(layers):
            visible_name = layer.display_name or layer.original_name or layer.name
            normalized_name = unique_layer_identifier(visible_name, used_identifiers, layer_index)
            used_identifiers.add(normalized_name)
            if layer.name != normalized_name:
                if auto_repair:
                    repair_notes.append(
                        f'Layer {layer_index}: normalized devicetree identifier "{layer.name}" to '
                        f'"{normalized_name}" while preserving display-name "{visible_name}"'
                    )
                    layer.name = normalized_name
                    layer.display_name = visible_name
                # With auto_repair disabled, validation below rejects it.

        combos_node = find_named_node(text, "combos")
        combos = parse_combos(text, combos_node) if combos_node else []
        original_combos = [copy_combo(combo) for combo in combos]
        conditional_layers_node = find_named_node(text, "conditional_layers")
        conditional_layers = parse_conditional_layers(text, conditional_layers_node) if conditional_layers_node else []
        original_conditional_layers = [copy_conditional(item) for item in conditional_layers]
        document = cls(
            text, repair_notes, layers, combos, original_combos, conditional_layers, original_conditional_layers,
            keymap_node, combos_node, conditional_layers_node,
        )
        errors = document.validate()
        if errors:
            raise KeymapError("\n".join(errors))
        return document

    @classmethod
    def from_file(cls, path: Path) -> "KeymapDocument":
        try:
            return cls.from_text(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError as exc:
            raise KeymapError(f"The file is not valid UTF-8: {exc}") from exc

    def validate(self) -> list[str]:
        errors: list[str] = []
        if not self.layers:
            errors.append("The file contains no editable layers.")
        layer_names: set[str] = set()
        for layer in self.layers:
            if not SAFE_LAYER_IDENTIFIER.fullmatch(layer.name):
                errors.append(
                    f'Layer identifier "{layer.name}" is not normalized. Use lowercase letters, numbers, and underscores only.'
                )
            if layer.name in layer_names:
                errors.append(f'Duplicate layer name "{layer.name}".')
            layer_names.add(layer.name)
            if len(layer.bindings) != KEY_COUNT:
                errors.append(f'{layer.name}: expected {KEY_COUNT} bindings, found {len(layer.bindings)}.')
            for index, binding in enumerate(layer.bindings):
                if not binding.strip().startswith("&"):
                    errors.append(f'{layer.name}, key {index}: binding must start with "&".')
                behavior_error = layer_behavior_error(binding)
                if behavior_error:
                    errors.append(f"{layer.name}, key {index}: {behavior_error}")
        seen: set[str] = set()
        for combo in self.combos:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", combo.name):
                errors.append(f'Combo name "{combo.name}" is not a valid devicetree node name.')
            if combo.name in seen:
                errors.append(f'Duplicate combo name "{combo.name}".')
            seen.add(combo.name)
            if len(set(combo.positions)) < 2:
                errors.append(f'Combo "{combo.name}" must use at least two different key positions.')
            if any(position < 0 or position >= KEY_COUNT for position in combo.positions):
                errors.append(f'Combo "{combo.name}" has a position outside 0-{KEY_COUNT - 1}.')
            if not combo.binding.strip().startswith("&"):
                errors.append(f'Combo "{combo.name}" binding must start with "&".')
            behavior_error = layer_behavior_error(combo.binding)
            if behavior_error:
                errors.append(f'Combo "{combo.name}": {behavior_error}')
            if combo.timeout_ms <= 0:
                errors.append(f'Combo "{combo.name}" timeout must be greater than zero.')
            if combo.require_prior_idle_ms is not None and combo.require_prior_idle_ms < 0:
                errors.append(f'Combo "{combo.name}" prior-idle value cannot be negative.')
            if any(layer < 0 or layer >= len(self.layers) for layer in combo.layers):
                errors.append(f'Combo "{combo.name}" references a layer outside 0-{len(self.layers) - 1}.')
        conditional_names: set[str] = set()
        for item in self.conditional_layers:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", item.name):
                errors.append(f'Conditional activation name "{item.name}" is not a valid devicetree node name.')
            if item.name in conditional_names:
                errors.append(f'Duplicate conditional activation name "{item.name}".')
            conditional_names.add(item.name)
            if len(item.if_layers) < 2 or len(set(item.if_layers)) != len(item.if_layers):
                errors.append(f'Conditional activation "{item.name}" must contain at least two different input layers.')
            if any(layer < 0 or layer >= len(self.layers) for layer in item.if_layers):
                errors.append(f'Conditional activation "{item.name}" has an input outside 0-{len(self.layers) - 1}.')
            if item.then_layer < 0 or item.then_layer >= len(self.layers):
                errors.append(f'Conditional activation "{item.name}" has a target outside 0-{len(self.layers) - 1}.')
            if item.then_layer in item.if_layers:
                errors.append(f'Conditional activation "{item.name}" cannot target one of its input layers.')
        errors.extend(activation_syntax_issues(self.source))
        return errors

    def add_layer(self, name: str, bindings: Optional[list[str]] = None) -> int:
        """Append a layer, preserving all existing numeric layer indexes."""
        display_name = name.strip()
        if not display_name:
            raise KeymapError("Layer display name cannot be empty.")
        if any(layer.display_name == display_name for layer in self.layers):
            raise KeymapError(f'A layer displayed as "{display_name}" already exists.')
        used = {layer.name for layer in self.layers}
        identifier = unique_layer_identifier(display_name, used, len(self.layers))
        new_bindings = list(bindings) if bindings is not None else ["&trans"] * KEY_COUNT
        if len(new_bindings) != KEY_COUNT:
            raise KeymapError(f"A Corne layer must contain exactly {KEY_COUNT} bindings.")
        child_indent = node_indent(self.source, self.keymap_node.start) + "    "
        self.layers.append(
            Layer(
                name=identifier,
                original_name=None,
                display_name=display_name,
                original_display_name=None,
                display_name_span=None,
                bindings=new_bindings,
                original_bindings=[],
                binding_body_span=None,
                comment_span=None,
                binding_indent=child_indent + "    ",
                node_start=None,
                node_open_brace=None,
            )
        )
        return len(self.layers) - 1

    def rename_layer(self, index: int, new_name: str):
        if index < 0 or index >= len(self.layers):
            raise KeymapError(f"Layer index {index} is outside 0-{len(self.layers) - 1}.")
        display_name = new_name.strip()
        if not display_name:
            raise KeymapError("Layer display name cannot be empty.")
        if any(
            layer.display_name == display_name
            for other_index, layer in enumerate(self.layers)
            if other_index != index
        ):
            raise KeymapError(f'A layer displayed as "{display_name}" already exists.')
        used = {layer.name for other_index, layer in enumerate(self.layers) if other_index != index}
        identifier = unique_layer_identifier(display_name, used, index)
        self.layers[index].name = identifier
        self.layers[index].display_name = display_name

    def render(self) -> str:
        errors = self.validate()
        if errors:
            raise KeymapError("\n".join(errors))

        replacements: list[tuple[int, int, str]] = []
        layer_names = [layer.display_name for layer in self.layers]
        any_renamed = any(
            layer.original_name is not None and layer.name != layer.original_name
            for layer in self.layers
        )
        added_layers: list[Layer] = []
        for layer in self.layers:
            if layer.node_start is None:
                added_layers.append(layer)
                continue
            if layer.original_name is not None and layer.name != layer.original_name:
                replacements.append((layer.node_start, layer.node_start + len(layer.original_name), layer.name))
                if layer.display_name_span:
                    replacements.append((*layer.display_name_span, escape_dts_string(layer.display_name)))
                else:
                    # Insert immediately before the bindings property. This
                    # avoids overlapping the readable-layout replacement when
                    # that comment begins directly after the opening brace.
                    assert layer.binding_body_span is not None
                    insert_at = line_start(self.source, layer.binding_body_span[0])
                    property_indent = layer.binding_indent
                    replacements.append(
                        (
                            insert_at, insert_at,
                            f'{property_indent}display-name = "{escape_dts_string(layer.display_name)}";\n',
                        )
                    )
            bindings_changed = layer.bindings != layer.original_bindings
            if bindings_changed:
                body = render_bindings_body(layer.bindings, layer.binding_indent)
                assert layer.binding_body_span is not None
                replacements.append((*layer.binding_body_span, body))
            if bindings_changed or any_renamed:
                layout = render_layout_comment(layer.bindings, layer.binding_indent, layer_names)
                if layer.comment_span:
                    replacements.append((*layer.comment_span, layout))
                else:
                    assert layer.binding_body_span is not None
                    insert_at = line_start(self.source, layer.binding_body_span[0])
                    replacements.append((insert_at, insert_at, layout))

        if added_layers:
            child_indent = node_indent(self.source, self.keymap_node.start) + "    "
            insertion = "\n" + "\n\n".join(
                render_layer_node(layer.name, layer.display_name, layer.bindings, child_indent, layer_names)
                for layer in added_layers
            ) + "\n"
            replacements.append((line_start(self.source, self.keymap_node.close_brace),) * 2 + (insertion,))

        combos_changed = self.combos != self.original_combos
        if self.combos_node:
            if combos_changed:
                replacements.append(
                    (
                        line_start(self.source, self.combos_node.start),
                        self.combos_node.end,
                        render_combos_node(self.combos, node_indent(self.source, self.combos_node.start)),
                    )
                )
        elif self.combos:
            insertion = render_combos_node(self.combos, node_indent(self.source, self.keymap_node.start)) + "\n\n"
            insertion_point = line_start(self.source, self.keymap_node.start)
            replacements.append((insertion_point, insertion_point, insertion))

        conditionals_changed = self.conditional_layers != self.original_conditional_layers
        if self.conditional_layers_node:
            if conditionals_changed:
                replacements.append(
                    (
                        line_start(self.source, self.conditional_layers_node.start),
                        self.conditional_layers_node.end,
                        render_conditional_layers_node(
                            self.conditional_layers,
                            node_indent(self.source, self.conditional_layers_node.start),
                        ),
                    )
                )
        elif self.conditional_layers:
            insertion = render_conditional_layers_node(
                self.conditional_layers, node_indent(self.source, self.keymap_node.start)
            ) + "\n\n"
            insertion_point = line_start(self.source, self.keymap_node.start)
            replacements.append((insertion_point, insertion_point, insertion))

        output = self.source
        for start, end, replacement in sorted(replacements, key=lambda item: item[0], reverse=True):
            output = output[:start] + replacement + output[end:]
        return output


def scan_tokens(text: str, start: int = 0, end: Optional[int] = None) -> list[tuple[str, str, int, int]]:
    """Return significant devicetree tokens while ignoring comments and quoted text."""
    if end is None:
        end = len(text)
    tokens: list[tuple[str, str, int, int]] = []
    i = start
    while i < end:
        ch = text[i]
        if ch.isspace():
            i += 1
            continue
        if text.startswith("//", i):
            newline = text.find("\n", i + 2, end)
            i = end if newline < 0 else newline + 1
            continue
        if text.startswith("/*", i):
            close = text.find("*/", i + 2, end)
            i = end if close < 0 else close + 2
            continue
        if ch == '"':
            j = i + 1
            while j < end:
                if text[j] == "\\":
                    j += 2
                elif text[j] == '"':
                    j += 1
                    break
                else:
                    j += 1
            tokens.append(("STRING", text[i:j], i, j))
            i = j
            continue
        if ch.isalpha() or ch == "_":
            j = i + 1
            while j < end and (text[j].isalnum() or text[j] in "_-@,."):
                j += 1
            tokens.append(("IDENT", text[i:j], i, j))
            i = j
            continue
        tokens.append((ch, ch, i, i + 1))
        i += 1
    return tokens


def _node_from_token(tokens: list[tuple[str, str, int, int]], index: int, text: str) -> Optional[NodeSpan]:
    if index + 1 >= len(tokens) or tokens[index][0] != "IDENT" or tokens[index + 1][0] != "{":
        return None
    depth = 0
    for cursor in range(index + 1, len(tokens)):
        token_type = tokens[cursor][0]
        if token_type == "{":
            depth += 1
        elif token_type == "}":
            depth -= 1
            if depth == 0:
                close_brace = tokens[cursor][2]
                end = tokens[cursor][3]
                if cursor + 1 < len(tokens) and tokens[cursor + 1][0] == ";":
                    end = tokens[cursor + 1][3]
                return NodeSpan(tokens[index][1], tokens[index][2], tokens[index + 1][2], close_brace, end)
    raise KeymapError(f'Unclosed node "{tokens[index][1]}".')


def find_named_node(text: str, name: str) -> Optional[NodeSpan]:
    tokens = scan_tokens(text)
    for index, token in enumerate(tokens[:-1]):
        if token[0] == "IDENT" and token[1] == name and tokens[index + 1][0] == "{":
            return _node_from_token(tokens, index, text)
    return None


def direct_child_nodes(text: str, parent: NodeSpan) -> list[NodeSpan]:
    tokens = scan_tokens(text, parent.open_brace + 1, parent.close_brace)
    nodes: list[NodeSpan] = []
    index = 0
    depth = 0
    while index < len(tokens) - 1:
        token = tokens[index]
        if depth == 0 and token[0] == "IDENT" and tokens[index + 1][0] == "{":
            node = _node_from_token(tokens, index, text)
            if node:
                nodes.append(node)
                while index < len(tokens) and tokens[index][2] < node.end:
                    index += 1
                continue
        if token[0] == "{":
            depth += 1
        elif token[0] == "}":
            depth -= 1
        index += 1
    return nodes


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", " ", text)
    return text


def parse_bindings(body: str) -> list[str]:
    clean = strip_comments(body)
    starts = [match.start() for match in re.finditer(r"&[A-Za-z_][A-Za-z0-9_-]*", clean)]
    bindings: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(clean)
        value = re.sub(r"\s+", " ", clean[start:end]).strip()
        if value:
            bindings.append(value)
    return bindings


def parse_int_list(value: str) -> list[int]:
    return [int(item) for item in re.findall(r"-?\d+", strip_comments(value))]


def parse_combos(text: str, combos_node: NodeSpan) -> list[Combo]:
    combos: list[Combo] = []
    for node in direct_child_nodes(text, combos_node):
        body = text[node.open_brace + 1 : node.close_brace]
        positions_match = re.search(r"\bkey-positions\s*=\s*<(.*?)>\s*;", body, re.S)
        binding_match = re.search(r"\bbindings\s*=\s*<(.*?)>\s*;", body, re.S)
        if not positions_match or not binding_match:
            continue
        timeout_match = re.search(r"\btimeout-ms\s*=\s*<\s*(\d+)\s*>\s*;", body)
        layers_match = re.search(r"\blayers\s*=\s*<(.*?)>\s*;", body, re.S)
        idle_match = re.search(r"\brequire-prior-idle-ms\s*=\s*<\s*(\d+)\s*>\s*;", body)
        parsed_bindings = parse_bindings(binding_match.group(1))
        binding = " ".join(parsed_bindings) if parsed_bindings else re.sub(r"\s+", " ", binding_match.group(1)).strip()
        combos.append(
            Combo(
                name=node.name,
                positions=parse_int_list(positions_match.group(1)),
                binding=binding,
                timeout_ms=int(timeout_match.group(1)) if timeout_match else 50,
                layers=parse_int_list(layers_match.group(1)) if layers_match else [],
                require_prior_idle_ms=int(idle_match.group(1)) if idle_match else None,
                slow_release=bool(re.search(r"\bslow-release\s*;", body)),
            )
        )
    return combos


def parse_conditional_layers(text: str, conditional_node: NodeSpan) -> list[ConditionalLayer]:
    items: list[ConditionalLayer] = []
    for node in direct_child_nodes(text, conditional_node):
        body = text[node.open_brace + 1 : node.close_brace]
        if_match = re.search(r"\bif-layers\s*=\s*<(.*?)>\s*;", body, re.S)
        then_match = re.search(r"\bthen-layer\s*=\s*<\s*(-?\d+)\s*>\s*;", body)
        if not if_match or not then_match:
            continue
        items.append(
            ConditionalLayer(
                name=node.name,
                if_layers=parse_int_list(if_match.group(1)),
                then_layer=int(then_match.group(1)),
            )
        )
    return items


def line_start(text: str, position: int) -> int:
    return text.rfind("\n", 0, position) + 1


def node_indent(text: str, position: int) -> str:
    beginning = line_start(text, position)
    match = re.match(r"[ \t]*", text[beginning:position])
    return match.group(0) if match else ""


def normalize_binding(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip())


def render_bindings_body(bindings: list[str], indent: str) -> str:
    rows = [bindings[part] for part in ROW_SLICES]
    child_indent = indent + "    "
    lines = [child_indent + "  ".join(normalize_binding(binding) for binding in row) for row in rows]
    return "\n" + "\n".join(lines) + "\n" + indent


def render_layer_node(
    name: str, display_name: str, bindings: list[str], indent: str, layer_names: list[str]
) -> str:
    property_indent = indent + "    "
    layout = render_layout_comment(bindings, property_indent, layer_names).rstrip("\n")
    body = render_bindings_body(bindings, property_indent)
    return (
        f"{indent}{name} {{\n"
        f'{property_indent}display-name = "{escape_dts_string(display_name)}";\n'
        f"{layout}\n"
        f"{property_indent}bindings = <{body}>;\n"
        f"{indent}}};"
    )


LAYER_BEHAVIORS = {
    "Momentary (while held)": "mo",
    "Toggle on/off": "tog",
    "Switch to layer": "to",
}


def layer_behavior_binding(behavior_name: str, layer_index: int) -> str:
    behavior = LAYER_BEHAVIORS.get(behavior_name)
    if behavior is None:
        raise KeymapError(f'Unknown layer-key behavior "{behavior_name}".')
    if layer_index < 0:
        raise KeymapError("Target layer index cannot be negative.")
    return f"&{behavior} {layer_index}"


KEY_LABELS = {
    "TAB": "TAB", "BSPC": "BSPC", "BACKSPACE": "BSPC", "LCTRL": "CTRL", "RCTRL": "CTRL",
    "LSHFT": "SHFT", "RSHFT": "SHFT", "LALT": "ALT", "RALT": "ALT", "LGUI": "GUI", "RGUI": "GUI",
    "RET": "ENT", "RETURN": "ENT", "ENTER": "ENT", "SPACE": "SPC", "ESC": "ESC", "DEL": "DEL",
    "SEMI": ";", "SQT": "'", "COMMA": ",", "DOT": ".", "FSLH": "/", "BSLH": "\\",
    "EXCL": "!", "AT": "@", "HASH": "#", "DLLR": "$", "PRCNT": "%", "CARET": "^", "AMPS": "&",
    "ASTRK": "*", "LPAR": "(", "RPAR": ")", "MINUS": "-", "EQUAL": "=", "LBKT": "[", "RBKT": "]",
    "GRAVE": "`", "UNDER": "_", "PLUS": "+", "LBRC": "{", "RBRC": "}", "PIPE": "|", "TILDE": "~",
    "C_PLAY_PAUSE": "PLAY", "C_MUTE": "MUTE", "C_VOL_DN": "VOLD", "C_VOL_UP": "VOLU",
    "LEFT": "LFT", "RIGHT": "RGT", "UP": "UP", "DOWN": "DWN", "HOME": "HOME", "END": "END",
    "PG_UP": "PGUP", "PG_DN": "PGDN", "CAPS": "CAPS",
}

ACTION_LABELS = {
    "RGB_SAD": "SAD", "RGB_SAI": "SAI", "RGB_EFF": "EFF", "RGB_BRI": "BRI", "RGB_BRD": "BRD", "RGB_HUI": "HUI",
    "SCRL_LEFT": "SCL", "SCRL_DOWN": "SCD", "SCRL_UP": "SCU", "SCRL_RIGHT": "SCR",
    "MOVE_LEFT": "MOUL", "MOVE_DOWN": "MOUD", "MOVE_UP": "MOUU", "MOVE_RIGHT": "MOUR",
    "OUT_USB": "USB", "OUT_BLE": "BLE", "MCLK": "MCLK", "LCLK": "LCLK", "RCLK": "RCLK",
}


def layer_short_name(name: str) -> str:
    base = re.sub(r"_layer$", "", name, flags=re.I).upper()
    preferred = {"DEFAULT": "BASE", "LOWER": "LWR", "RAISE": "RSE", "ADJUST": "ADJ"}
    return preferred.get(base, base[:6])


def binding_label(binding: str, layer_names: Optional[list[str]] = None, max_length: int = 7) -> str:
    parts = normalize_binding(binding).split()
    if not parts:
        return ""
    behavior = parts[0]
    if behavior in ("&trans", "&none"):
        return ""
    if behavior == "&kp" and len(parts) > 1:
        code = parts[1]
        if re.fullmatch(r"N\d", code):
            label = code[1:]
        else:
            label = KEY_LABELS.get(code, code)
    elif behavior in ("&mo", "&tog", "&to") and len(parts) > 1:
        try:
            layer_index = int(parts[1])
            target = layer_short_name(layer_names[layer_index]) if layer_names and layer_index < len(layer_names) else str(layer_index)
            prefix = {"&mo": "", "&tog": "TG:", "&to": "TO:"}[behavior]
            label = prefix + target
        except (ValueError, IndexError):
            label = behavior.lstrip("&").upper()
    elif behavior == "&bt" and len(parts) > 1:
        label = f"BT{parts[2]}" if parts[1] == "BT_SEL" and len(parts) > 2 else ("BTCLR" if parts[1] == "BT_CLR" else parts[1])
    elif len(parts) > 1 and parts[1] in ACTION_LABELS:
        label = ACTION_LABELS[parts[1]]
    else:
        label = behavior.lstrip("&").upper()
        if len(parts) > 1:
            label += ":" + parts[1]
    return label[:max_length]


MODIFIER_WRAPPERS = {
    "LC": "CTRL", "RC": "CTRL", "LCTRL": "CTRL", "RCTRL": "CTRL",
    "LS": "SHIFT", "RS": "SHIFT", "LSHFT": "SHIFT", "RSHFT": "SHIFT",
    "LA": "ALT", "RA": "ALT", "LALT": "ALT", "RALT": "ALT",
    "LG": "WIN", "RG": "WIN", "LGUI": "WIN", "RGUI": "WIN",
}

SIMPLE_MODIFIER_CODES = {
    "LCTRL": "CTRL", "RCTRL": "CTRL", "LSHFT": "SHIFT", "RSHFT": "SHIFT",
    "LALT": "ALT", "RALT": "ALT", "LGUI": "WIN", "RGUI": "WIN",
}

COMMON_SHORTCUTS: dict[tuple[frozenset[str], str], tuple[str, str]] = {
    (frozenset({"CTRL"}), "C"): ("Ctrl+C", "Copy, or interrupt a terminal process"),
    (frozenset({"CTRL"}), "X"): ("Ctrl+X", "Cut"),
    (frozenset({"CTRL"}), "V"): ("Ctrl+V", "Paste"),
    (frozenset({"CTRL"}), "Z"): ("Ctrl+Z", "Undo"),
    (frozenset({"CTRL"}), "Y"): ("Ctrl+Y", "Redo"),
    (frozenset({"CTRL"}), "A"): ("Ctrl+A", "Select all"),
    (frozenset({"CTRL"}), "S"): ("Ctrl+S", "Save"),
    (frozenset({"CTRL"}), "P"): ("Ctrl+P", "Print"),
    (frozenset({"CTRL"}), "F"): ("Ctrl+F", "Find"),
    (frozenset({"CTRL"}), "W"): ("Ctrl+W", "Close tab or window"),
    (frozenset({"CTRL"}), "T"): ("Ctrl+T", "New browser or terminal tab"),
    (frozenset({"CTRL"}), "N"): ("Ctrl+N", "New document or window"),
    (frozenset({"CTRL"}), "O"): ("Ctrl+O", "Open"),
    (frozenset({"CTRL"}), "R"): ("Ctrl+R", "Refresh or reverse search"),
    (frozenset({"CTRL", "SHIFT"}), "ESC"): ("Ctrl+Shift+Esc", "Open Task Manager or system monitor"),
    (frozenset({"CTRL", "ALT"}), "DEL"): ("Ctrl+Alt+Del", "Open the system security screen"),
    (frozenset({"ALT"}), "TAB"): ("Alt+Tab", "Switch applications"),
    (frozenset({"ALT"}), "F4"): ("Alt+F4", "Close the active window"),
    (frozenset({"WIN"}), "LEFT"): ("Win/Super+Left", "Snap the active window left"),
    (frozenset({"WIN"}), "RIGHT"): ("Win/Super+Right", "Snap the active window right"),
    (frozenset({"WIN"}), "UP"): ("Win/Super+Up", "Maximize the active window"),
    (frozenset({"WIN"}), "DOWN"): ("Win/Super+Down", "Restore or minimize the active window"),
    (frozenset({"WIN"}), "L"): ("Win/Super+L", "Lock the session"),
    (frozenset({"WIN"}), "D"): ("Win/Super+D", "Show the desktop"),
    (frozenset({"WIN"}), "E"): ("Win/Super+E", "Open the file manager"),
    (frozenset({"WIN"}), "R"): ("Win/Super+R", "Open the Run dialog or launcher"),
    (frozenset({"WIN"}), "I"): ("Win/Super+I", "Open system settings"),
    (frozenset({"WIN"}), "TAB"): ("Win/Super+Tab", "Open task or workspace view"),
}


def _normalize_shortcut_key(code: str) -> str:
    code = code.strip().upper()
    if re.fullmatch(r"N\d", code):
        return code[1:]
    aliases = {"RETURN": "ENTER", "RET": "ENTER", "BSPC": "BACKSPACE", "PG_UP": "PAGEUP", "PG_DN": "PAGEDOWN"}
    return aliases.get(code, code)


def _modified_shortcut(binding: str) -> Optional[tuple[frozenset[str], str]]:
    parts = normalize_binding(binding).split(maxsplit=1)
    if len(parts) != 2 or parts[0] != "&kp":
        return None
    expression = parts[1].strip()
    modifiers: set[str] = set()
    while True:
        match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\((.*)\)", expression)
        if not match:
            break
        wrapper = match.group(1).upper()
        modifier = MODIFIER_WRAPPERS.get(wrapper)
        if modifier is None:
            return None
        modifiers.add(modifier)
        expression = match.group(2).strip()
    if not modifiers or not re.fullmatch(r"[A-Za-z0-9_]+", expression):
        return None
    return frozenset(modifiers), _normalize_shortcut_key(expression)


def _simple_shortcut_component(binding: str) -> Optional[tuple[str, str]]:
    parts = normalize_binding(binding).split()
    if len(parts) != 2 or parts[0] != "&kp":
        return None
    code = parts[1].upper()
    if code in SIMPLE_MODIFIER_CODES:
        return "modifier", SIMPLE_MODIFIER_CODES[code]
    return "key", _normalize_shortcut_key(code)


def _common_shortcut(modifiers: frozenset[str], key: str) -> Optional[tuple[str, str]]:
    return COMMON_SHORTCUTS.get((modifiers, _normalize_shortcut_key(key)))


def key_binding_shortcut_warning(
    binding: str, layer_index: int, layer_name: str, position: int
) -> Optional[ShortcutWarning]:
    shortcut = _modified_shortcut(binding)
    if shortcut is None:
        return None
    known = _common_shortcut(*shortcut)
    if known is None:
        return None
    display, action = known
    return ShortcutWarning(
        source=f"Layer {layer_index} {layer_name}, key {position}",
        shortcut=display,
        message=f'This key sends {display}, a common Windows/Linux shortcut ({action}).',
        layer_index=layer_index,
        key_position=position,
    )


def combo_shortcut_warnings(combo: Combo, document: "KeymapDocument", combo_index: int) -> list[ShortcutWarning]:
    warnings: list[ShortcutWarning] = []
    output = _modified_shortcut(combo.binding)
    if output:
        known = _common_shortcut(*output)
        if known:
            display, action = known
            affected_layers = combo.layers or list(range(len(document.layers)))
            for layer_index in affected_layers:
                if layer_index < 0 or layer_index >= len(document.layers):
                    continue
                for position in combo.positions:
                    if position < 0 or position >= KEY_COUNT:
                        continue
                    warnings.append(ShortcutWarning(
                        source=(
                            f"Layer {layer_index} {document.layers[layer_index].display_name}, "
                            f"key {position}; combo {combo.name}"
                        ),
                        shortcut=display,
                        message=(
                            f'Combo "{combo.name}" at this key outputs {display}, '
                            f"a common Windows/Linux shortcut ({action})."
                        ),
                        layer_index=layer_index,
                        key_position=position,
                        combo_index=combo_index,
                    ))

    applicable_layers = combo.layers or list(range(len(document.layers)))
    for layer_index in applicable_layers:
        if layer_index < 0 or layer_index >= len(document.layers):
            continue
        modifiers: set[str] = set()
        keys: list[str] = []
        for position in combo.positions:
            if position < 0 or position >= KEY_COUNT:
                continue
            component = _simple_shortcut_component(document.layers[layer_index].bindings[position])
            if component is None:
                continue
            kind, value = component
            if kind == "modifier":
                modifiers.add(value)
            else:
                keys.append(value)
        if modifiers and len(keys) == 1:
            known = _common_shortcut(frozenset(modifiers), keys[0])
            if known:
                display, action = known
                for position in combo.positions:
                    warnings.append(ShortcutWarning(
                        source=(
                            f"Layer {layer_index} {document.layers[layer_index].display_name}, "
                            f"key {position}; combo {combo.name}"
                        ),
                        shortcut=display,
                        message=(
                            f'Combo "{combo.name}" uses this key in trigger positions that overlap '
                            f"{display} ({action}); the combo may intercept that operating-system shortcut."
                        ),
                        layer_index=layer_index,
                        key_position=position,
                        combo_index=combo_index,
                    ))
    return warnings


def _layer_behavior_target(binding: str) -> Optional[int]:
    match = re.fullmatch(r"&(mo|tog|to)\s+(\d+)", normalize_binding(binding))
    return int(match.group(2)) if match else None


def _binding_for_active_layers(
    document: "KeymapDocument", active_layers: Iterable[int], position: int
) -> str:
    """Resolve one physical position using ZMK's higher-layer-first precedence."""
    valid_layers = sorted(
        {layer for layer in active_layers if 0 <= layer < len(document.layers)},
        reverse=True,
    )
    if 0 not in valid_layers and document.layers:
        valid_layers.append(0)
    for layer_index in valid_layers:
        binding = normalize_binding(document.layers[layer_index].bindings[position])
        if binding != "&trans":
            return binding
    return "&trans"


def physical_layer_activation_warnings(document: "KeymapDocument") -> list[ShortcutWarning]:
    """Detect shortcuts broken by modifier+layer-key activation combos.

    Example: positions 36 and 37 are GUI and Lower on the base layer. If a
    combo on those positions activates layer 4, the physical positions that
    normally provide Lower's arrows are instead resolved through layer 4.
    Layer-4 functions at those positions therefore interfere with Win+Arrow.
    """
    warnings: list[ShortcutWarning] = []
    seen: set[tuple[int, int, int, str]] = set()
    layer_names = [layer.display_name for layer in document.layers]

    for combo_index, combo in enumerate(document.combos):
        target_layer = _layer_behavior_target(combo.binding)
        if target_layer is None or target_layer >= len(document.layers):
            continue
        activation_contexts = combo.layers or list(range(len(document.layers)))
        for context_layer in activation_contexts:
            if context_layer < 0 or context_layer >= len(document.layers):
                continue
            modifiers: set[str] = set()
            source_layers: set[int] = set()
            for trigger_position in combo.positions:
                if trigger_position < 0 or trigger_position >= KEY_COUNT:
                    continue
                trigger_binding = _binding_for_active_layers(
                    document, [context_layer, 0], trigger_position
                )
                component = _simple_shortcut_component(trigger_binding)
                if component and component[0] == "modifier":
                    modifiers.add(component[1])
                source_target = _layer_behavior_target(trigger_binding)
                if source_target is not None and source_target != target_layer:
                    source_layers.add(source_target)

            if not modifiers or not source_layers:
                continue

            for source_layer in source_layers:
                if source_layer < 0 or source_layer >= len(document.layers):
                    continue
                intended_active = [source_layer, context_layer, 0]
                actual_active = [target_layer, context_layer, 0]
                for position in range(KEY_COUNT):
                    intended_binding = _binding_for_active_layers(
                        document, intended_active, position
                    )
                    component = _simple_shortcut_component(intended_binding)
                    if component is None or component[0] != "key":
                        continue
                    known = _common_shortcut(frozenset(modifiers), component[1])
                    if known is None:
                        continue
                    actual_binding = _binding_for_active_layers(
                        document, actual_active, position
                    )
                    if actual_binding == intended_binding:
                        continue
                    display, action = known
                    identity = (combo_index, target_layer, position, display)
                    if identity in seen:
                        continue
                    seen.add(identity)
                    actual_label = binding_label(actual_binding, layer_names, 14) or actual_binding
                    modifier_positions = "+".join(map(str, combo.positions))
                    warnings.append(ShortcutWarning(
                        source=(
                            f"Layer {target_layer} {document.layers[target_layer].display_name}, "
                            f"key {position} ({actual_label})"
                        ),
                        shortcut=display,
                        message=(
                            f'Activation combo "{combo.name}" uses physical positions {modifier_positions}, '
                            f"which combine a {display.split('+')[0]} modifier with layer "
                            f"{source_layer} {document.layers[source_layer].display_name}. "
                            f"This key normally supplies {display} ({action}), but layer {target_layer} "
                            f"resolves the same position as {actual_label}; the shortcut may not work."
                        ),
                        layer_index=target_layer,
                        key_position=position,
                        combo_index=combo_index,
                    ))
    return warnings


def analyze_shortcut_conflicts(document: "KeymapDocument") -> list[ShortcutWarning]:
    warnings: list[ShortcutWarning] = []
    for layer_index, layer in enumerate(document.layers):
        for position, binding in enumerate(layer.bindings):
            warning = key_binding_shortcut_warning(binding, layer_index, layer.display_name, position)
            if warning:
                warnings.append(warning)
    for combo_index, combo in enumerate(document.combos):
        warnings.extend(combo_shortcut_warnings(combo, document, combo_index))
    warnings.extend(physical_layer_activation_warnings(document))
    return warnings


def _pdf_text(value: str) -> str:
    value = value.encode("ascii", "replace").decode("ascii")
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


class _PDFPage:
    def __init__(self, width: float = 792, height: float = 612):
        self.width = width
        self.height = height
        self.commands: list[str] = []

    def rect(self, x: float, y: float, width: float, height: float, fill=(1, 1, 1), stroke=(0.18, 0.22, 0.28), line=0.8):
        self.commands.append(
            f"q {line:.2f} w {fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg "
            f"{stroke[0]:.3f} {stroke[1]:.3f} {stroke[2]:.3f} RG "
            f"{x:.2f} {y:.2f} {width:.2f} {height:.2f} re B Q"
        )

    def line(self, x1: float, y1: float, x2: float, y2: float, color=(0.10, 0.35, 0.55), width=1.2):
        self.commands.append(
            f"q {width:.2f} w {color[0]:.3f} {color[1]:.3f} {color[2]:.3f} RG "
            f"{x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S Q"
        )

    def text(self, x: float, y: float, value: str, size: float = 9, bold: bool = False, center: bool = False, color=(0.08, 0.10, 0.14)):
        safe = _pdf_text(value)
        if center:
            x -= len(safe) * size * (0.29 if bold else 0.27)
        font = "F2" if bold else "F1"
        self.commands.append(
            f"BT /{font} {size:.2f} Tf {color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg "
            f"1 0 0 1 {x:.2f} {y:.2f} Tm ({safe}) Tj ET"
        )

    def stream(self) -> bytes:
        return ("\n".join(self.commands) + "\n").encode("ascii")


def _write_pdf(path: Path, pages: list[_PDFPage]):
    objects: list[bytes] = []
    page_ids = [5 + index * 2 for index in range(len(pages))]
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>")
    for page_id, page in zip(page_ids, pages):
        content_id = page_id + 1
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page.width:.0f} {page.height:.0f}] "
                f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>"
            ).encode("ascii")
        )
        stream = page.stream()
        objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"endstream")

    output = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_number, obj in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f"{object_number} 0 obj\n".encode("ascii"))
        output.extend(obj)
        output.extend(b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii")
    )
    path.write_bytes(output)


def _key_rectangles(
    panel_x: float, panel_top: float, panel_width: float, panel_height: float
) -> dict[int, tuple[float, float, float, float]]:
    gap = 2.2
    side_padding = 9.0
    split_gap = max(19.0, panel_width * 0.075)
    available_width = panel_width - 2 * side_padding
    half_w = (available_width - split_gap) / 2
    key_w = (half_w - 5 * gap) / 6
    key_h = min(19.0, max(14.0, (panel_height - 55) / 4.5))
    left_x = panel_x + side_padding
    right_x = left_x + half_w + split_gap
    top_y = panel_top - 52
    rectangles: dict[int, tuple[float, float, float, float]] = {}
    for row in range(3):
        y = top_y - row * (key_h + gap)
        for column in range(6):
            rectangles[row * 12 + column] = (left_x + column * (key_w + gap), y, key_w, key_h)
            rectangles[row * 12 + 6 + column] = (right_x + column * (key_w + gap), y, key_w, key_h)
    thumb_y = top_y - 3 * (key_h + gap) - 1
    left_thumb_x = left_x + 2.45 * (key_w + gap)
    right_thumb_x = right_x + 0.55 * (key_w + gap)
    for offset in range(3):
        rectangles[36 + offset] = (left_thumb_x + offset * (key_w + gap), thumb_y, key_w, key_h)
        rectangles[39 + offset] = (right_thumb_x + offset * (key_w + gap), thumb_y, key_w, key_h)
    return rectangles


def _adjacent_combo_pair(first: int, second: int) -> bool:
    pair = tuple(sorted((first, second)))
    horizontal: set[tuple[int, int]] = set()
    vertical: set[tuple[int, int]] = set()
    for row_start in (0, 12, 24):
        for half_start in (row_start, row_start + 6):
            horizontal.update((position, position + 1) for position in range(half_start, half_start + 5))
    horizontal.update({(36, 37), (37, 38), (39, 40), (40, 41)})
    for position in range(24):
        vertical.add((position, position + 12))
    return pair in horizontal or pair in vertical


def _activation_summary(document: "KeymapDocument", layer_index: int) -> str:
    layer_names = [layer.display_name for layer in document.layers]
    details: list[str] = []
    for item in document.conditional_layers:
        if item.then_layer == layer_index:
            names = " + ".join(layer_short_name(layer_names[index]) for index in item.if_layers if index < len(layer_names))
            if names:
                details.append("when " + names + " are active")
    selectors = 0
    for layer in document.layers:
        selectors += sum(
            1 for binding in layer.bindings
            if re.fullmatch(rf"&(mo|tog|to)\s+{layer_index}", normalize_binding(binding))
        )
    pair_count = sum(
        1 for combo in document.combos
        if re.fullmatch(rf"&(mo|tog|to)\s+{layer_index}", normalize_binding(combo.binding)) and len(combo.positions) == 2
    )
    if selectors:
        details.append(f"{selectors} layer key{'s' if selectors != 1 else ''}")
    if pair_count:
        details.append(f"{pair_count} two-key activation{'s' if pair_count != 1 else ''}")
    return "Activation: " + "; ".join(details) if details else ""


def export_keymap_pdf(document: "KeymapDocument", path: Path, title: str = "CORNE 42-KEY KEYMAP"):
    """Create a printable, single-sided landscape cheat sheet."""
    errors = document.validate()
    if errors:
        raise KeymapError("Cannot export PDF until the keymap is valid:\n" + "\n".join(errors))
    layer_names = [layer.display_name for layer in document.layers]
    diagram_indices = list(range(len(document.layers)))
    default_omitted = False
    if len(diagram_indices) > 6:
        diagram_indices = diagram_indices[1:]
        default_omitted = True
    panel_count = max(1, len(diagram_indices))
    columns = 2 if panel_count <= 6 else 3
    rows = ceil(panel_count / columns)
    left_margin, right_margin = 36.0, 36.0
    content_top, content_bottom = 558.0, 37.0
    column_gap, row_gap = 12.0, 7.0
    panel_width = (792 - left_margin - right_margin - (columns - 1) * column_gap) / columns
    panel_height = (content_top - content_bottom - (rows - 1) * row_gap) / rows

    page = _PDFPage()
    page.text(36, 582, title, 16, bold=True)
    page.line(36, 573, 756, 573, width=1.4)

    for slot, layer_index in enumerate(diagram_indices):
            layer = document.layers[layer_index]
            column = slot % columns
            row = slot // columns
            panel_x = left_margin + column * (panel_width + column_gap)
            panel_top = content_top - row * (panel_height + row_gap)
            page.rect(
                panel_x, panel_top - panel_height, panel_width, panel_height,
                fill=(1, 1, 1), stroke=(0.78, 0.81, 0.84), line=0.55,
            )
            heading = f"LAYER {layer_index} - {layer.display_name.upper()}"
            page.text(panel_x + panel_width / 2, panel_top - 15, heading, 10.5, bold=True, center=True)
            summary = _activation_summary(document, layer_index)
            if summary:
                page.text(
                    panel_x + panel_width / 2, panel_top - 26, summary, 5.2,
                    center=True, color=(0.30, 0.34, 0.38),
                )
            rectangles = _key_rectangles(panel_x, panel_top, panel_width, panel_height)
            for position, binding in enumerate(layer.bindings):
                x, y, width, height = rectangles[position]
                behavior = normalize_binding(binding).split()[0]
                fill = (0.965, 0.975, 0.985)
                if behavior in ("&mo", "&tog", "&to"):
                    fill = (0.88, 0.94, 0.98)
                elif behavior == "&none":
                    fill = (0.93, 0.93, 0.93)
                page.rect(x, y, width, height, fill=fill)
                label = binding_label(binding, layer_names, 9)
                base_label = binding_label(document.layers[0].bindings[position], layer_names, 7)
                if layer_index == 0:
                    if label:
                        page.text(x + width / 2, y + height / 2 - 2.2, label, 6.6, bold=True, center=True)
                elif behavior == "&trans":
                    if base_label:
                        page.text(
                            x + width / 2, y + height / 2 - 2.1, base_label, 5.8,
                            center=True, color=(0.40, 0.43, 0.47),
                        )
                else:
                    if base_label:
                        page.text(x + 2.2, y + height - 5.2, base_label, 3.8, color=(0.48, 0.50, 0.54))
                    if label:
                        page.text(x + width / 2, y + height / 2 - 2.7, label, 6.2, bold=True, center=True)
                if behavior not in ("&kp", "&trans", "&none"):
                    page.text(x + width / 2, y + 2.2, behavior.lstrip("&").upper(), 3.5, center=True, color=(0.35, 0.38, 0.42))

            used_pairs: dict[tuple[int, int], int] = {}
            for combo in document.combos:
                if len(combo.positions) != 2:
                    continue
                if combo.layers and layer_index not in combo.layers:
                    continue
                first, second = sorted(combo.positions)
                if not _adjacent_combo_pair(first, second):
                    continue
                first_rect, second_rect = rectangles[first], rectangles[second]
                first_center = (first_rect[0] + first_rect[2] / 2, first_rect[1] + first_rect[3] / 2)
                second_center = (second_rect[0] + second_rect[2] / 2, second_rect[1] + second_rect[3] / 2)
                center_x = (first_center[0] + second_center[0]) / 2
                center_y = (first_center[1] + second_center[1]) / 2
                label = binding_label(combo.binding, layer_names, 8) or combo.name[:8].upper()
                badge_w = max(20.0, min(43.0, 8.0 + len(label) * 3.4))
                stack = used_pairs.get((first, second), 0)
                used_pairs[(first, second)] = stack + 1
                badge_y = center_y + 5 + stack * 9
                page.rect(center_x - badge_w / 2, badge_y, badge_w, 8.5, fill=(1.0, 0.91, 0.67), stroke=(0.55, 0.35, 0.08), line=0.6)
                page.text(center_x, badge_y + 2.1, label, 4.4, bold=True, center=True, color=(0.28, 0.18, 0.04))

    footer = "Small gray legends are default keycaps; amber badges are adjacent two-key combos."
    if default_omitted:
        footer += " The default diagram was omitted to keep every layer on one side."
    page.text(36, 18, footer, 6.2, color=(0.38, 0.40, 0.44))
    _write_pdf(path, [page])


def render_layout_comment(bindings: list[str], indent: str, layer_names: list[str]) -> str:
    labels = [binding_label(binding, layer_names, 6) for binding in bindings]

    def cell(value: str) -> str:
        return f" {value:^6} "

    lines = [indent + "// " + "-" * 103]
    for start in (0, 12, 24):
        left = "|" + "|".join(cell(value) for value in labels[start : start + 6]) + "|"
        right = "|" + "|".join(cell(value) for value in labels[start + 6 : start + 12]) + "|"
        lines.append(indent + "// " + left + "   " + right)
    thumb_left = "|" + "|".join(cell(value) for value in labels[36:39]) + "|"
    thumb_right = "|" + "|".join(cell(value) for value in labels[39:42]) + "|"
    lines.append(indent + "// " + " " * 27 + thumb_left + "   " + thumb_right)
    return "\n".join(lines) + "\n"


def render_combos_node(combos: list[Combo], indent: str) -> str:
    lines = [f"{indent}combos {{", f'{indent}    compatible = "zmk,combos";']
    if combos:
        lines.append("")
    for combo_index, combo in enumerate(combos):
        lines.append(f"{indent}    {combo.name} {{")
        lines.append(f"{indent}        timeout-ms = <{combo.timeout_ms}>;")
        if combo.require_prior_idle_ms is not None:
            lines.append(f"{indent}        require-prior-idle-ms = <{combo.require_prior_idle_ms}>;")
        lines.append(f"{indent}        key-positions = <{' '.join(map(str, combo.positions))}>;")
        if combo.layers:
            lines.append(f"{indent}        layers = <{' '.join(map(str, combo.layers))}>;")
        lines.append(f"{indent}        bindings = <{normalize_binding(combo.binding)}>;")
        if combo.slow_release:
            lines.append(f"{indent}        slow-release;")
        lines.append(f"{indent}    }};")
        if combo_index != len(combos) - 1:
            lines.append("")
    lines.append(f"{indent}}};")
    return "\n".join(lines)


def render_conditional_layers_node(items: list[ConditionalLayer], indent: str) -> str:
    lines = [f"{indent}conditional_layers {{", f'{indent}    compatible = "zmk,conditional-layers";']
    if items:
        lines.append("")
    for index, item in enumerate(items):
        lines.append(f"{indent}    {item.name} {{")
        lines.append(f"{indent}        if-layers = <{' '.join(map(str, item.if_layers))}>;")
        lines.append(f"{indent}        then-layer = <{item.then_layer}>;")
        lines.append(f"{indent}    }};")
        if index != len(items) - 1:
            lines.append("")
    lines.append(f"{indent}}};")
    return "\n".join(lines)


def layer_pair_combo(
    name: str,
    positions: list[int],
    target_layer: int,
    behavior_name: str = "Momentary (while held)",
    timeout_ms: int = 50,
    source_layers: Optional[list[int]] = None,
) -> Combo:
    """Create the combo form used for two-physical-key layer activation."""
    if len(positions) != 2 or len(set(positions)) != 2:
        raise KeymapError("Physical layer activation requires exactly two different key positions.")
    binding = layer_behavior_binding(behavior_name, target_layer)
    return Combo(
        name=name,
        positions=sorted(positions),
        binding=binding,
        timeout_ms=timeout_ms,
        layers=ensure_toggle_target_layer(binding, source_layers or []),
        slow_release=binding.startswith("&mo "),
    )


def is_layer_pair_combo(combo: Combo) -> bool:
    return len(combo.positions) == 2 and bool(re.fullmatch(r"&(mo|tog|to)\s+\d+", normalize_binding(combo.binding)))


def toggle_target_layer(binding: str) -> Optional[int]:
    match = re.fullmatch(r"&tog\s+(\d+)", normalize_binding(binding))
    return int(match.group(1)) if match else None


def ensure_toggle_target_layer(binding: str, layers: Iterable[int]) -> list[int]:
    """Include a restricted toggle's target so the combo can toggle back off."""
    selected = sorted(set(int(layer) for layer in layers))
    target = toggle_target_layer(binding)
    # An empty list means global scope and already includes the target.
    if target is not None and selected and target not in selected:
        selected.append(target)
        selected.sort()
    return selected


def toggle_layer_scope_warning(binding: str, layers: Iterable[int]) -> str:
    selected = sorted(set(int(layer) for layer in layers))
    target = toggle_target_layer(binding)
    if target is not None and selected and target not in selected:
        return (
            f"Toggle target layer {target} is not selected. Include it so this combo remains "
            "available while the target layer is active and can toggle the layer back off."
        )
    return ""


KEYSYM_TO_ZMK = {
    "space": "SPACE", "Return": "RET", "KP_Enter": "RET", "BackSpace": "BSPC", "Tab": "TAB",
    "Escape": "ESC", "Delete": "DEL", "Insert": "INS", "Home": "HOME", "End": "END",
    "Prior": "PG_UP", "Next": "PG_DN", "Left": "LEFT", "Right": "RIGHT", "Up": "UP", "Down": "DOWN",
    "Caps_Lock": "CAPS", "Print": "PSCRN", "Scroll_Lock": "SLCK", "Pause": "PAUSE",
    "Shift_L": "LSHFT", "Shift_R": "RSHFT", "Control_L": "LCTRL", "Control_R": "RCTRL",
    "Alt_L": "LALT", "Alt_R": "RALT", "Super_L": "LGUI", "Super_R": "RGUI", "Meta_L": "LGUI", "Meta_R": "RGUI",
    "semicolon": "SEMI", "colon": "COLON", "apostrophe": "SQT", "quotedbl": "DQT", "comma": "COMMA",
    "less": "LT", "period": "DOT", "greater": "GT", "slash": "FSLH", "question": "QMARK",
    "backslash": "BSLH", "bar": "PIPE", "grave": "GRAVE", "asciitilde": "TILDE", "minus": "MINUS",
    "underscore": "UNDER", "equal": "EQUAL", "plus": "PLUS", "bracketleft": "LBKT", "braceleft": "LBRC",
    "bracketright": "RBKT", "braceright": "RBRC", "exclam": "EXCL", "at": "AT", "numbersign": "HASH",
    "dollar": "DLLR", "percent": "PRCNT", "asciicircum": "CARET", "ampersand": "AMPS", "asterisk": "ASTRK",
    "parenleft": "LPAR", "parenright": "RPAR",
}
MODIFIER_KEYSYMS = {"Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Super_L", "Super_R", "Meta_L", "Meta_R"}


def keysym_to_binding(keysym: str, char: str = "") -> Optional[str]:
    if keysym in KEYSYM_TO_ZMK:
        return "&kp " + KEYSYM_TO_ZMK[keysym]
    if re.fullmatch(r"F(?:[1-9]|1[0-9]|2[0-4])", keysym):
        return "&kp " + keysym
    if len(keysym) == 1 and keysym.isalpha():
        return "&kp " + keysym.upper()
    if len(keysym) == 1 and keysym.isdigit():
        return "&kp N" + keysym
    if char and char in "!@#$%^&*()_+{}|:\"<>?~":
        char_map = {
            "!": "EXCL", "@": "AT", "#": "HASH", "$": "DLLR", "%": "PRCNT", "^": "CARET", "&": "AMPS", "*": "ASTRK",
            "(": "LPAR", ")": "RPAR", "_": "UNDER", "+": "PLUS", "{": "LBRC", "}": "RBRC", "|": "PIPE", ":": "COLON",
            '"': "DQT", "<": "LT", ">": "GT", "?": "QMARK", "~": "TILDE",
        }
        return "&kp " + char_map[char]
    return None


class CorneKeymapApp:
    def __init__(self, root, initial_path: Optional[Path] = None):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.colors = cinnamon_appearance()
        self._apply_cinnamon_style()
        self.root.title(APP_NAME)
        try:
            self.root.tk.call("wm", "class", self.root._w, "CorneKeymapEditor")
        except self.tk.TclError:
            pass
        self.root.geometry("1120x780")
        self.root.minsize(900, 650)
        self.settings = load_editor_settings()
        configured_keymap_directory = self.settings.get("last_keymap_dir", self.settings.get("last_open_dir", ""))
        self.last_keymap_dir = Path(configured_keymap_directory).expanduser() if configured_keymap_directory else Path.home()
        if not self.last_keymap_dir.is_dir():
            self.last_keymap_dir = Path.home()
        configured_keymap_file = self.settings.get("last_keymap_file", "")
        self.last_keymap_file = Path(configured_keymap_file).expanduser() if configured_keymap_file else None
        configured_pdf_directory = self.settings.get("last_pdf_dir", "")
        self.last_pdf_dir = Path(configured_pdf_directory).expanduser() if configured_pdf_directory else self.last_keymap_dir
        if not self.last_pdf_dir.is_dir():
            self.last_pdf_dir = self.last_keymap_dir
        configured_pdf_file = self.settings.get("last_pdf_file", "")
        self.last_pdf_file = Path(configured_pdf_file).expanduser() if configured_pdf_file else None
        self.path: Optional[Path] = None
        self.document: Optional[KeymapDocument] = None
        self.dirty = False
        self.selected_key = 0
        self.pending_modifier: Optional[str] = None
        self.nonmodifier_captured = False
        self.key_buttons: list = []
        self.default_button_bg = None

        self._build_ui()
        self.root.bind_all("<KeyPress>", self._on_key_press, add="+")
        self.root.bind_all("<KeyRelease>", self._on_key_release, add="+")
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        if initial_path:
            self.open_path(initial_path)
        else:
            self.root.after(100, self.open_file)

    def _apply_cinnamon_style(self):
        """Make Tk widgets follow Cinnamon's current light/dark appearance."""
        from tkinter import font as tkfont

        style = self.ttk.Style(self.root)
        available = style.theme_names()
        if "clam" in available:
            style.theme_use("clam")
        colors = self.colors
        try:
            self.root.tk_setPalette(
                background=colors["background"], foreground=colors["foreground"],
                activeBackground=colors["selected"], activeForeground=colors["foreground"],
                highlightColor=colors["accent"], selectBackground=colors["accent"],
                selectForeground="#ffffff",
            )
        except self.tk.TclError:
            pass
        # Configure Tk's named fonts with separate scalar arguments. Never pass
        # Cinnamon's Pango description through option_add("*Font", ...): Tcl's
        # compact font parser can interpret a bare size such as "10" as a font
        # style and crash widget construction.
        font_options = {
            "family": colors.get("font_family", "Sans"),
            "size": int(colors.get("font_size", "10")),
            "weight": colors.get("font_weight", "normal"),
            "slant": colors.get("font_slant", "roman"),
        }
        for font_name in (
            "TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
            "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont",
        ):
            try:
                tkfont.nametofont(font_name, root=self.root).configure(**font_options)
            except (self.tk.TclError, ValueError, TypeError):
                # Older Tk versions do not define every named font. A bad or
                # unavailable desktop font must never prevent startup.
                continue
        try:
            tkfont.nametofont("TkFixedFont", root=self.root).configure(size=font_options["size"])
        except (self.tk.TclError, ValueError, TypeError):
            pass
        self.root.configure(background=colors["background"])
        common = {
            "background": colors["background"], "foreground": colors["foreground"],
            "bordercolor": colors["border"], "lightcolor": colors["border"],
            "darkcolor": colors["border"],
        }
        style.configure(".", **common)
        style.configure("TFrame", background=colors["background"])
        style.configure("TLabel", background=colors["background"], foreground=colors["foreground"])
        style.configure("Warning.TLabel", background=colors["background"], foreground=colors["warning_fg"])
        style.configure("TLabelframe", background=colors["background"], foreground=colors["foreground"])
        style.configure("TLabelframe.Label", background=colors["background"], foreground=colors["foreground"])
        style.configure("TButton", background=colors["button"], foreground=colors["foreground"], padding=(7, 4))
        style.map("TButton", background=[("active", colors["selected"]), ("pressed", colors["accent"])])
        style.configure("TEntry", fieldbackground=colors["field"], foreground=colors["foreground"])
        style.configure("TCombobox", fieldbackground=colors["field"], foreground=colors["foreground"])
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", colors["field"])],
            foreground=[("readonly", colors["foreground"])],
            selectbackground=[("readonly", colors["accent"])],
            selectforeground=[("readonly", "#ffffff")],
        )
        style.configure(
            "Treeview", background=colors["panel"], fieldbackground=colors["panel"],
            foreground=colors["foreground"], rowheight=26,
        )
        style.map("Treeview", background=[("selected", colors["accent"])], foreground=[("selected", "#ffffff")])
        style.configure("Treeview.Heading", background=colors["button"], foreground=colors["foreground"])
        style.configure("TNotebook", background=colors["background"], bordercolor=colors["border"])
        style.configure("TNotebook.Tab", background=colors["button"], foreground=colors["foreground"], padding=(10, 5))
        style.map("TNotebook.Tab", background=[("selected", colors["panel"]), ("active", colors["selected"])])

    def _build_ui(self):
        tk, ttk = self.tk, self.ttk
        toolbar = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="Open…", command=self.open_file).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Save", command=self.save).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Save As…", command=self.save_as).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Export PDF…", command=self.export_pdf).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Validate", command=self.validate_document).pack(side="left", padx=12)
        ttk.Label(toolbar, text="Layer:").pack(side="left", padx=(15, 4))
        self.layer_var = tk.StringVar()
        self.layer_combo = ttk.Combobox(toolbar, textvariable=self.layer_var, state="readonly", width=24)
        self.layer_combo.pack(side="left")
        self.layer_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_keys())
        ttk.Button(toolbar, text="Add layer…", command=self.add_layer).pack(side="left", padx=5)
        ttk.Button(toolbar, text="Rename layer…", command=self.rename_layer).pack(side="left", padx=2)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=8, pady=4)
        self.keymap_tab = ttk.Frame(self.notebook, padding=10)
        self.activation_tab = ttk.Frame(self.notebook, padding=10)
        self.combo_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(self.keymap_tab, text="Keymap")
        self.notebook.add(self.activation_tab, text="Layer activation")
        self.notebook.add(self.combo_tab, text="Combos")

        ttk.Label(
            self.keymap_tab,
            text="Click a key, then type it. Use the Layer key controls to move or change layer selectors without typing ZMK code.",
        ).pack(fill="x", pady=(0, 8))
        self.keymap_warning_summary_var = tk.StringVar(value="")
        ttk.Label(
            self.keymap_tab,
            textvariable=self.keymap_warning_summary_var,
            style="Warning.TLabel",
            wraplength=1000,
            justify="left",
        ).pack(fill="x", pady=(0, 5))

        keyboard = ttk.Frame(self.keymap_tab)
        keyboard.pack(fill="x", expand=True, anchor="n", pady=8)
        configure_corne_grid(keyboard, CORNE_MAIN_GAP_PX)

        for index in range(KEY_COUNT):
            row, column = corne_grid_position(index)
            button = tk.Button(
                keyboard, text=str(index), width=8, height=3, wraplength=82,
                bg=self.colors["button"], fg=self.colors["foreground"],
                activebackground=self.colors["selected"], activeforeground=self.colors["foreground"],
                highlightbackground=self.colors["border"],
                command=lambda key_index=index: self.select_key(key_index)
            )
            button.grid(row=row, column=column, padx=3, pady=4, sticky="nsew")
            self.key_buttons.append(button)
            if self.default_button_bg is None:
                self.default_button_bg = button.cget("bg")

        editor = ttk.LabelFrame(self.keymap_tab, text="Selected key", padding=10)
        editor.pack(fill="x", pady=10)
        self.selected_label = ttk.Label(editor, text="Key 0")
        self.selected_label.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.binding_var = tk.StringVar()
        self.binding_entry = ttk.Entry(editor, textvariable=self.binding_var, width=55)
        self.binding_entry.grid(row=0, column=1, sticky="ew", padx=4)
        self.binding_entry.bind("<Return>", lambda _event: self.apply_raw_binding())
        ttk.Button(editor, text="Apply raw binding", command=self.apply_raw_binding).grid(row=0, column=2, padx=4)
        ttk.Button(editor, text="Transparent", command=lambda: self.set_selected_binding("&trans")).grid(row=1, column=1, sticky="w", padx=4, pady=6)
        ttk.Button(editor, text="None", command=lambda: self.set_selected_binding("&none")).grid(row=1, column=1, padx=(100, 4), pady=6, sticky="w")

        layer_key = ttk.LabelFrame(editor, text="Layer key", padding=7)
        layer_key.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(6, 0))
        ttk.Label(layer_key, text="Behavior:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.layer_behavior_var = tk.StringVar(value="Momentary (while held)")
        self.layer_behavior_combo = ttk.Combobox(
            layer_key, textvariable=self.layer_behavior_var, state="readonly", width=22,
            values=list(LAYER_BEHAVIORS),
        )
        self.layer_behavior_combo.grid(row=0, column=1, sticky="w", padx=3)
        ttk.Label(layer_key, text="Target:").grid(row=0, column=2, sticky="w", padx=(12, 4))
        self.layer_target_var = tk.StringVar()
        self.layer_target_combo = ttk.Combobox(layer_key, textvariable=self.layer_target_var, state="readonly", width=24)
        self.layer_target_combo.grid(row=0, column=3, sticky="w", padx=3)
        ttk.Button(layer_key, text="Set selected key", command=self.apply_layer_key).grid(row=0, column=4, sticky="w", padx=8)
        ttk.Label(
            layer_key,
            text="To move a layer selector, assign the replacement first, then select the old key and click Transparent.",
            wraplength=850,
            justify="left",
        ).grid(row=1, column=0, columnspan=5, sticky="w", padx=1, pady=(6, 0))
        self.shortcut_warning_var = tk.StringVar(value="")
        self.shortcut_warning_label = ttk.Label(
            editor, textvariable=self.shortcut_warning_var, style="Warning.TLabel", wraplength=980, justify="left"
        )
        self.shortcut_warning_label.grid(row=3, column=0, columnspan=3, sticky="w", pady=(7, 0))
        editor.columnconfigure(1, weight=1)

        activation_intro = ttk.Label(
            self.activation_tab,
            text=(
                "Create a layer from two active layers, or activate one by pressing two physical keys together. "
                "Momentary physical pairs stay active until both trigger keys are released."
            ),
            wraplength=1000,
        )
        activation_intro.pack(fill="x", pady=(0, 8))

        conditional_frame = ttk.LabelFrame(
            self.activation_tab, text="Two-layer conditional activation", padding=8
        )
        conditional_frame.pack(fill="both", expand=True, pady=4)
        conditional_buttons = ttk.Frame(conditional_frame)
        conditional_buttons.pack(fill="x")
        ttk.Label(
            conditional_buttons,
            text="The target becomes active while both selected source layers are active.",
        ).pack(side="left")
        ttk.Button(conditional_buttons, text="Add…", command=self.add_conditional_activation).pack(side="right", padx=3)
        ttk.Button(conditional_buttons, text="Edit…", command=self.edit_conditional_activation).pack(side="right", padx=3)
        ttk.Button(conditional_buttons, text="Delete", command=self.delete_conditional_activation).pack(side="right", padx=3)
        conditional_columns = ("name", "first", "second", "target")
        self.conditional_tree = ttk.Treeview(
            conditional_frame, columns=conditional_columns, show="headings", height=5, selectmode="browse"
        )
        for column, heading, width in (
            ("name", "Name", 180), ("first", "First active layer", 210),
            ("second", "Second active layer", 210), ("target", "Activated layer", 210),
        ):
            self.conditional_tree.heading(column, text=heading)
            self.conditional_tree.column(column, width=width, anchor="w")
        self.conditional_tree.pack(fill="both", expand=True, pady=(7, 0))
        self.conditional_tree.bind("<Double-1>", lambda _event: self.edit_conditional_activation())

        pair_frame = ttk.LabelFrame(
            self.activation_tab, text="Two physical keys pressed simultaneously", padding=8
        )
        pair_frame.pack(fill="both", expand=True, pady=4)
        pair_buttons = ttk.Frame(pair_frame)
        pair_buttons.pack(fill="x")
        ttk.Label(
            pair_buttons,
            text="Uses a ZMK combo containing exactly two physical key positions and a layer behavior.",
        ).pack(side="left")
        ttk.Button(pair_buttons, text="Add…", command=self.add_layer_pair).pack(side="right", padx=3)
        ttk.Button(pair_buttons, text="Edit…", command=self.edit_layer_pair).pack(side="right", padx=3)
        ttk.Button(pair_buttons, text="Delete", command=self.delete_layer_pair).pack(side="right", padx=3)
        pair_columns = ("name", "positions", "behavior", "target", "timeout", "available")
        self.layer_pair_tree = ttk.Treeview(
            pair_frame, columns=pair_columns, show="headings", height=5, selectmode="browse"
        )
        for column, heading, width in (
            ("name", "Name", 150), ("positions", "Key positions", 120),
            ("behavior", "Behavior", 145), ("target", "Target layer", 190),
            ("timeout", "Window", 85), ("available", "Available on", 150),
        ):
            self.layer_pair_tree.heading(column, text=heading)
            self.layer_pair_tree.column(column, width=width, anchor="w")
        self.layer_pair_tree.pack(fill="both", expand=True, pady=(7, 0))
        self.layer_pair_tree.bind("<Double-1>", lambda _event: self.edit_layer_pair())

        combo_top = ttk.Frame(self.combo_tab)
        combo_top.pack(fill="x")
        ttk.Label(combo_top, text="Combos are saved as a standard zmk,combos node using Corne key positions 0–41.").pack(side="left")
        ttk.Button(combo_top, text="Add combo…", command=self.add_combo).pack(side="right", padx=3)
        ttk.Button(combo_top, text="Edit…", command=self.edit_combo).pack(side="right", padx=3)
        ttk.Button(combo_top, text="Delete", command=self.delete_combo).pack(side="right", padx=3)

        columns = ("name", "positions", "binding", "timeout", "layers", "options")
        self.combo_tree = ttk.Treeview(self.combo_tab, columns=columns, show="headings", selectmode="browse")
        headings = {"name": "Name", "positions": "Positions", "binding": "Output binding", "timeout": "Timeout", "layers": "Layers", "options": "Options"}
        widths = {"name": 150, "positions": 170, "binding": 220, "timeout": 90, "layers": 100, "options": 190}
        for column in columns:
            self.combo_tree.heading(column, text=headings[column])
            self.combo_tree.column(column, width=widths[column], anchor="w")
        self.combo_tree.pack(fill="both", expand=True, pady=10)
        self.combo_tree.bind("<Double-1>", lambda _event: self.edit_combo())

        self.status_var = tk.StringVar(value="Open a corne.keymap file to begin.")
        ttk.Label(self.root, textvariable=self.status_var, relief="sunken", anchor="w", padding=(6, 3)).pack(fill="x", side="bottom")

    def current_layer(self) -> Optional[Layer]:
        if not self.document:
            return None
        index = self.layer_combo.current()
        if index < 0:
            index = 0
        return self.document.layers[index]

    def select_key(self, index: int):
        self.selected_key = index
        self.refresh_keys()
        self.root.focus_set()
        self.status_var.set(f"Key position {index} selected. Type a key, or edit the raw binding below.")

    def refresh_keys(self):
        layer = self.current_layer()
        if not layer:
            for index, button in enumerate(self.key_buttons):
                button.configure(text=str(index), state="disabled", relief="raised", bg=self.root.cget("bg"))
            return
        layer_names = [item.display_name for item in self.document.layers]
        layer_index = max(0, self.layer_combo.current())
        direct_warnings: dict[int, list[ShortcutWarning]] = {}
        for warning in analyze_shortcut_conflicts(self.document):
            if warning.layer_index == layer_index and warning.key_position is not None:
                direct_warnings.setdefault(warning.key_position, []).append(warning)
        for index, button in enumerate(self.key_buttons):
            binding = layer.bindings[index]
            label = binding_label(binding, layer_names, 8) or "—"
            selected = index == self.selected_key
            if index in direct_warnings:
                background = self.colors["warning_selected"] if selected else self.colors["warning_bg"]
            else:
                background = self.colors["selected"] if selected else self.default_button_bg
            button.configure(
                text=f"{index}\n{label}{' !' if index in direct_warnings else ''}",
                state="normal", relief="sunken" if selected else "raised",
                bg=background,
            )
        self.selected_label.configure(text=f"Key {self.selected_key}")
        self.binding_var.set(layer.bindings[self.selected_key])
        self._sync_layer_key_controls(layer.bindings[self.selected_key])
        selected_warnings = direct_warnings.get(self.selected_key, [])
        self.shortcut_warning_var.set(
            "Shortcut warning:\n" + "\n".join(f"• {warning.message}" for warning in selected_warnings)
            if selected_warnings else ""
        )
        layer_warning_count = sum(len(items) for items in direct_warnings.values())
        self.keymap_warning_summary_var.set(
            f"! {layer_warning_count} shortcut conflict warning(s) on this layer. "
            "Affected keys are amber and marked with !; select one for details."
            if layer_warning_count else ""
        )

    def refresh_layer_selectors(self, selected_index: Optional[int] = None):
        if not self.document:
            self.layer_combo.configure(values=[])
            self.layer_target_combo.configure(values=[])
            return
        values = [f"{index}: {layer.display_name}" for index, layer in enumerate(self.document.layers)]
        current = self.layer_combo.current() if selected_index is None else selected_index
        if current < 0 or current >= len(values):
            current = 0
        self.layer_combo.configure(values=values)
        self.layer_combo.current(current)
        target = self.layer_target_combo.current()
        self.layer_target_combo.configure(values=values)
        if values:
            self.layer_target_combo.current(target if 0 <= target < len(values) else min(1, len(values) - 1))

    def _sync_layer_key_controls(self, binding: str):
        match = re.fullmatch(r"&(mo|tog|to)\s+(\d+)", normalize_binding(binding))
        if not match or not self.document:
            return
        behavior_code, target_text = match.groups()
        target = int(target_text)
        behavior_name = next((name for name, code in LAYER_BEHAVIORS.items() if code == behavior_code), None)
        if behavior_name:
            self.layer_behavior_var.set(behavior_name)
        if target < len(self.document.layers):
            self.layer_target_combo.current(target)

    def apply_layer_key(self):
        if not self.document:
            return
        target = self.layer_target_combo.current()
        if target < 0:
            self._show_error("No target layer", "Choose a target layer first.")
            return
        try:
            binding = layer_behavior_binding(self.layer_behavior_var.get(), target)
        except KeymapError as exc:
            self._show_error("Invalid layer key", str(exc))
            return
        self.set_selected_binding(binding)

    def add_layer(self):
        from tkinter import messagebox, simpledialog
        if not self.document:
            return
        name = simpledialog.askstring(
            "Add layer", "Visible layer name (for example: Navigation or BT-LED):", parent=self.root
        )
        if name is None:
            return
        name = name.strip()
        if not name:
            self._show_error("Invalid layer name", "Layer display name cannot be empty.")
            return
        if any(layer.display_name == name for layer in self.document.layers):
            self._show_error("Duplicate layer name", f'A layer displayed as "{name}" already exists.')
            return
        copy_current = messagebox.askyesnocancel(
            "Initialize layer",
            "Copy the currently displayed layer?\n\nYes: copy current bindings\nNo: start with all keys transparent",
            parent=self.root,
        )
        if copy_current is None:
            return
        template = list(self.current_layer().bindings) if copy_current else None
        try:
            new_index = self.document.add_layer(name, template)
        except KeymapError as exc:
            self._show_error("Could not add layer", str(exc))
            return
        self.refresh_layer_selectors(new_index)
        self.refresh_keys()
        self.refresh_combos()
        self.refresh_activations()
        self.mark_dirty()
        added = self.document.layers[new_index]
        self.status_var.set(
            f'Added layer {new_index}: "{added.display_name}" (identifier: {added.name}). '
            "Existing layer numbers were not changed."
        )

    def rename_layer(self):
        from tkinter import simpledialog
        if not self.document:
            return
        index = max(0, self.layer_combo.current())
        old_name = self.document.layers[index].display_name
        new_name = simpledialog.askstring(
            "Rename layer",
            f"New visible name for layer {index} ({old_name}):",
            initialvalue=old_name,
            parent=self.root,
        )
        if new_name is None or new_name.strip() == old_name:
            return
        try:
            self.document.rename_layer(index, new_name)
        except KeymapError as exc:
            self._show_error("Could not rename layer", str(exc))
            return
        self.refresh_layer_selectors(index)
        self.refresh_keys()
        self.refresh_combos()
        self.refresh_activations()
        self.refresh_warnings()
        self.mark_dirty()
        self.status_var.set(
            f'Renamed layer {index} from "{old_name}" to "{self.document.layers[index].display_name}" '
            f'(identifier: {self.document.layers[index].name}). Numeric references are unchanged.'
        )

    def refresh_combos(self):
        for item in self.combo_tree.get_children():
            self.combo_tree.delete(item)
        if not self.document:
            return
        for index, combo in enumerate(self.document.combos):
            options = []
            if combo.slow_release:
                options.append("slow release")
            if combo.require_prior_idle_ms is not None:
                options.append(f"idle {combo.require_prior_idle_ms} ms")
            if combo_shortcut_warnings(combo, self.document, index):
                options.append("shortcut conflict")
            self.combo_tree.insert(
                "", "end", iid=str(index), values=(
                    combo.name, " ".join(map(str, combo.positions)), combo.binding,
                    f"{combo.timeout_ms} ms", " ".join(map(str, combo.layers)) or "all", ", ".join(options)
                )
            )
        self.refresh_warnings()

    def refresh_warnings(self):
        if not self.document:
            return
        self.refresh_keys()

    def _layer_display(self, index: int) -> str:
        if not self.document or index < 0 or index >= len(self.document.layers):
            return str(index)
        return f"{index}: {self.document.layers[index].display_name}"

    def refresh_activations(self):
        for tree in (self.conditional_tree, self.layer_pair_tree):
            for item in tree.get_children():
                tree.delete(item)
        if not self.document:
            return
        for index, item in enumerate(self.document.conditional_layers):
            first = item.if_layers[0] if item.if_layers else -1
            second = item.if_layers[1] if len(item.if_layers) > 1 else -1
            self.conditional_tree.insert(
                "", "end", iid=str(index),
                values=(item.name, self._layer_display(first), self._layer_display(second), self._layer_display(item.then_layer)),
            )
        behavior_names = {code: name for name, code in LAYER_BEHAVIORS.items()}
        for combo_index, combo in enumerate(self.document.combos):
            if not is_layer_pair_combo(combo):
                continue
            match = re.fullmatch(r"&(mo|tog|to)\s+(\d+)", normalize_binding(combo.binding))
            behavior_code, target_text = match.groups()
            available = ", ".join(self._layer_display(layer) for layer in combo.layers) or "All layers"
            self.layer_pair_tree.insert(
                "", "end", iid=str(combo_index),
                values=(
                    combo.name, " + ".join(map(str, combo.positions)), behavior_names[behavior_code],
                    self._layer_display(int(target_text)), f"{combo.timeout_ms} ms", available,
                ),
            )

    def _selected_tree_index(self, tree, message: str) -> Optional[int]:
        selection = tree.selection()
        if not selection:
            self.status_var.set(message)
            return None
        return int(selection[0])

    def _next_conditional_name(self) -> str:
        existing = {item.name for item in self.document.conditional_layers}
        index = 1
        while f"conditional_{index}" in existing:
            index += 1
        return f"conditional_{index}"

    def add_conditional_activation(self):
        if not self.document:
            return
        if len(self.document.layers) < 3:
            self._show_error(
                "Not enough layers",
                "Two-layer conditional activation requires two source layers and a different target layer.",
            )
            return
        item = ConditionalLayer(self._next_conditional_name(), [0, min(1, len(self.document.layers) - 1)], min(2, len(self.document.layers) - 1))
        if ConditionalLayerDialog(self, item, is_new=True).show():
            self.document.conditional_layers.append(item)
            self.mark_dirty()
            self.refresh_activations()

    def edit_conditional_activation(self):
        index = self._selected_tree_index(self.conditional_tree, "Select a conditional activation first.")
        if index is None:
            return
        item = copy_conditional(self.document.conditional_layers[index])
        if ConditionalLayerDialog(self, item, is_new=False, editing_index=index).show():
            self.document.conditional_layers[index] = item
            self.mark_dirty()
            self.refresh_activations()

    def delete_conditional_activation(self):
        from tkinter import messagebox
        index = self._selected_tree_index(self.conditional_tree, "Select a conditional activation first.")
        if index is None:
            return
        item = self.document.conditional_layers[index]
        if messagebox.askyesno("Delete conditional activation", f'Delete "{item.name}"?'):
            del self.document.conditional_layers[index]
            self.mark_dirty()
            self.refresh_activations()

    def _next_layer_pair_name(self) -> str:
        existing = {combo.name for combo in self.document.combos}
        index = 1
        while f"layer_pair_{index}" in existing:
            index += 1
        return f"layer_pair_{index}"

    def add_layer_pair(self):
        if not self.document:
            return
        combo = layer_pair_combo(
            self._next_layer_pair_name(), [36, 39], min(1, len(self.document.layers) - 1),
            source_layers=[0],
        )
        if LayerPairDialog(self, combo, is_new=True).show():
            self.document.combos.append(combo)
            self.mark_dirty()
            self.refresh_combos()
            self.refresh_activations()

    def edit_layer_pair(self):
        index = self._selected_tree_index(self.layer_pair_tree, "Select a physical-key activation first.")
        if index is None:
            return
        combo = copy_combo(self.document.combos[index])
        if LayerPairDialog(self, combo, is_new=False, editing_index=index).show():
            self.document.combos[index] = combo
            self.mark_dirty()
            self.refresh_combos()
            self.refresh_activations()

    def delete_layer_pair(self):
        from tkinter import messagebox
        index = self._selected_tree_index(self.layer_pair_tree, "Select a physical-key activation first.")
        if index is None:
            return
        combo = self.document.combos[index]
        if messagebox.askyesno("Delete physical-key activation", f'Delete "{combo.name}"?'):
            del self.document.combos[index]
            self.mark_dirty()
            self.refresh_combos()
            self.refresh_activations()

    def _widget_accepts_text(self, widget) -> bool:
        names = {"Entry", "TEntry", "Text", "TCombobox", "Spinbox", "TSpinbox"}
        return widget.winfo_class() in names

    def _on_key_press(self, event):
        if self._widget_accepts_text(event.widget) or not self.document or self.notebook.index(self.notebook.select()) != 0:
            return
        if event.keysym in MODIFIER_KEYSYMS:
            self.pending_modifier = event.keysym
            self.nonmodifier_captured = False
            return "break"
        binding = keysym_to_binding(event.keysym, event.char)
        if binding:
            self.nonmodifier_captured = True
            self.pending_modifier = None
            self.set_selected_binding(binding)
            return "break"

    def _on_key_release(self, event):
        if self._widget_accepts_text(event.widget) or not self.document:
            return
        if event.keysym in MODIFIER_KEYSYMS and self.pending_modifier == event.keysym and not self.nonmodifier_captured:
            binding = keysym_to_binding(event.keysym, event.char)
            self.pending_modifier = None
            if binding:
                self.set_selected_binding(binding)
                return "break"

    def set_selected_binding(self, binding: str):
        layer = self.current_layer()
        if not layer:
            return
        binding = normalize_binding(binding)
        if not binding.startswith("&"):
            self._show_error("Invalid binding", 'A ZMK binding must start with "&".')
            return
        behavior_error = layer_behavior_error(binding)
        if behavior_error:
            self._show_error("Invalid layer binding", behavior_error)
            return
        layer.bindings[self.selected_key] = binding
        self.mark_dirty()
        self.refresh_keys()
        self.refresh_combos()
        self.status_var.set(f"Position {self.selected_key} set to {binding}.")

    def apply_raw_binding(self):
        self.set_selected_binding(self.binding_var.get())
        self.root.focus_set()

    def mark_dirty(self):
        self.dirty = True
        self._update_title()

    def _update_title(self):
        name = self.path.name if self.path else "Untitled"
        self.root.title(f"{'*' if self.dirty else ''}{name} — {APP_NAME}")

    def _confirm_discard(self) -> bool:
        from tkinter import messagebox
        if not self.dirty:
            return True
        answer = messagebox.askyesnocancel(APP_NAME, "Save changes before continuing?")
        if answer is None:
            return False
        if answer:
            return self.save()
        return True

    def open_file(self):
        from tkinter import filedialog
        if not self._confirm_discard():
            return
        initial_file = self.last_keymap_file.name if self.last_keymap_file else ""
        handled, selected = native_file_dialog(
            "open", "Open Corne keymap", self.last_keymap_dir, initial_file,
            [("ZMK keymaps", "*.keymap *.txt"), ("All files", "*")],
        )
        if not handled:
            filename = filedialog.askopenfilename(
                title="Open Corne keymap",
                initialdir=str(self.last_keymap_dir),
                initialfile=initial_file,
                filetypes=(("ZMK keymap", "*.keymap"), ("Text files", "*.txt"), ("All files", "*")),
            )
            selected = Path(filename) if filename else None
        if selected:
            self.open_path(selected)

    def open_path(self, path: Path):
        from tkinter import messagebox
        try:
            document = KeymapDocument.from_file(path)
        except (OSError, KeymapError) as exc:
            self._show_error("Could not open keymap", str(exc))
            return
        self.document = document
        self.path = path
        self._remember_keymap_location(path)
        self.dirty = bool(document.repair_notes)
        self.selected_key = 0
        self.refresh_layer_selectors(0)
        self.refresh_keys()
        self.refresh_combos()
        self.refresh_activations()
        self._update_title()
        if document.repair_notes:
            self.status_var.set(
                f"Recovered {path.name} with {len(document.repair_notes)} activation-syntax repair(s). Save to write the corrected keymap."
            )
            messagebox.showwarning(
                "Broken keymap recovered",
                "The keymap was opened and its malformed layer-activation syntax was repaired in memory.\n\n"
                + "\n".join("• " + note for note in document.repair_notes)
                + "\n\nReview the recovered layers and activations, then use Save or Save As to write a corrected corne.keymap.",
                parent=self.root,
            )
        else:
            self.status_var.set(
                f"Loaded {len(document.layers)} layers, {len(document.conditional_layers)} conditional activations, "
                f"and {len(document.combos)} combos from {path.name}."
            )

    def validate_document(self):
        from tkinter import messagebox
        if not self.document:
            return
        errors = self.document.validate()
        if errors:
            messagebox.showerror("Validation errors", "\n".join("• " + error for error in errors))
        else:
            warnings = analyze_shortcut_conflicts(self.document)
            if warnings:
                messagebox.showwarning(
                    "Valid keymap with shortcut warnings",
                    f"{len(self.document.layers)} layers and {len(self.document.combos)} combos are structurally valid.\n\n"
                    + "\n".join(f"• {warning.source}: {warning.shortcut}" for warning in warnings)
                    + "\n\nAffected keys are amber and marked with ! on the Keymap tab. Select a key for details.",
                    parent=self.root,
                )
            else:
                messagebox.showinfo("Valid keymap", f"{len(self.document.layers)} layers and {len(self.document.combos)} combos are valid.")

    def export_pdf(self):
        from tkinter import filedialog, messagebox
        if not self.document:
            return
        stem = self.path.stem.replace(".keymap", "") if self.path else "corne"
        initial_name = self.last_pdf_file.name if self.last_pdf_file else f"{stem}-cheat-sheet.pdf"
        handled, selected = native_file_dialog(
            "save", "Export printable keymap cheat sheet", self.last_pdf_dir,
            initial_name, [("PDF documents", "*.pdf"), ("All files", "*")],
        )
        if not handled:
            filename = filedialog.asksaveasfilename(
                title="Export printable keymap cheat sheet",
                defaultextension=".pdf",
                initialfile=initial_name,
                initialdir=str(self.last_pdf_dir),
                filetypes=(("PDF document", "*.pdf"), ("All files", "*")),
            )
            selected = Path(filename) if filename else None
        if not selected:
            return
        if selected.suffix.lower() != ".pdf":
            selected = selected.with_suffix(".pdf")
        try:
            export_keymap_pdf(self.document, selected)
        except (OSError, KeymapError) as exc:
            self._show_error("Could not export PDF", str(exc))
            return
        self._remember_pdf_location(selected)
        self.status_var.set(f"Exported printable cheat sheet to {selected}.")
        messagebox.showinfo("PDF exported", f"Printable keymap cheat sheet saved to:\n{selected}", parent=self.root)

    def save(self) -> bool:
        if not self.document:
            return False
        if not self.path or self.path.suffix.lower() == ".txt":
            return self.save_as()
        return self._write_path(self.path)

    def save_as(self) -> bool:
        from tkinter import filedialog
        if not self.document:
            return False
        initial = (self.path.stem if self.path else "corne") + ".keymap"
        handled, selected = native_file_dialog(
            "save", "Save Corne keymap as", self.last_keymap_dir, initial,
            [("ZMK keymaps", "*.keymap"), ("All files", "*")],
        )
        if not handled:
            filename = filedialog.asksaveasfilename(
                title="Save Corne keymap as",
                defaultextension=".keymap",
                initialfile=initial,
                initialdir=str(self.last_keymap_dir),
                filetypes=(("ZMK keymap", "*.keymap"), ("All files", "*")),
            )
            selected = Path(filename) if filename else None
        if selected and not selected.suffix:
            selected = selected.with_suffix(".keymap")
        return self._write_path(selected) if selected else False

    def _write_path(self, path: Path) -> bool:
        selected_layer = max(0, self.layer_combo.current())
        try:
            rendered = self.document.render()
            path.write_text(rendered, encoding="utf-8")
            self.document = KeymapDocument.from_text(rendered)
        except (OSError, KeymapError) as exc:
            self._show_error("Could not save keymap", str(exc))
            return False
        self.path = path
        self._remember_keymap_location(path)
        self.dirty = False
        self.refresh_layer_selectors(min(selected_layer, len(self.document.layers) - 1))
        self.refresh_keys()
        self.refresh_combos()
        self.refresh_activations()
        self._update_title()
        self.status_var.set(f"Saved {path}.")
        return True

    def _remember_keymap_location(self, path: Path):
        path = path.expanduser().resolve()
        directory = path.parent
        if not directory.is_dir():
            return
        self.last_keymap_dir = directory
        self.last_keymap_file = path
        self.settings["last_keymap_dir"] = str(directory)
        self.settings["last_keymap_file"] = str(path)
        self.settings.pop("last_open_dir", None)
        self._save_settings_quietly()

    def _remember_pdf_location(self, path: Path):
        path = path.expanduser().resolve()
        directory = path.parent
        if not directory.is_dir():
            return
        self.last_pdf_dir = directory
        self.last_pdf_file = path
        self.settings["last_pdf_dir"] = str(directory)
        self.settings["last_pdf_file"] = str(path)
        self._save_settings_quietly()

    def _save_settings_quietly(self):
        try:
            save_editor_settings(self.settings)
        except OSError:
            # Remembering locations is a convenience; a read-only home or
            # config directory must never block opening or saving the keymap.
            pass

    def add_combo(self):
        if not self.document:
            return
        # Require the user to choose positions explicitly. Earlier releases
        # preselected Tab+Q, which made an unedited combo look intentional.
        combo = Combo(name=self._next_combo_name(), positions=[])
        if ComboDialog(self, combo, is_new=True).show():
            self.document.combos.append(combo)
            self.mark_dirty()
            self.refresh_combos()
            self.refresh_activations()

    def _next_combo_name(self) -> str:
        existing = {combo.name for combo in self.document.combos}
        index = 1
        while f"combo_{index}" in existing:
            index += 1
        return f"combo_{index}"

    def selected_combo_index(self) -> Optional[int]:
        selection = self.combo_tree.selection()
        if not selection:
            self.status_var.set("Select a combo first.")
            return None
        return int(selection[0])

    def edit_combo(self):
        index = self.selected_combo_index()
        if index is None:
            return
        original = self.document.combos[index]
        edited = Combo(
            original.name, list(original.positions), original.binding, original.timeout_ms,
            list(original.layers), original.require_prior_idle_ms, original.slow_release
        )
        if ComboDialog(self, edited, is_new=False, editing_index=index).show():
            self.document.combos[index] = edited
            self.mark_dirty()
            self.refresh_combos()
            self.refresh_activations()

    def delete_combo(self):
        from tkinter import messagebox
        index = self.selected_combo_index()
        if index is None:
            return
        combo = self.document.combos[index]
        if messagebox.askyesno("Delete combo", f'Delete combo "{combo.name}"?'):
            del self.document.combos[index]
            self.mark_dirty()
            self.refresh_combos()
            self.refresh_activations()

    def _show_error(self, title: str, message: str):
        from tkinter import messagebox
        messagebox.showerror(title, message)

    def _on_close(self):
        if self._confirm_discard():
            self.root.destroy()


class ConditionalLayerDialog:
    def __init__(
        self, app: CorneKeymapApp, item: ConditionalLayer, is_new: bool,
        editing_index: Optional[int] = None,
    ):
        tk, ttk = app.tk, app.ttk
        self.app = app
        self.item = item
        self.editing_index = editing_index
        self.result = False
        self.window = tk.Toplevel(app.root)
        self.window.title("Add conditional activation" if is_new else "Edit conditional activation")
        self.window.transient(app.root)
        self.window.grab_set()
        self.window.resizable(False, False)

        form = ttk.Frame(self.window, padding=14)
        form.pack(fill="both", expand=True)
        values = [f"{index}: {layer.display_name}" for index, layer in enumerate(app.document.layers)]
        first = item.if_layers[0] if item.if_layers else 0
        second = item.if_layers[1] if len(item.if_layers) > 1 else min(1, len(values) - 1)
        self.name_var = tk.StringVar(value=item.name)
        self.first_var = tk.StringVar()
        self.second_var = tk.StringVar()
        self.target_var = tk.StringVar()

        ttk.Label(form, text="Name:").grid(row=0, column=0, sticky="e", padx=5, pady=4)
        ttk.Entry(form, textvariable=self.name_var, width=30).grid(row=0, column=1, sticky="ew", padx=5, pady=4)
        ttk.Label(form, text="First active layer:").grid(row=1, column=0, sticky="e", padx=5, pady=4)
        self.first_combo = ttk.Combobox(form, textvariable=self.first_var, values=values, state="readonly", width=30)
        self.first_combo.grid(row=1, column=1, padx=5, pady=4)
        self.first_combo.current(first)
        ttk.Label(form, text="Second active layer:").grid(row=2, column=0, sticky="e", padx=5, pady=4)
        self.second_combo = ttk.Combobox(form, textvariable=self.second_var, values=values, state="readonly", width=30)
        self.second_combo.grid(row=2, column=1, padx=5, pady=4)
        self.second_combo.current(second)
        ttk.Label(form, text="Layer to activate:").grid(row=3, column=0, sticky="e", padx=5, pady=4)
        self.target_combo = ttk.Combobox(form, textvariable=self.target_var, values=values, state="readonly", width=30)
        self.target_combo.grid(row=3, column=1, padx=5, pady=4)
        self.target_combo.current(item.then_layer)
        ttk.Label(
            form,
            text="The target should normally have a higher layer number than both source layers.",
            wraplength=430,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 4))
        buttons = ttk.Frame(form)
        buttons.grid(row=5, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.window.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text="OK", command=self.accept).pack(side="right", padx=4)
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

    def show(self) -> bool:
        self.window.wait_visibility()
        self.window.focus_set()
        self.window.wait_window()
        return self.result

    def accept(self):
        from tkinter import messagebox
        name = self.name_var.get().strip()
        first = self.first_combo.current()
        second = self.second_combo.current()
        target = self.target_combo.current()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
            messagebox.showerror("Invalid activation", "Enter a valid devicetree node name.", parent=self.window)
            return
        for index, existing in enumerate(self.app.document.conditional_layers):
            if existing.name == name and index != self.editing_index:
                messagebox.showerror("Invalid activation", f'An activation named "{name}" already exists.', parent=self.window)
                return
        if first == second:
            messagebox.showerror("Invalid activation", "Choose two different source layers.", parent=self.window)
            return
        if target in (first, second):
            messagebox.showerror("Invalid activation", "The activated layer cannot also be one of the source layers.", parent=self.window)
            return
        self.item.name = name
        self.item.if_layers = [first, second]
        self.item.then_layer = target
        self.result = True
        self.window.destroy()


class LayerMultiSelect:
    """Reusable multi-select list for combo layer restrictions."""

    def __init__(self, parent, app: CorneKeymapApp, selected: Iterable[int] = ()):
        tk, ttk = app.tk, app.ttk
        self.app = app
        self.frame = ttk.Frame(parent)
        self.values = [f"{index}: {layer.display_name}" for index, layer in enumerate(app.document.layers)]
        self.listbox = tk.Listbox(
            self.frame,
            selectmode=tk.MULTIPLE,
            exportselection=False,
            height=min(6, max(3, len(self.values))),
            width=34,
            bg=app.colors["field"],
            fg=app.colors["foreground"],
            selectbackground=app.colors["accent"],
            selectforeground="#ffffff",
            highlightbackground=app.colors["border"],
            relief="sunken",
        )
        scrollbar = ttk.Scrollbar(self.frame, orient="vertical", command=self.listbox.yview)
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        ttk.Button(self.frame, text="All layers (clear selection)", command=self.clear).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0)
        )
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)
        for value in self.values:
            self.listbox.insert(tk.END, value)
        self.set_selected(selected)

    def grid(self, **kwargs):
        self.frame.grid(**kwargs)

    def bind_changed(self, callback):
        self.listbox.bind("<<ListboxSelect>>", callback, add="+")

    def get_selected(self) -> list[int]:
        return [int(index) for index in self.listbox.curselection()]

    def set_selected(self, layers: Iterable[int]):
        self.listbox.selection_clear(0, self.app.tk.END)
        for layer in sorted(set(int(value) for value in layers)):
            if 0 <= layer < len(self.values):
                self.listbox.selection_set(layer)

    def select_layer(self, layer: int):
        if 0 <= layer < len(self.values):
            self.listbox.selection_set(layer)
            self.listbox.see(layer)

    def clear(self):
        self.listbox.selection_clear(0, self.app.tk.END)
        self.listbox.event_generate("<<ListboxSelect>>")


class LayerPairDialog:
    def __init__(
        self, app: CorneKeymapApp, combo: Combo, is_new: bool,
        editing_index: Optional[int] = None,
    ):
        tk, ttk = app.tk, app.ttk
        self.app = app
        self.combo = combo
        self.editing_index = editing_index
        self.result = False
        self.selected = set(combo.positions)
        self.window = tk.Toplevel(app.root)
        self.window.title("Add physical-key layer activation" if is_new else "Edit physical-key layer activation")
        self.window.transient(app.root)
        self.window.grab_set()
        self.window.resizable(True, True)

        match = re.fullmatch(r"&(mo|tog|to)\s+(\d+)", normalize_binding(combo.binding))
        code, target_text = match.groups() if match else ("mo", "0")
        behavior = next(name for name, value in LAYER_BEHAVIORS.items() if value == code)
        target = int(target_text)

        form = ttk.Frame(self.window, padding=12)
        form.pack(fill="both", expand=True)
        layer_values = [f"{index}: {layer.display_name}" for index, layer in enumerate(app.document.layers)]
        self.name_var = tk.StringVar(value=combo.name)
        self.behavior_var = tk.StringVar(value=behavior)
        self.target_var = tk.StringVar()
        self.timeout_var = tk.StringVar(value=str(combo.timeout_ms))
        self.idle_var = tk.StringVar(value="" if combo.require_prior_idle_ms is None else str(combo.require_prior_idle_ms))

        ttk.Label(form, text="Name:").grid(row=0, column=0, sticky="e", padx=5, pady=3)
        ttk.Entry(form, textvariable=self.name_var, width=28).grid(row=0, column=1, sticky="ew", padx=5, pady=3)
        ttk.Label(form, text="Layer behavior:").grid(row=1, column=0, sticky="e", padx=5, pady=3)
        self.behavior_combo = ttk.Combobox(
            form, textvariable=self.behavior_var, values=list(LAYER_BEHAVIORS), state="readonly", width=24,
        )
        self.behavior_combo.grid(row=1, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(form, text="Target layer:").grid(row=2, column=0, sticky="e", padx=5, pady=3)
        self.target_combo = ttk.Combobox(form, textvariable=self.target_var, values=layer_values, state="readonly", width=28)
        self.target_combo.grid(row=2, column=1, sticky="w", padx=5, pady=3)
        self.target_combo.current(target if target < len(layer_values) else 0)
        ttk.Label(form, text="Simultaneous window (ms):").grid(row=3, column=0, sticky="e", padx=5, pady=3)
        ttk.Entry(form, textvariable=self.timeout_var, width=10).grid(row=3, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(form, text="Available on layers:").grid(row=4, column=0, sticky="ne", padx=5, pady=3)
        initial_layers = ensure_toggle_target_layer(combo.binding, combo.layers)
        self.layer_selector = LayerMultiSelect(form, app, initial_layers)
        self.layer_selector.grid(row=4, column=1, columnspan=2, sticky="nsew", padx=5, pady=3)
        ttk.Label(form, text="Prior idle (ms, optional):").grid(row=5, column=0, sticky="e", padx=5, pady=3)
        ttk.Entry(form, textvariable=self.idle_var, width=10).grid(row=5, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(form, text="Select exactly two physical key positions:").grid(
            row=6, column=0, columnspan=3, sticky="w", pady=(10, 3)
        )

        keyboard = ttk.Frame(form)
        keyboard.grid(row=7, column=0, columnspan=3, sticky="nsew")
        self.position_buttons = []
        self.default_button_bg = None
        base = app.document.layers[0]
        layer_names = [layer.display_name for layer in app.document.layers]
        for index in range(KEY_COUNT):
            row, column = corne_grid_position(index)
            label = binding_label(base.bindings[index], layer_names, 6) or "—"
            button = tk.Button(
                keyboard, text=f"{index}\n{label}", width=6, height=2,
                command=lambda position=index: self.toggle_position(position),
            )
            button.grid(row=row, column=column, padx=2, pady=2, sticky="nsew")
            self.position_buttons.append(button)
            if self.default_button_bg is None:
                self.default_button_bg = button.cget("bg")
        configure_corne_grid(keyboard, CORNE_DIALOG_GAP_PX)
        self._refresh_positions()

        self.conflict_var = tk.StringVar(value="")
        ttk.Label(
            form, textvariable=self.conflict_var, style="Warning.TLabel", wraplength=700, justify="left"
        ).grid(row=8, column=0, columnspan=3, sticky="w", pady=(7, 0))
        ttk.Label(
            form,
            text="Momentary activation automatically uses slow release so the layer remains active until both keys are released.",
            wraplength=700,
        ).grid(row=9, column=0, columnspan=3, sticky="w", pady=(6, 0))
        buttons = ttk.Frame(form)
        buttons.grid(row=10, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="Cancel", command=self.window.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text="OK", command=self.accept).pack(side="right", padx=4)
        form.columnconfigure(1, weight=1)
        form.rowconfigure(7, weight=1)
        self.behavior_combo.bind("<<ComboboxSelected>>", self._on_behavior_or_target_changed, add="+")
        self.target_combo.bind("<<ComboboxSelected>>", self._on_behavior_or_target_changed, add="+")
        self.layer_selector.bind_changed(lambda _event: self._refresh_shortcut_warning())
        self._sync_toggle_layers(auto_include=True)
        self._refresh_shortcut_warning()
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

    def show(self) -> bool:
        self.window.wait_visibility()
        self.window.focus_set()
        self.window.wait_window()
        return self.result

    def toggle_position(self, position: int):
        if position in self.selected:
            self.selected.remove(position)
        elif len(self.selected) < 2:
            self.selected.add(position)
        else:
            self.app.status_var.set("A physical layer activation uses exactly two positions; deselect one first.")
        self._refresh_positions()

    def _refresh_positions(self):
        for index, button in enumerate(self.position_buttons):
            button.configure(
                relief="sunken" if index in self.selected else "raised",
                bg=self.app.colors["selected"] if index in self.selected else self.default_button_bg,
            )
        if hasattr(self, "conflict_var"):
            self._refresh_shortcut_warning()

    def _refresh_shortcut_warning(self):
        if not hasattr(self, "conflict_var"):
            return
        source_layers = self.layer_selector.get_selected()
        binding = layer_behavior_binding(self.behavior_var.get(), max(0, self.target_combo.current()))
        draft = Combo(
            name=self.name_var.get().strip() or "layer_pair",
            positions=sorted(self.selected),
            binding=binding,
            layers=source_layers,
        )
        warnings = combo_shortcut_warnings(
            draft, self.app.document, self.editing_index if self.editing_index is not None else -1
        )
        messages = [warning.message for warning in warnings]
        toggle_warning = toggle_layer_scope_warning(binding, source_layers)
        if toggle_warning:
            messages.insert(0, toggle_warning)
        self.conflict_var.set("Warning: " + " ".join(messages) if messages else "")

    def _on_behavior_or_target_changed(self, _event=None):
        self._sync_toggle_layers(auto_include=True)
        self._refresh_shortcut_warning()

    def _sync_toggle_layers(self, auto_include: bool):
        target = max(0, self.target_combo.current())
        binding = layer_behavior_binding(self.behavior_var.get(), target)
        selected = self.layer_selector.get_selected()
        if auto_include and toggle_target_layer(binding) is not None:
            if not selected:
                selected = [0]
            selected = ensure_toggle_target_layer(binding, selected)
            self.layer_selector.set_selected(selected)

    def accept(self):
        from tkinter import messagebox
        name = self.name_var.get().strip()
        try:
            timeout = int(self.timeout_var.get())
            source_layers = self.layer_selector.get_selected()
            idle_text = self.idle_var.get().strip()
            prior_idle = int(idle_text) if idle_text else None
        except ValueError:
            messagebox.showerror("Invalid activation", "Timing and layer values must be whole numbers.", parent=self.window)
            return
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
            messagebox.showerror("Invalid activation", "Enter a valid devicetree node name.", parent=self.window)
            return
        for index, existing in enumerate(self.app.document.combos):
            if existing.name == name and index != self.editing_index:
                messagebox.showerror("Invalid activation", f'A combo named "{name}" already exists.', parent=self.window)
                return
        if len(self.selected) != 2:
            messagebox.showerror("Invalid activation", "Select exactly two physical key positions.", parent=self.window)
            return
        if timeout <= 0 or (prior_idle is not None and prior_idle < 0):
            messagebox.showerror("Invalid activation", "Timing values must be positive.", parent=self.window)
            return
        if any(layer < 0 or layer >= len(self.app.document.layers) for layer in source_layers):
            messagebox.showerror(
                "Invalid activation", f"Available layers must be between 0 and {len(self.app.document.layers) - 1}.",
                parent=self.window,
            )
            return
        try:
            updated = layer_pair_combo(
                name, sorted(self.selected), self.target_combo.current(), self.behavior_var.get(), timeout, source_layers
            )
        except KeymapError as exc:
            messagebox.showerror("Invalid activation", str(exc), parent=self.window)
            return
        updated.require_prior_idle_ms = prior_idle
        self.combo.name = updated.name
        self.combo.positions = updated.positions
        self.combo.binding = updated.binding
        self.combo.timeout_ms = updated.timeout_ms
        self.combo.layers = updated.layers
        self.combo.require_prior_idle_ms = updated.require_prior_idle_ms
        self.combo.slow_release = updated.slow_release
        self.result = True
        self.window.destroy()


class ComboDialog:
    def __init__(self, app: CorneKeymapApp, combo: Combo, is_new: bool, editing_index: Optional[int] = None):
        tk, ttk = app.tk, app.ttk
        self.app = app
        self.combo = combo
        self.is_new = is_new
        self.editing_index = editing_index
        self.result = False
        self.capture_armed = False
        self.selected = set(combo.positions)

        self.window = tk.Toplevel(app.root)
        self.window.title("Add combo" if is_new else "Edit combo")
        self.window.transient(app.root)
        self.window.grab_set()
        self.window.resizable(True, True)

        form = ttk.Frame(self.window, padding=12)
        form.pack(fill="both", expand=True)
        self.name_var = tk.StringVar(value=combo.name)
        self.binding_var = tk.StringVar(value=combo.binding)
        self.timeout_var = tk.StringVar(value=str(combo.timeout_ms))
        self.idle_var = tk.StringVar(value="" if combo.require_prior_idle_ms is None else str(combo.require_prior_idle_ms))
        self.slow_var = tk.BooleanVar(value=combo.slow_release)

        ttk.Label(form, text="Name:").grid(row=0, column=0, sticky="e", padx=5, pady=3)
        ttk.Entry(form, textvariable=self.name_var, width=24).grid(row=0, column=1, sticky="ew", padx=5, pady=3)
        ttk.Label(form, text="Output binding:").grid(row=1, column=0, sticky="e", padx=5, pady=3)
        self.binding_entry = ttk.Entry(form, textvariable=self.binding_var, width=35)
        self.binding_entry.grid(row=1, column=1, sticky="ew", padx=5, pady=3)
        self.capture_button = ttk.Button(form, text="Capture typed key", command=self.arm_capture)
        self.capture_button.grid(row=1, column=2, padx=5, pady=3)
        ttk.Label(form, text="Timeout (ms):").grid(row=2, column=0, sticky="e", padx=5, pady=3)
        ttk.Entry(form, textvariable=self.timeout_var, width=10).grid(row=2, column=1, sticky="w", padx=5, pady=3)
        ttk.Label(form, text="Available on layers:").grid(row=3, column=0, sticky="ne", padx=5, pady=3)
        initial_layers = ensure_toggle_target_layer(combo.binding, combo.layers)
        self.layer_selector = LayerMultiSelect(form, app, initial_layers)
        self.layer_selector.grid(row=3, column=1, columnspan=2, sticky="nsew", padx=5, pady=3)
        ttk.Label(form, text="Prior idle (ms, optional):").grid(row=4, column=0, sticky="e", padx=5, pady=3)
        ttk.Entry(form, textvariable=self.idle_var, width=10).grid(row=4, column=1, sticky="w", padx=5, pady=3)
        ttk.Checkbutton(form, text="Slow release", variable=self.slow_var).grid(row=4, column=2, sticky="w", padx=5)

        self.conflict_var = tk.StringVar(value="")
        ttk.Label(
            form, textvariable=self.conflict_var, style="Warning.TLabel", wraplength=700, justify="left"
        ).grid(row=5, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Label(form, text="Click at least two trigger positions:").grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 3))
        keyboard = ttk.Frame(form)
        keyboard.grid(row=7, column=0, columnspan=3, sticky="nsew")
        self.position_buttons = []
        self.default_button_bg = None
        base = app.document.layers[0]
        layer_names = [layer.display_name for layer in app.document.layers]
        for index in range(KEY_COUNT):
            row, column = corne_grid_position(index)
            label = binding_label(base.bindings[index], layer_names, 6) or "—"
            button = tk.Button(keyboard, text=f"{index}\n{label}", width=6, height=2, command=lambda i=index: self.toggle_position(i))
            button.grid(row=row, column=column, padx=2, pady=2, sticky="nsew")
            self.position_buttons.append(button)
            if self.default_button_bg is None:
                self.default_button_bg = button.cget("bg")
        configure_corne_grid(keyboard, CORNE_DIALOG_GAP_PX)
        self._refresh_positions()

        buttons = ttk.Frame(form)
        buttons.grid(row=8, column=0, columnspan=3, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self.window.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text="OK", command=self.accept).pack(side="right", padx=4)
        form.columnconfigure(1, weight=1)
        form.rowconfigure(7, weight=1)
        self.binding_var.trace_add("write", lambda *_args: self._on_binding_changed())
        self.layer_selector.bind_changed(lambda _event: self._refresh_shortcut_warning())
        self._sync_toggle_layers(auto_include=True)
        self._refresh_shortcut_warning()
        self.window.bind("<KeyPress>", self._capture_key, add="+")
        self.window.protocol("WM_DELETE_WINDOW", self.window.destroy)

    def show(self) -> bool:
        self.window.wait_visibility()
        self.window.focus_set()
        self.window.wait_window()
        return self.result

    def toggle_position(self, index: int):
        if index in self.selected:
            self.selected.remove(index)
        else:
            self.selected.add(index)
        self._refresh_positions()

    def _refresh_positions(self):
        for index, button in enumerate(self.position_buttons):
            button.configure(relief="sunken" if index in self.selected else "raised", bg=self.app.colors["selected"] if index in self.selected else self.default_button_bg)
        if hasattr(self, "conflict_var"):
            self._refresh_shortcut_warning()

    def _refresh_shortcut_warning(self):
        if not hasattr(self, "conflict_var"):
            return
        layers = self.layer_selector.get_selected()
        binding = normalize_binding(self.binding_var.get())
        draft = Combo(
            name=self.name_var.get().strip() or "combo",
            positions=sorted(self.selected),
            binding=binding,
            layers=layers,
        )
        warnings = combo_shortcut_warnings(
            draft, self.app.document, self.editing_index if self.editing_index is not None else -1
        )
        messages = [warning.message for warning in warnings]
        toggle_warning = toggle_layer_scope_warning(binding, layers)
        if toggle_warning:
            messages.insert(0, toggle_warning)
        self.conflict_var.set("Warning: " + " ".join(messages) if messages else "")

    def _on_binding_changed(self):
        self._sync_toggle_layers(auto_include=True)
        self._refresh_shortcut_warning()

    def _sync_toggle_layers(self, auto_include: bool):
        binding = normalize_binding(self.binding_var.get())
        selected = self.layer_selector.get_selected()
        target = toggle_target_layer(binding)
        if auto_include and target is not None:
            if not selected:
                selected = [0]
            selected = ensure_toggle_target_layer(binding, selected)
            self.layer_selector.set_selected(selected)

    def arm_capture(self):
        self.capture_armed = True
        self.capture_button.configure(text="Type a key now…")
        self.window.focus_set()

    def _capture_key(self, event):
        if not self.capture_armed:
            return
        if event.keysym in MODIFIER_KEYSYMS:
            return "break"
        binding = keysym_to_binding(event.keysym, event.char)
        if binding:
            self.binding_var.set(binding)
            self.capture_armed = False
            self.capture_button.configure(text="Capture typed key")
            return "break"

    def accept(self):
        from tkinter import messagebox
        name = self.name_var.get().strip()
        binding = normalize_binding(self.binding_var.get())
        try:
            timeout = int(self.timeout_var.get())
            layers = self.layer_selector.get_selected()
            idle_text = self.idle_var.get().strip()
            idle = int(idle_text) if idle_text else None
        except ValueError:
            messagebox.showerror("Invalid combo", "Timeout, layers, and prior-idle values must be whole numbers.", parent=self.window)
            return
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", name):
            messagebox.showerror("Invalid combo", "Name must start with a letter or underscore and contain only letters, numbers, underscores, or hyphens.", parent=self.window)
            return
        for index, existing in enumerate(self.app.document.combos):
            if existing.name == name and index != self.editing_index:
                messagebox.showerror("Invalid combo", f'A combo named "{name}" already exists.', parent=self.window)
                return
        if len(self.selected) < 2:
            messagebox.showerror("Invalid combo", "Select at least two key positions.", parent=self.window)
            return
        if not binding.startswith("&"):
            messagebox.showerror("Invalid combo", 'The output binding must start with "&".', parent=self.window)
            return
        behavior_error = layer_behavior_error(binding)
        if behavior_error:
            messagebox.showerror("Invalid combo", behavior_error, parent=self.window)
            return
        if timeout <= 0 or (idle is not None and idle < 0):
            messagebox.showerror("Invalid combo", "Timeout must be positive, and prior idle cannot be negative.", parent=self.window)
            return
        if any(layer < 0 or layer >= len(self.app.document.layers) for layer in layers):
            messagebox.showerror("Invalid combo", f"Layers must be between 0 and {len(self.app.document.layers) - 1}.", parent=self.window)
            return
        self.combo.name = name
        self.combo.positions = sorted(self.selected)
        self.combo.binding = binding
        self.combo.timeout_ms = timeout
        self.combo.layers = layers
        self.combo.require_prior_idle_ms = idle
        self.combo.slow_release = self.slow_var.get()
        self.result = True
        self.window.destroy()


def check_file(path: Path) -> int:
    try:
        document = KeymapDocument.from_file(path)
        rendered = document.render()
        reparsed = KeymapDocument.from_text(rendered)
    except (OSError, KeymapError) as exc:
        print(f"INVALID: {exc}", file=sys.stderr)
        return 1
    print(f"VALID: {path}")
    if document.repair_notes:
        print(f"Repairs applied in memory: {len(document.repair_notes)}")
        for note in document.repair_notes:
            print(f"  - {note}")
    print(
        f"Layers: {len(reparsed.layers)} ("
        + ", ".join(
            f"{layer.display_name} [{layer.name}]" if layer.display_name != layer.name else layer.name
            for layer in reparsed.layers
        )
        + ")"
    )
    print(f"Conditional activations: {len(reparsed.conditional_layers)}")
    print(f"Combos: {len(reparsed.combos)}")
    warnings = analyze_shortcut_conflicts(reparsed)
    print(f"Shortcut warnings: {len(warnings)}")
    for warning in warnings:
        print(f"  - {warning.source}: {warning.shortcut} - {warning.message}")
    return 0


def repair_file(input_path: Path, output_path: Path) -> int:
    try:
        document = KeymapDocument.from_file(input_path)
        rendered = document.render()
        verified = KeymapDocument.from_text(rendered, auto_repair=False)
        errors = verified.validate()
        if errors:
            raise KeymapError("\n".join(errors))
        output_path.write_text(rendered, encoding="utf-8")
    except (OSError, KeymapError) as exc:
        print(f"REPAIR FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"REPAIRED: {input_path} -> {output_path}")
    if document.repair_notes:
        for note in document.repair_notes:
            print(f"  - {note}")
    else:
        print("  No known malformed activation syntax was found; the validated keymap was copied.")
    return 0


def export_pdf_file(input_path: Path, output_path: Path) -> int:
    try:
        document = KeymapDocument.from_file(input_path)
        export_keymap_pdf(document, output_path, title="CORNE 42-KEY KEYMAP")
    except (OSError, KeymapError) as exc:
        print(f"PDF EXPORT FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"PDF EXPORTED: {input_path} -> {output_path}")
    return 0


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Edit 42-key ZMK Corne keymaps with a graphical keyboard layout.")
    parser.add_argument("file", nargs="?", type=Path, help="corne.keymap file to open")
    parser.add_argument("--check", action="store_true", help="parse, validate, render, and reparse FILE without starting the GUI")
    parser.add_argument("--repair-output", type=Path, metavar="OUTPUT", help="repair FILE and save a validated corrected keymap to OUTPUT")
    parser.add_argument("--export-pdf", type=Path, metavar="OUTPUT", help="create a printable PDF cheat sheet from FILE")
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    args = parser.parse_args(argv)
    if args.check:
        if not args.file:
            parser.error("--check requires FILE")
        return check_file(args.file)
    if args.repair_output:
        if not args.file:
            parser.error("--repair-output requires FILE")
        return repair_file(args.file, args.repair_output)
    if args.export_pdf:
        if not args.file:
            parser.error("--export-pdf requires FILE")
        return export_pdf_file(args.file, args.export_pdf)

    try:
        import tkinter as tk
    except ImportError:
        print("Tkinter is not installed. On Debian/Ubuntu, run: sudo apt install python3-tk", file=sys.stderr)
        return 2
    root = tk.Tk()
    CorneKeymapApp(root, args.file)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
