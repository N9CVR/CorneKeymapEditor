# Corne Keymap Editor

A small Linux desktop program for editing a **42-key ZMK Corne** `corne.keymap` file. It loads an existing keymap, displays each layer as a clickable Corne layout, captures a typed key, keeps the human-readable layout comments synchronized, supports ZMK combos, and saves the result to a location you choose.

## Main features

- Opens `.keymap`, `.txt`, or other UTF-8 ZMK source files.
- Remembers the most recently used keymap file/folder and PDF export file/folder separately.
- Uses Cinnamon's GTK/Zenity Open and Save dialogs when available, with Tk fallback dialogs on other systems.
- Finds every 42-binding layer inside the `keymap` node.
- Adds new layers either as all-transparent layouts or as copies of the current layer.
- Renames existing layer nodes and synchronizes their `display-name` labels without changing numeric layer indexes or references.
- Click a GUI key and type a physical key to assign a normal `&kp` binding.
- Dedicated controls assign momentary (`&mo`), toggle (`&tog`), or direct-switch (`&to`) layer keys without requiring ZMK syntax.
- Graphical two-layer conditional activation editor for Lower + Raise → Adjust-style configurations.
- Graphical two-physical-key layer activation editor using standard ZMK combos.
- Raw binding field for behaviors such as `&mo 1`, `&lt 2 SPACE`, `&bt BT_SEL 0`, `&trans`, and `&none`.
- Rebuilds the four-line human-readable Corne layout above every layer's `bindings` property.
- Adds, edits, and removes standard `zmk,combos` entries.
- Combo options: trigger positions, output binding, timeout, layer restriction, prior-idle time, and slow release.
- Recovers malformed activation syntax and display-label substitutions from version 1.2.0 without discarding the rest of the keymap.
- Exports a single-sided landscape PDF cheat sheet with default legends on the keycaps and badges for adjacent two-key combos.
- Warns about keys and combos that send or intercept common Windows/Linux shortcuts such as `Ctrl+C`, `Alt+F4`, and `Win/Super+Arrow`.
- Follows Cinnamon's light/dark preference, desktop font, and Mint theme accent where available.
- Includes a binary `run.sh` launcher that opens as an application instead of a display/run/terminal prompt in Cinnamon's file manager.
- Preserves includes, behaviors, conditional layers, LED configuration, and other source outside the edited layout/binding and combo regions.
- Validates all layers and combos before writing.
- Uses only Python's standard library and Tkinter.

## Run without installing

1. Install Python 3 and Tkinter. On Debian, Ubuntu, Mint, or Pop!_OS:

   ```bash
   sudo apt install python3 python3-tk
   ```

2. Extract this folder and double-click **run.sh**, or run:

   ```bash
   ./run.sh
   ```

   Or open a file directly:

   ```bash
   ./run.sh /path/to/corne.keymap
   ```

Despite its familiar name, `run.sh` is a small compiled application launcher rather than a text shell script. Cinnamon/Nemo therefore launches it directly without asking whether to display it, run it, or run it in a terminal. The included launcher is for 64-bit x86 Linux, matching standard Linux Mint/Cinnamon installations.

## Install for the current user

From this folder:

```bash
chmod +x install.sh
./install.sh
```

This creates a normal application-menu entry with an icon, startup notification, and a `~/.local/bin/corne-keymap-editor` command. It does not require root access. To remove it:

```bash
./uninstall.sh
```

## Editing keys

1. Select a layer from the **Layer** list.
2. Click a key in the on-screen Corne layout.
3. Type the key that should be assigned. Letters, numbers, punctuation, arrows, navigation keys, modifiers, and F1–F24 are recognized.
4. For a non-keypress behavior, enter the complete ZMK expression in **Raw binding** and click **Apply raw binding**.
5. Use **Save As** to choose the output location. If the opened source is named `.txt`, the first save intentionally prompts for a `.keymap` filename.

