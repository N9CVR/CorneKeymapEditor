#!/usr/bin/env python3
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from corne_keymap_editor import (
    CORNE_DIALOG_GAP_PX, CORNE_MAIN_GAP_PX, CORNE_SPLIT_COLUMN,
    Combo, ConditionalLayer, KeymapDocument, KeymapError, analyze_shortcut_conflicts,
    cinnamon_appearance, configure_corne_grid, corne_grid_position,
    ensure_toggle_target_layer, export_keymap_pdf, is_layer_pair_combo, key_binding_shortcut_warning,
    keysym_to_binding, layer_behavior_binding, layer_pair_combo, load_editor_settings,
    native_file_dialog, normalize_cinnamon_font, normalize_layer_identifier,
    repair_file, save_editor_settings, toggle_layer_scope_warning,
)


SAMPLE = Path(__file__).parent / "source" / "corne.keymap.txt"
FAILED_F_KEYS = Path(__file__).parent / "source" / "corne.failed-f-keys.keymap.txt"


class KeymapDocumentTests(unittest.TestCase):
    def setUp(self):
        self.text = SAMPLE.read_text(encoding="utf-8")
        self.document = KeymapDocument.from_text(self.text)

    def test_attached_current_file_has_five_42_key_layers(self):
        self.assertEqual([layer.name for layer in self.document.layers], [
            "default_layer", "lower_layer", "raise_layer", "adjust_layer", "bt_led"
        ])
        self.assertEqual(self.document.layers[4].display_name, "BT-LED")
        self.assertTrue(all(len(layer.bindings) == 42 for layer in self.document.layers))
        self.assertEqual(self.document.layers[0].bindings[0], "&kp TAB")
        self.assertEqual(self.document.layers[0].bindings[41], "&kp RALT")

    def test_attached_broken_bluetooth_label_is_repaired(self):
        self.assertIn("&bt BTCLR", self.text)
        self.assertEqual(self.document.layers[4].bindings[35], "&bt BT_CLR")
        self.assertTrue(any("Bluetooth" in note for note in self.document.repair_notes))
        corrected = self.document.render()
        self.assertIn("&bt BT_CLR", corrected)
        self.assertNotIn("&bt BTCLR", corrected)

    def test_unintended_tab_q_brace_combos_are_repaired(self):
        self.assertIn("L_Brace", self.text)
        self.assertIn("R_Brace", self.text)
        combos = {combo.name: combo for combo in self.document.combos}
        self.assertEqual(combos["L_Brace"].positions, [28, 29])
        self.assertEqual(combos["R_Brace"].positions, [30, 31])
        self.assertTrue(any("Tab+Q" in note for note in self.document.repair_notes))
        corrected = self.document.render()
        self.assertIn("key-positions = <28 29>;", corrected)
        self.assertIn("key-positions = <30 31>;", corrected)

    def test_attached_conditional_layer_is_loaded(self):
        self.assertEqual(len(self.document.conditional_layers), 1)
        item = self.document.conditional_layers[0]
        self.assertEqual(item.name, "adjust_layer")
        self.assertEqual(item.if_layers, [1, 2])
        self.assertEqual(item.then_layer, 3)

    def test_binding_edit_round_trip_and_readable_layout(self):
        self.document.layers[0].bindings[0] = "&kp ESC"
        rendered = self.document.render()
        reparsed = KeymapDocument.from_text(rendered)
        self.assertEqual(reparsed.layers[0].bindings[0], "&kp ESC")
        self.assertIn("ESC", rendered)
        self.assertIn("PandaKB Corne V3 MX starter keymap", rendered)
        self.assertIn("if-layers = <1 2>;", rendered)

    def test_combo_insert_and_round_trip(self):
        self.document.combos.append(Combo(
            name="escape_combo",
            positions=[0, 1],
            binding="&kp ESC",
            timeout_ms=45,
            layers=[0, 1],
            require_prior_idle_ms=100,
            slow_release=True,
        ))
        rendered = self.document.render()
        self.assertIn('compatible = "zmk,combos";', rendered)
        reparsed = KeymapDocument.from_text(rendered)
        self.assertEqual(len(reparsed.combos), 10)
        combo = reparsed.combos[-1]
        self.assertEqual(combo.name, "escape_combo")
        self.assertEqual(combo.positions, [0, 1])
        self.assertEqual(combo.binding, "&kp ESC")
        self.assertEqual(combo.layers, [0, 1])
        self.assertEqual(combo.require_prior_idle_ms, 100)
        self.assertTrue(combo.slow_release)

    def test_existing_combo_edit_preserves_node_indentation(self):
        self.document.combos.append(Combo("escape_combo", [0, 1], "&kp ESC"))
        with_combo = self.document.render()
        loaded = KeymapDocument.from_text(with_combo)
        loaded.combos[0].timeout_ms = 60
        edited = loaded.render()
        self.assertIn("\n    combos {", edited)
        self.assertNotIn("\n        combos {", edited)
        reparsed = KeymapDocument.from_text(edited)
        self.assertEqual(reparsed.combos[0].timeout_ms, 60)
        self.assertEqual(reparsed.render(), edited)

    def test_second_render_is_stable(self):
        first = self.document.render()
        second = KeymapDocument.from_text(first).render()
        self.assertEqual(first, second)

    def test_add_transparent_layer_and_select_it(self):
        new_index = self.document.add_layer("navigation_layer")
        self.assertEqual(new_index, 5)
        self.document.layers[0].bindings[36] = layer_behavior_binding("Momentary (while held)", new_index)
        rendered = self.document.render()
        reparsed = KeymapDocument.from_text(rendered)
        self.assertEqual([layer.name for layer in reparsed.layers][-1], "navigation_layer")
        self.assertEqual(reparsed.layers[0].bindings[36], "&mo 5")
        self.assertEqual(reparsed.layers[5].bindings, ["&trans"] * 42)
        self.assertEqual(reparsed.layers[5].display_name, "navigation_layer")
        self.assertIn('display-name = "navigation_layer";', rendered)
        self.assertEqual(reparsed.render(), rendered)

    def test_add_layer_by_copying_current_layer(self):
        copied = list(self.document.layers[2].bindings)
        new_index = self.document.add_layer("symbols_two_layer", copied)
        rendered = self.document.render()
        reparsed = KeymapDocument.from_text(rendered)
        self.assertEqual(new_index, 5)
        self.assertEqual(reparsed.layers[5].bindings, copied)

    def test_added_layer_name_must_be_unique_and_valid(self):
        with self.assertRaises(KeymapError):
            self.document.add_layer("lower_layer")
        with self.assertRaises(KeymapError):
            self.document.add_layer("")

    def test_rename_layer_preserves_numeric_references_and_updates_layout_labels(self):
        self.document.rename_layer(1, "numbers_layer")
        rendered = self.document.render()
        self.assertIn("numbers_layer {", rendered)
        self.assertNotIn("lower_layer {", rendered)
        self.assertIn('display-name = "numbers_layer";', rendered)
        self.assertIn("&mo 1", rendered)
        self.assertIn("NUMBER", rendered)
        reparsed = KeymapDocument.from_text(rendered, auto_repair=False)
        self.assertEqual(reparsed.layers[1].name, "numbers_layer")
        self.assertEqual(reparsed.layers[1].display_name, "numbers_layer")
        self.assertEqual(reparsed.layers[0].bindings[37], "&mo 1")

    def test_rename_layer_updates_existing_display_name_in_place(self):
        source = self.text.replace(
            "        lower_layer {\n",
            '        lower_layer {\n            display-name = "Old Lower Label";\n',
        )
        document = KeymapDocument.from_text(source)
        self.assertEqual(document.layers[1].display_name, "Old Lower Label")
        document.rename_layer(1, "numbers_layer")
        rendered = document.render()
        self.assertIn('display-name = "numbers_layer";', rendered)
        self.assertNotIn("Old Lower Label", rendered)
        self.assertEqual(rendered.count('display-name = "numbers_layer";'), 1)

    def make_legacy_rename_syntax_failure(self):
        document = KeymapDocument.from_text(self.text)
        document.rename_layer(2, "symbols_layer")
        good = document.render()
        # Reproduce the older overlapping-edit failure: the display-name write
        # was lost and one ASCII-layout row remained without its // prefix.
        broken = good.replace('            display-name = "symbols_layer";\n', "", 1)
        orphan = "            |  GUI   |        |  ENT   |   |  SPC   |        |  ALT   |\n"
        marker = "            bindings = <\n"
        layer_start = broken.index("        symbols_layer {")
        binding_start = broken.index(marker, layer_start)
        broken = broken[:binding_start] + orphan + broken[binding_start:]
        return good, broken, orphan.strip()

    def test_legacy_orphaned_layout_fragment_is_repaired_on_open(self):
        _good, broken, orphan = self.make_legacy_rename_syntax_failure()
        recovered = KeymapDocument.from_text(broken)
        self.assertEqual(recovered.layers[2].name, "symbols_layer")
        self.assertEqual(recovered.layers[2].bindings, self.document.layers[2].bindings)
        self.assertEqual(len(recovered.combos), len(self.document.combos))
        self.assertEqual(recovered.conditional_layers, self.document.conditional_layers)
        self.assertTrue(any("orphaned human-readable layout fragment" in note for note in recovered.repair_notes))
        corrected = recovered.render()
        self.assertFalse(any(line.strip() == orphan for line in corrected.splitlines()))
        verified = KeymapDocument.from_text(corrected, auto_repair=False)
        self.assertEqual(verified.layers[2].bindings, recovered.layers[2].bindings)

    def test_legacy_orphaned_layout_fragment_is_rejected_without_repair(self):
        _good, broken, _orphan = self.make_legacy_rename_syntax_failure()
        with self.assertRaises(KeymapError) as context:
            KeymapDocument.from_text(broken, auto_repair=False)
        self.assertIn("orphaned human-readable layout fragment", str(context.exception))

    def test_truncated_legacy_layout_fragment_is_also_repaired(self):
        _good, broken, orphan = self.make_legacy_rename_syntax_failure()
        lines = broken.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if line.strip() == orphan:
                newline = "\n" if line.endswith("\n") else ""
                lines[index] = "            GUI |        | ENT |   | SPC |        | ALT" + newline
                break
        broken = "".join(lines)
        recovered = KeymapDocument.from_text(broken)
        self.assertTrue(any("orphaned human-readable layout fragment" in note for note in recovered.repair_notes))
        corrected = recovered.render()
        self.assertNotIn("GUI |        | ENT |   | SPC |        | ALT", corrected)
        KeymapDocument.from_text(corrected, auto_repair=False)

    def test_reported_line_142_devicetree_failure_is_repaired(self):
        _good, broken, orphan = self.make_legacy_rename_syntax_failure()
        broken = broken.replace("        symbols_layer {", "\n\n\n\n\n        symbols_layer {", 1)
        orphan_line = next(
            index for index, line in enumerate(broken.splitlines(), 1) if line.strip() == orphan
        )
        self.assertEqual(orphan_line, 142)
        recovered = KeymapDocument.from_text(broken)
        self.assertTrue(any(note.startswith("Line 142:") for note in recovered.repair_notes))
        corrected = recovered.render()
        KeymapDocument.from_text(corrected, auto_repair=False)

    def test_current_rename_never_emits_bare_layout_rows(self):
        document = KeymapDocument.from_text(self.text)
        document.rename_layer(2, "symbols_layer")
        rendered = document.render()
        bare_layout_rows = [
            line for line in rendered.splitlines()
            if line.strip().startswith("|") and line.strip().endswith("|")
        ]
        self.assertEqual(bare_layout_rows, [])
        KeymapDocument.from_text(rendered, auto_repair=False)

    def test_rename_layer_rejects_duplicate_or_invalid_names(self):
        with self.assertRaises(KeymapError):
            self.document.rename_layer(1, "raise_layer")
        with self.assertRaises(KeymapError):
            self.document.rename_layer(1, "")

    def test_human_layer_name_is_separate_from_safe_identifier(self):
        self.document.rename_layer(4, "BT-LED System")
        layer = self.document.layers[4]
        self.assertEqual(layer.display_name, "BT-LED System")
        self.assertEqual(layer.name, "bt_led_system")
        rendered = self.document.render()
        self.assertIn("        bt_led_system {", rendered)
        self.assertIn('display-name = "BT-LED System";', rendered)
        self.assertNotIn("        BT-LED System {", rendered)
        KeymapDocument.from_text(rendered, auto_repair=False)

    def test_layer_identifier_normalization_handles_hyphens_numbers_and_collisions(self):
        self.assertEqual(normalize_layer_identifier("BT-LED", 4), "bt_led")
        self.assertEqual(normalize_layer_identifier("6 Gaming", 6), "layer_6_gaming")
        first = self.document.add_layer("Mouse Control")
        second = self.document.add_layer("Mouse-Control")
        self.assertEqual(self.document.layers[first].name, "mouse_control")
        self.assertEqual(self.document.layers[second].name, "mouse_control_2")

    def test_hyphenated_layer_identifier_at_reported_column_is_repaired(self):
        bindings = "  ".join(["&trans"] * 42)
        prefix = ["/ {", "    keymap {", '        compatible = "zmk,keymap";']
        prefix.extend([""] * (141 - len(prefix)))
        source = "\n".join(prefix) + "\n"
        source += "        RF-Tools {\n"
        source += f"            bindings = <{bindings}>;\n"
        source += "        };\n    };\n};\n"
        line_number = next(index for index, line in enumerate(source.splitlines(), 1) if line.strip() == "RF-Tools {")
        self.assertEqual(line_number, 142)
        recovered = KeymapDocument.from_text(source)
        repaired = recovered.layers[-1]
        self.assertEqual(repaired.name, "rf_tools")
        self.assertEqual(repaired.display_name, "RF-Tools")
        self.assertTrue(any(note.startswith("Layer 0:") for note in recovered.repair_notes))
        corrected = recovered.render()
        self.assertIn("        rf_tools {", corrected)
        self.assertIn('display-name = "RF-Tools";', corrected)
        KeymapDocument.from_text(corrected, auto_repair=False)

    def test_exact_attached_f_keys_failure_is_repaired(self):
        source = FAILED_F_KEYS.read_text(encoding="utf-8")
        self.assertEqual(source.splitlines()[141].strip(), "F-Keys {")
        recovered = KeymapDocument.from_text(source)
        f_keys = recovered.layers[2]
        self.assertEqual(f_keys.name, "f_keys")
        self.assertEqual(f_keys.display_name, "F-Keys")
        self.assertTrue(any('"F-Keys" to "f_keys"' in note for note in recovered.repair_notes))
        self.assertTrue(any("obsolete layer label property" in note for note in recovered.repair_notes))
        corrected = recovered.render()
        self.assertIn("        f_keys {", corrected)
        self.assertIn('display-name = "F-Keys";', corrected)
        self.assertNotIn("        F-Keys {", corrected)
        self.assertNotRegex(corrected, r'(?m)^\s*label\s*=')
        verified = KeymapDocument.from_text(corrected, auto_repair=False)
        self.assertEqual(verified.layers[2].bindings, f_keys.bindings)
        self.assertEqual(len(verified.combos), len(recovered.combos))
        self.assertEqual(verified.conditional_layers, recovered.conditional_layers)

    def test_unsafe_f_keys_identifier_is_rejected_without_repair(self):
        with self.assertRaises(KeymapError) as context:
            KeymapDocument.from_text(FAILED_F_KEYS.read_text(encoding="utf-8"), auto_repair=False)
        message = str(context.exception)
        self.assertIn('Layer identifier "F-Keys" is not normalized', message)

    def test_current_serializer_never_uses_visible_name_as_identifier(self):
        document = KeymapDocument.from_text(self.text)
        index = document.add_layer("F-Keys")
        self.assertEqual(document.layers[index].name, "f_keys")
        self.assertEqual(document.layers[index].display_name, "F-Keys")
        output = document.render()
        self.assertIn("        f_keys {", output)
        self.assertIn('display-name = "F-Keys";', output)
        self.assertNotIn("        F-Keys {", output)
        KeymapDocument.from_text(output, auto_repair=False)

    def test_obsolete_label_property_is_removed_and_prevented(self):
        source = FAILED_F_KEYS.read_text(encoding="utf-8")
        recovered = KeymapDocument.from_text(source)
        self.assertFalse(any(re.match(r"^\s*label\s*=", line) for line in recovered.render().splitlines()))
        with self.assertRaises(KeymapError) as context:
            KeymapDocument.from_text(source, auto_repair=False)
        self.assertIn("obsolete layer label property", str(context.exception))

    def test_layer_key_behaviors(self):
        self.assertEqual(layer_behavior_binding("Momentary (while held)", 2), "&mo 2")
        self.assertEqual(layer_behavior_binding("Toggle on/off", 3), "&tog 3")
        self.assertEqual(layer_behavior_binding("Switch to layer", 0), "&to 0")

    def test_conditional_activation_edit_round_trip(self):
        self.document.conditional_layers[0].name = "lower_raise_adjust"
        rendered = self.document.render()
        reparsed = KeymapDocument.from_text(rendered)
        self.assertEqual(reparsed.conditional_layers, [ConditionalLayer("lower_raise_adjust", [1, 2], 3)])
        self.assertIn('compatible = "zmk,conditional-layers";', rendered)
        self.assertEqual(reparsed.render(), rendered)

    def test_add_second_conditional_activation(self):
        target = self.document.add_layer("navigation_layer")
        self.document.conditional_layers.append(ConditionalLayer("lower_adjust_navigation", [1, 3], target))
        rendered = self.document.render()
        reparsed = KeymapDocument.from_text(rendered)
        self.assertEqual(reparsed.conditional_layers[-1], ConditionalLayer("lower_adjust_navigation", [1, 3], target))

    def test_momentary_physical_pair_activation(self):
        combo = layer_pair_combo("thumbs_adjust", [36, 39], 3, timeout_ms=60, source_layers=[0])
        self.assertTrue(is_layer_pair_combo(combo))
        self.assertEqual(combo.binding, "&mo 3")
        self.assertTrue(combo.slow_release)
        self.document.combos.append(combo)
        rendered = self.document.render()
        reparsed = KeymapDocument.from_text(rendered)
        loaded = reparsed.combos[-1]
        self.assertEqual(loaded.positions, [36, 39])
        self.assertEqual(loaded.binding, "&mo 3")
        self.assertEqual(loaded.layers, [0])
        self.assertTrue(loaded.slow_release)

    def test_toggle_physical_pair_does_not_need_slow_release(self):
        combo = layer_pair_combo("toggle_nav", [0, 11], 2, "Toggle on/off")
        self.assertEqual(combo.binding, "&tog 2")
        self.assertFalse(combo.slow_release)

    def test_toggle_pair_defaults_to_source_and_target_layers(self):
        combo = layer_pair_combo(
            "Game", [40, 41], 5, "Toggle on/off", source_layers=[0]
        )
        self.assertEqual(combo.positions, [40, 41])
        self.assertEqual(combo.binding, "&tog 5")
        self.assertEqual(combo.layers, [0, 5])

    def test_toggle_scope_warning_when_target_is_missing(self):
        self.assertIn("target layer 5", toggle_layer_scope_warning("&tog 5", [0]))
        self.assertEqual(toggle_layer_scope_warning("&tog 5", [0, 5]), "")
        self.assertEqual(toggle_layer_scope_warning("&tog 5", []), "")
        self.assertEqual(ensure_toggle_target_layer("&tog 5", [0]), [0, 5])

    def test_game_combo_regression_serializes_layers_zero_and_five(self):
        document = KeymapDocument.from_file(FAILED_F_KEYS)
        game = next(combo for combo in document.combos if combo.name == "Game")
        self.assertEqual(game.positions, [40, 41])
        self.assertEqual(game.binding, "&tog 5")
        self.assertEqual(game.layers, [0])
        game.layers = ensure_toggle_target_layer(game.binding, game.layers)
        rendered = document.render()
        self.assertRegex(
            rendered,
            r"(?s)Game\s*\{.*?key-positions\s*=\s*<40 41>;.*?layers\s*=\s*<0 5>;.*?bindings\s*=\s*<&tog 5>;",
        )
        reparsed = KeymapDocument.from_text(rendered, auto_repair=False)
        reloaded = next(combo for combo in reparsed.combos if combo.name == "Game")
        self.assertEqual(reloaded.layers, [0, 5])

    def test_existing_multi_layer_combo_restriction_round_trips(self):
        document = KeymapDocument.from_text(self.document.render())
        document.combos.append(Combo("multi_layer", [2, 3], "&kp ESC", layers=[0, 2, 4]))
        rendered = document.render()
        self.assertIn("layers = <0 2 4>;", rendered)
        reparsed = KeymapDocument.from_text(rendered, auto_repair=False)
        self.assertEqual(reparsed.combos[-1].layers, [0, 2, 4])

    def test_physical_pair_requires_exactly_two_keys(self):
        with self.assertRaises(KeymapError):
            layer_pair_combo("bad_pair", [0], 1)

    def test_conditional_activation_validation(self):
        self.document.conditional_layers.append(ConditionalLayer("bad_condition", [1, 1], 1))
        errors = self.document.validate()
        self.assertTrue(any("two different input layers" in error for error in errors))
        self.assertTrue(any("cannot target" in error for error in errors))

    def make_broken_v120_keymap(self):
        document = KeymapDocument.from_text(self.text)
        navigation = document.add_layer("navigation_layer")
        document.layers[navigation].bindings[18:22] = ["&kp LEFT", "&kp DOWN", "&kp UP", "&kp RIGHT"]
        document.conditional_layers.append(ConditionalLayer("lower_adjust_navigation", [1, 3], navigation))
        document.combos.append(Combo("escape_combo", [0, 1], "&kp ESC", timeout_ms=45))
        document.combos.append(layer_pair_combo("thumb_nav", [36, 39], navigation, timeout_ms=65, source_layers=[0]))
        good = document.render().replace(
            "// |        |        |        |        |        |        |   |  LFT   |  DWN   |   UP   |  RGT   |        |        |",
            "// USER HUMAN-READABLE NAVIGATION LAYOUT — PRESERVE EXACTLY",
        )
        broken = good.replace("if-layers = <1 3>;", "if-layers = <[1, 3]>;")
        broken = broken.replace(f"then-layer = <{navigation}>;", f"then-layer = <{navigation}: navigation_layer>;")
        broken = broken.replace("key-positions = <36 39>;", "key-positions = <36 + 39>;")
        broken = broken.replace("layers = <0>;", "layers = <0: default_layer>;")
        broken = broken.replace(f"bindings = <&mo {navigation}>;", f"bindings = <&mo <{navigation}: navigation_layer>>;")
        return good, broken

    def test_broken_v120_keymap_is_repaired_without_returning_to_original(self):
        good, broken = self.make_broken_v120_keymap()
        recovered = KeymapDocument.from_text(broken)
        self.assertGreaterEqual(len(recovered.repair_notes), 5)
        self.assertEqual(len(recovered.layers), 6)
        self.assertEqual(recovered.layers[5].bindings[18:22], ["&kp LEFT", "&kp DOWN", "&kp UP", "&kp RIGHT"])
        self.assertEqual(len(recovered.combos), 11)
        self.assertEqual(len(recovered.conditional_layers), 2)
        corrected = recovered.render()
        self.assertIn("// USER HUMAN-READABLE NAVIGATION LAYOUT — PRESERVE EXACTLY", corrected)
        self.assertIn("if-layers = <1 3>;", corrected)
        self.assertIn("then-layer = <5>;", corrected)
        self.assertIn("key-positions = <36 39>;", corrected)
        self.assertIn("layers = <0>;", corrected)
        self.assertIn("bindings = <&mo 5>;", corrected)
        self.assertNotIn("5: navigation_layer", corrected)
        verified = KeymapDocument.from_text(corrected, auto_repair=False)
        self.assertEqual(verified.render(), corrected)

    def test_repair_command_writes_corrected_keymap(self):
        _good, broken = self.make_broken_v120_keymap()
        with TemporaryDirectory() as directory:
            source = Path(directory) / "broken.keymap"
            output = Path(directory) / "corne.keymap"
            source.write_text(broken, encoding="utf-8")
            self.assertEqual(repair_file(source, output), 0)
            repaired = output.read_text(encoding="utf-8")
            self.assertNotEqual(repaired, broken)
            verified = KeymapDocument.from_text(repaired, auto_repair=False)
            self.assertEqual(len(verified.layers), 6)
            self.assertEqual(len(verified.combos), 11)

    def test_unrecoverable_layer_behavior_is_rejected(self):
        document = KeymapDocument.from_text(self.text)
        document.layers[0].bindings[36] = "&mo missing_layer"
        with self.assertRaises(KeymapError):
            document.render()

    def test_display_only_bluetooth_label_cannot_be_saved_again(self):
        document = KeymapDocument.from_text(self.text)
        document.layers[4].bindings[35] = "&bt BTCLR"
        with self.assertRaises(KeymapError):
            document.render()

    def test_pdf_cheat_sheet_contains_every_layer(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "cheat-sheet.pdf"
            export_keymap_pdf(self.document, output)
            data = output.read_bytes()
            self.assertTrue(data.startswith(b"%PDF-1.4"))
            self.assertIn(b"/Count 1", data)
            self.assertNotIn(b"SINGLE-SIDED REFERENCE", data)
            self.assertNotIn(b"Generated by Corne Keymap Editor", data)
            for index, name in enumerate(("DEFAULT", "LOWER", "RAISE", "ADJUST", "BT-LED")):
                self.assertIn(f"LAYER {index} - {name}".encode("ascii"), data)

    def test_pdf_shows_only_adjacent_two_key_combo_badges(self):
        document = KeymapDocument.from_text(self.document.render())
        document.combos.append(Combo("adjacent_test", [2, 3], "&kp F23", layers=[0]))
        document.combos.append(Combo("nonadjacent_test", [0, 41], "&kp F24", layers=[0]))
        with TemporaryDirectory() as directory:
            output = Path(directory) / "combos.pdf"
            export_keymap_pdf(document, output)
            data = output.read_bytes()
            self.assertIn(b"(F23)", data)
            self.assertNotIn(b"(F24)", data)

    def test_validation_rejects_bad_combo(self):
        self.document.combos.append(Combo(name="bad combo", positions=[1], binding="kp A", timeout_ms=0))
        errors = self.document.validate()
        self.assertGreaterEqual(len(errors), 4)

    def test_keysym_capture(self):
        self.assertEqual(keysym_to_binding("a", "a"), "&kp A")
        self.assertEqual(keysym_to_binding("1", "1"), "&kp N1")
        self.assertEqual(keysym_to_binding("exclam", "!"), "&kp EXCL")
        self.assertEqual(keysym_to_binding("BackSpace", ""), "&kp BSPC")
        self.assertEqual(keysym_to_binding("F12", ""), "&kp F12")

    def test_corne_grid_restores_unlabeled_gap_between_halves(self):
        class FakeGrid:
            def __init__(self):
                self.columns = {}

            def columnconfigure(self, column, **options):
                self.columns[column] = options

        grid = FakeGrid()
        configure_corne_grid(grid, CORNE_MAIN_GAP_PX)
        self.assertEqual(CORNE_SPLIT_COLUMN, 6)
        self.assertGreaterEqual(CORNE_MAIN_GAP_PX, 40)
        self.assertGreaterEqual(CORNE_DIALOG_GAP_PX, 28)
        self.assertEqual(grid.columns[6]["weight"], 0)
        self.assertEqual(grid.columns[6]["minsize"], CORNE_MAIN_GAP_PX)
        self.assertEqual(corne_grid_position(5), (0, 5))
        self.assertEqual(corne_grid_position(6), (0, 7))
        self.assertEqual(corne_grid_position(38), (3, 5))
        self.assertEqual(corne_grid_position(39), (3, 7))

    def test_gui_does_not_restore_split_word(self):
        source = (Path(__file__).parent / "corne_keymap_editor.py").read_text(encoding="utf-8")
        self.assertNotIn('text="split"', source)
        self.assertNotIn("text='split'", source)

    def test_last_open_directory_settings_round_trip(self):
        with TemporaryDirectory() as directory:
            settings_file = Path(directory) / "config" / "settings.json"
            expected = str(Path(directory).resolve())
            save_editor_settings({"last_open_dir": expected}, settings_file)
            self.assertEqual(load_editor_settings(settings_file)["last_open_dir"], expected)

    def test_keymap_and_pdf_locations_are_stored_separately(self):
        with TemporaryDirectory() as directory:
            settings_file = Path(directory) / "settings.json"
            settings = {
                "last_keymap_dir": str(Path(directory) / "keymaps"),
                "last_keymap_file": str(Path(directory) / "keymaps" / "corne.keymap"),
                "last_pdf_dir": str(Path(directory) / "pdfs"),
                "last_pdf_file": str(Path(directory) / "pdfs" / "cheat.pdf"),
            }
            save_editor_settings(settings, settings_file)
            loaded = load_editor_settings(settings_file)
            self.assertEqual(loaded, settings)

    def test_native_cinnamon_dialog_uses_zenity_and_initial_location(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": "/tmp/corne.keymap\n"})()
        with patch("corne_keymap_editor.shutil.which", return_value="/usr/bin/zenity"), patch(
            "corne_keymap_editor.subprocess.run", return_value=completed
        ) as run:
            handled, selected = native_file_dialog(
                "open", "Open Corne keymap", Path("/tmp"), "previous.keymap",
                [("ZMK keymaps", "*.keymap *.txt")],
            )
        self.assertTrue(handled)
        self.assertEqual(selected, Path("/tmp/corne.keymap"))
        command = run.call_args.args[0]
        self.assertIn("--file-selection", command)
        self.assertIn("--filename=/tmp/previous.keymap", command)
        self.assertIn("--file-filter=ZMK keymaps | *.keymap *.txt", command)

    def test_native_dialog_cancel_does_not_fall_back(self):
        completed = type("Completed", (), {"returncode": 1, "stdout": ""})()
        with patch("corne_keymap_editor.shutil.which", return_value="/usr/bin/zenity"), patch(
            "corne_keymap_editor.subprocess.run", return_value=completed
        ):
            handled, selected = native_file_dialog("save", "Save", Path("/tmp"), "file.pdf")
        self.assertTrue(handled)
        self.assertIsNone(selected)

    def test_cinnamon_dark_appearance_uses_dark_palette_and_theme_font(self):
        palette = cinnamon_appearance({
            "gtk_theme": "Mint-Y-Dark-Aqua",
            "font_name": "Ubuntu 11",
            "color_scheme": "prefer-dark",
        })
        self.assertEqual(palette["dark"], "1")
        self.assertEqual(palette["font"], "Ubuntu 11")
        self.assertEqual(palette["accent"], "#2aa198")
        self.assertEqual(palette["background"], "#2b2b2b")

    def test_cinnamon_light_appearance_uses_light_palette(self):
        palette = cinnamon_appearance({
            "gtk_theme": "Mint-Y-Blue",
            "font_name": "Sans 10",
            "color_scheme": "default",
            "portal_scheme": "0",
        })
        self.assertEqual(palette["dark"], "0")
        self.assertEqual(palette["accent"], "#4a90d9")
        self.assertEqual(palette["background"], "#f4f4f4")

    def test_cinnamon_multiword_font_is_split_into_safe_tk_arguments(self):
        font = normalize_cinnamon_font("Noto Sans Regular 10")
        self.assertEqual(font, {
            "family": "Noto Sans", "size": 10, "weight": "normal", "slant": "roman"
        })

    def test_cinnamon_bold_italic_decimal_font_is_normalized(self):
        font = normalize_cinnamon_font("'Noto Sans Bold Italic 11.6'")
        self.assertEqual(font["family"], "Noto Sans")
        self.assertEqual(font["size"], 12)
        self.assertEqual(font["weight"], "bold")
        self.assertEqual(font["slant"], "italic")

    def test_cinnamon_typed_gsettings_font_is_normalized(self):
        font = normalize_cinnamon_font("@s 'Ubuntu Medium 10'")
        self.assertEqual(font["family"], "Ubuntu")
        self.assertEqual(font["size"], 10)

    def test_bare_numeric_or_empty_font_uses_safe_fallback(self):
        self.assertEqual(normalize_cinnamon_font("10")["family"], "Sans")
        self.assertEqual(normalize_cinnamon_font("10")["size"], 10)
        self.assertEqual(normalize_cinnamon_font("")["family"], "Sans")

    def test_cinnamon_appearance_exposes_scalar_font_arguments_only(self):
        palette = cinnamon_appearance({
            "gtk_theme": None,
            "font_name": "Noto Sans Regular 10",
            "color_scheme": 1,
            "portal_scheme": 0,
        })
        self.assertEqual(palette["font_family"], "Noto Sans")
        self.assertEqual(palette["font_size"], "10")
        self.assertEqual(palette["font_weight"], "normal")
        self.assertEqual(palette["font_slant"], "roman")

    def test_direct_win_arrow_and_ctrl_c_bindings_are_warned(self):
        win_warning = key_binding_shortcut_warning("&kp LG(LEFT)", 0, "default_layer", 5)
        ctrl_warning = key_binding_shortcut_warning("&kp LC(C)", 0, "default_layer", 6)
        self.assertEqual(win_warning.shortcut, "Win/Super+Left")
        self.assertEqual(ctrl_warning.shortcut, "Ctrl+C")

    def test_combo_trigger_that_intercepts_ctrl_c_is_warned(self):
        document = KeymapDocument.from_text(self.document.render())
        document.combos.append(Combo("ctrl_c_interceptor", [12, 27], "&kp ESC", layers=[0]))
        warnings = analyze_shortcut_conflicts(document)
        matching = [warning for warning in warnings if warning.combo_index == len(document.combos) - 1]
        self.assertTrue(any(warning.shortcut == "Ctrl+C" for warning in matching))
        self.assertTrue(any("intercept" in warning.message for warning in matching))

    def test_combo_output_that_sends_common_shortcut_is_warned(self):
        document = KeymapDocument.from_text(self.document.render())
        document.combos.append(Combo("copy_combo", [2, 3], "&kp LC(C)", layers=[0]))
        warnings = analyze_shortcut_conflicts(document)
        matching = [warning for warning in warnings if warning.combo_index == len(document.combos) - 1]
        self.assertTrue(any(warning.shortcut == "Ctrl+C" and "outputs" in warning.message for warning in matching))

    def test_layer_four_functions_warn_at_physical_win_arrow_positions(self):
        document = KeymapDocument.from_text(self.document.render())
        # Match the user's current Layer 4 arrangement: these positions are
        # Left, Down, Up, and Right on Lower, but Layer 4 overrides them.
        document.layers[4].bindings[21] = "&kp C_VOL_UP"
        warnings = analyze_shortcut_conflicts(document)
        affected = {
            warning.key_position: warning
            for warning in warnings
            if warning.layer_index == 4
            and warning.combo_index is not None
            and warning.key_position in (18, 19, 20, 21)
            and warning.shortcut.startswith("Win/Super+")
        }
        self.assertEqual(set(affected), {18, 19, 20, 21})
        self.assertEqual(affected[18].shortcut, "Win/Super+Left")
        self.assertEqual(affected[19].shortcut, "Win/Super+Down")
        self.assertEqual(affected[20].shortcut, "Win/Super+Up")
        self.assertEqual(affected[21].shortcut, "Win/Super+Right")
        self.assertIn("SAI", affected[18].message)
        self.assertIn("BRI", affected[19].message)
        self.assertIn("HUI", affected[20].message)
        self.assertIn("VOLU", affected[21].message)
        self.assertTrue(all("key" in warning.source.lower() for warning in affected.values()))

    def test_current_layer_four_conflicts_are_detected_without_serialized_shortcuts(self):
        warnings = analyze_shortcut_conflicts(self.document)
        affected_positions = {
            warning.key_position
            for warning in warnings
            if warning.layer_index == 4 and warning.shortcut.startswith("Win/Super+")
        }
        self.assertTrue({18, 19, 20}.issubset(affected_positions))

    def test_plain_c_key_is_not_warned(self):
        self.assertIsNone(key_binding_shortcut_warning("&kp C", 0, "default_layer", 27))


if __name__ == "__main__":
    unittest.main(verbosity=2)