A modifier pressed by itself is assigned when released. This permits shifted punctuation such as `!` to be captured as `&kp EXCL` instead of being mistaken for `&kp LSHFT`.

## Cinnamon desktop appearance

At startup, the editor reads Cinnamon's current GTK theme, preferred light/dark color scheme, and desktop font using `gsettings`. It then applies a matching light or dark Tk theme, including Mint-Y accent variants such as Aqua, Blue, Orange, Purple, Red, or Teal. On another Linux desktop, it falls back to a neutral system-style palette.

Version 1.5.1 normalizes Cinnamon's Pango font descriptions before applying them. Multiword families, style names, quoted or typed `gsettings` values, decimal sizes, missing values, and malformed values are converted into separate safe Tk family/size/weight/slant arguments. The editor no longer passes a raw string such as `Noto Sans Regular 10` through Tcl's compact font parser, preventing startup errors such as `unknown font style "10"`.

Version 1.6.0 also uses Cinnamon's native GTK file chooser through `zenity` when it is installed. This gives Open, Save As, and PDF Export dialogs the same theme and behavior as the desktop. If `zenity` is unavailable, the editor automatically uses its portable Tk dialogs.

The toolbar no longer displays the absolute file path next to **Rename layer…**. On narrow windows that path was truncated to `/h…`, which looked like stray text. File status remains in the window title and status bar. The visual “split” labels between keyboard halves have also been removed while retaining the physical gap.

Version 1.6.2 restores a deliberate unlabeled spacer column between the left and right Corne halves in the main keymap and both combo-selection diagrams. The word “split” remains removed.

Keymap and PDF locations are independent. Opening or saving a keymap updates `last_keymap_file` and `last_keymap_dir`; exporting a PDF updates `last_pdf_file` and `last_pdf_dir`. A previous `last_open_dir` setting is migrated automatically.

## Adding layers and changing layer keys

1. Click **Add layer…** beside the layer selector.
2. Enter a valid devicetree node name such as `navigation_layer`.
3. Choose whether to copy the displayed layer or start with 42 transparent keys.
4. The new layer is appended to the end of the keymap. Appending is intentional: all existing numeric layer references keep their original meanings.
5. To assign a selector for the new layer, choose the layer that should contain the selector, click its desired key position, and use the **Layer key** panel:
   - **Momentary (while held)** writes `&mo N`.
   - **Toggle on/off** writes `&tog N`.
   - **Switch to layer** writes `&to N`.
6. If moving an existing selector, select its old position and click **Transparent** after assigning the replacement.

New layers are immediately available as combo layer restrictions and as targets for other layer keys.

### Renaming a layer

Select the layer and click **Rename layer…**. Enter the human-readable name you want to see, including spaces, capitalization, or hyphens. The editor stores that exact text in ZMK's quoted `display-name`, while generating a separate conservative devicetree node identifier containing only lowercase letters, numbers, and underscores.

For example:

```dts
f_keys {
    display-name = "F-Keys";
    // human-readable layout...
    bindings = < ... >;
};
```

Identifiers are normalized deterministically (`F-Keys` -> `f_keys`, `6 Gaming` -> `layer_6_gaming`) and receive numeric suffixes when needed to avoid collisions. If `display-name` already exists, it is updated in place; if it is missing, it is added. The human-readable layout selector labels are also refreshed.

New layers are created with a matching label automatically:

```dts
navigation_layer {
    display-name = "navigation_layer";
    // human-readable layout...
    bindings = < ... >;
};
```

The layer's numeric index remains unchanged. Existing `&mo`, `&tog`, `&to`, combo layer restrictions, and conditional-layer references therefore continue to point to the same layer.

The layer-key help now displays on its own wrapped line:

> To move a layer selector, assign the replacement first, then select the old key and click Transparent.

## Common shortcut warnings

Warnings are advisory and do not block saving because an operating-system shortcut may be intentional.

- A modified key binding such as `&kp LC(C)` is identified as `Ctrl+C`.
- A combo output that sends a common shortcut is identified.
- A combo whose trigger positions overlap a normal shortcut is identified. For example, a combo using the physical `LCTRL` and `C` positions on the default layer may intercept `Ctrl+C`.
- Modifier and layer-selector positions are traced across layers. For example, when a physical GUI/Win + Lower pair activates another layer, functions that replace Lower's arrow positions are flagged as conflicts with Win/Super+Arrow window snapping.
- Conflicting keys are highlighted in amber and marked with `!` directly in the on-screen keymap.
- A warning count appears at the top of the Keymap tab, and selecting an affected key shows the full warning immediately under **Selected key**.
- Combo dialogs show applicable warnings before the combo is saved.
- Validation continues to list all detected shortcut warnings separately from structural errors.

The warning library covers common editing, window-management, application-switching, session, file-manager, and desktop shortcuts used by Windows and mainstream Linux desktops. It does not attempt to catalog application-specific shortcuts.

The remembered directory is stored in the standard per-user configuration location: `$XDG_CONFIG_HOME/corne-keymap-editor/settings.json`, or `~/.config/corne-keymap-editor/settings.json` when `XDG_CONFIG_HOME` is not set.

## Two-layer conditional activation

Open **Layer activation** and use **Two-layer conditional activation**. Choose two source layers and a different target layer. The editor writes the standard ZMK structure:

```dts
    conditional_layers {
        compatible = "zmk,conditional-layers";

        lower_raise_adjust {
            if-layers = <1 2>;
            then-layer = <3>;
        };
    };
```

The target is active if and only if both source layers are active. A target should normally have a higher number than either source. Do not use an ordinary layer key or physical-key pair to activate the same target layer, because ZMK gives the conditional-layer system control of that target's activation state.

## Two physical keys activating a layer

In **Layer activation**, use **Two physical keys pressed simultaneously** and select exactly two positions on the Corne diagram. Choose the target, behavior, timing window, and optional layer restrictions.

For momentary activation the editor generates a combo like this:

```dts
    combos {
        compatible = "zmk,combos";

        thumbs_adjust {
            timeout-ms = <50>;
            key-positions = <36 39>;
            bindings = <&mo 3>;
            slow-release;
        };
    };
```

`slow-release` keeps a momentary target active until both physical trigger keys have been released. Toggle and direct-switch pair activations do not add `slow-release`.

## Recovering a keymap generated by 1.2.0

Open the broken `.keymap` normally. The editor repairs recognized problems before parsing, marks the document as modified, and displays a line-by-line repair report. It preserves recoverable layers, bindings, readable layout comments, combos, conditional layers, and unrelated source text.

The recovery rules include:

- Human-readable layer references such as `4: navigation_layer` where ZMK requires a number.
- Bracketed or comma/plus-separated activation lists.
- Nested or incomplete layer-behavior cells such as `&mo <4>`.
- Display labels accidentally written as source, including the observed `&bt BTCLR` failure. This is corrected to `&bt BT_CLR`.
- The affected 1.2.0 file's two brace combos accidentally left on the generic Tab+Q default. `L_Brace` is recovered to V+B (`28 29`) and `R_Brace` to N+M (`30 31`).
- Unsafe visible layer names used directly as devicetree identifiers. The exact failed keymap declared `F-Keys {` at line 142. It is repaired to the safe node `f_keys {` while preserving `display-name = "F-Keys";`.
- The obsolete layer property `label = "...";` written by older versions. ZMK layer names use `display-name`; the old property is removed during recovery.
- Orphaned human-readable layout rows left by an older layer-rename operation. These appear as bare or truncated ASCII table fragments containing several `|` separators without the required `//` comment prefix and cause Zephyr to report `expected node name, property name, or '}'`. The editor removes only unmistakable orphaned table rows, preserving the layer bindings, remaining readable layout, combos, and conditional layers.

After recovery, use **Save** or **Save As** to write the corrected keymap. The command-line equivalent is:

```bash
python3 corne_keymap_editor.py broken.keymap --repair-output corne.keymap
```

Unrecognized activation syntax is rejected rather than silently guessed.

Current versions also validate source text before saving and reject any bare human-readable table row, preventing the older malformed rename output from being generated again.

The release includes `zephyr_dts_validate.py`, an independent full DTS grammar validator used in regression testing. It verifies the repaired keymap separately for `corne_left` and `corne_right`, checks normalized layer identifiers, and rejects obsolete `label` properties.

## Printable PDF cheat sheet

Click **Export PDF…** to create a single-sided landscape Letter cheat sheet modeled on a traditional Corne keymap reference. It includes:

- All layer diagrams arranged on one side of one sheet. With unusually large keymaps, the separate default diagram is omitted first to preserve legibility.
- The default keycap legend appears in small gray text on every non-default diagram. Transparent positions show the inherited default key prominently.
- Readable active-layer labels and subtle highlighting for layer keys.
- Activation summaries for conditional layers, layer keys, and physical-key activations.
- Small amber output boxes over adjacent two-key combo pairs.
- Layer-specific combo filtering. Combos restricted to other layers do not appear.

The sheet intentionally contains no application branding: the phrases “single-sided reference” and “generated by Corne Keymap Editor” are not printed.

Two-key combos whose physical positions are not adjacent are intentionally omitted from the diagram. They remain unchanged in the keymap itself.

The generic **Add combo** dialog starts with no physical positions selected. This prevents an unfinished combo from silently becoming a Tab+Q combo.

Command-line PDF export is also available:

```bash
python3 corne_keymap_editor.py corne.keymap --export-pdf corne-cheat-sheet.pdf
```

## Editing combos

Open the **Combos** tab and choose **Add combo**. Select at least two numbered key positions, enter the output binding, and set any optional restrictions. Existing standard combo nodes are loaded into the table.

The general combo editor and the **Two physical keys pressed simultaneously** editor use a multi-select layer list. Every row shows the numeric index and visible name, such as `0: Base` or `5: Game`. Select any number of layers; selecting none means that the combo is global. Existing multi-layer restrictions are restored when a combo is reopened and serialize in standard ZMK form:

```dts
layers = <0 5>;
```

For a restricted `&tog N` combo, the target layer must be included or the combo disappears after it turns that layer on and cannot turn it back off. When a toggle behavior is selected, the editor automatically includes the source selection and target layer. If the user later deselects the target, an in-place warning explains the problem.

The Game regression case is emitted as:

```dts
Game {
    timeout-ms = <50>;
    key-positions = <40 41>;
    layers = <0 5>;
    bindings = <&tog 5>;
};
```

Corne position numbering used by the editor:

```text
 0  1  2  3  4  5       6  7  8  9 10 11
12 13 14 15 16 17      18 19 20 21 22 23
24 25 26 27 28 29      30 31 32 33 34 35
         36 37 38      39 40 41
```

The editor writes combos in this form:

```dts
    combos {
        compatible = "zmk,combos";

        escape_combo {
            timeout-ms = <50>;
            key-positions = <0 1>;
            bindings = <&kp ESC>;
        };
    };
```

## Validation and command-line check

Use the GUI's **Validate** button, or run a headless parser/render check:

```bash
python3 corne_keymap_editor.py --check /path/to/corne.keymap
```

Run the included automated tests with:

```bash
python3 -m unittest -v test_editor.py
```

## Scope and safeguards

- The editor expects the standard 42-position Corne order: 12 + 12 + 12 + 6.
- A layer with any other binding count is rejected rather than silently rewritten.
- The combo editor understands the common ZMK properties it exposes. If a file uses custom, nonstandard properties inside combo child nodes, keep a backup and inspect the generated diff before replacing your production keymap.
- This tool edits source; it does not compile or flash ZMK firmware.
