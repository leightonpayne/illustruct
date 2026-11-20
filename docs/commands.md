---
icon: lucide/terminal
---

# Command reference

Run these commands in ChimeraX:

```text
illustruct
illustruct export <filename> [arguments]
```

`illustruct` opens the panel. `illustruct export` writes an illustration to the
required filename; its `.svg`, `.pdf`, or `.png` extension chooses the format.
Arguments are optional keyword/value pairs, such as `simplification 25`, and may
be given in any order. Quote paths containing spaces:
`illustruct export "~/Desktop/my figure.svg"`.

Exports inherit the open panel’s settings, source filter, and component
visibility. Arguments change that export only; they do not change the panel or
ChimeraX selection. Without a panel, the defaults below apply. Every command
export captures the current scene, colors, and camera, even when **Follow view**
is paused.

## Examples

Adjust the shape, export a particular source, match the camera, or set a print
width:

```text
illustruct export ~/Desktop/smooth.svg simplification 25 smoothing 45 minimumPieceSize 5
illustruct export ~/Desktop/chain.svg atoms #1/A framing fit
illustruct export ~/Desktop/selection.svg atoms sel
illustruct export ~/Desktop/nucleic.svg components nucleic
illustruct export ~/Desktop/complex.svg components protein,ligand opacity 75
illustruct export ~/Desktop/view.png framing camera width 2400
illustruct export ~/Desktop/figure.pdf widthMm 89 strokeWidthPt 0.4
```

## Appearance

Switches accept `true` or `false`. Percentages use the same scale as the panel;
omit the `%` sign.

| Argument                      | Values or effect                                                        | Default    |
| ----------------------------- | ----------------------------------------------------------------------- | ---------- |
| `simplification`              | `0–100`; broader shapes at higher values                                | `50`       |
| `smoothing`                   | `0–100`; smooth contours and backbones                                  | `32.5`     |
| `minimumPieceSize`            | `0–100`; percentage of the largest visible piece’s area per component   | `0`        |
| `opacity`                     | `0–100`; overall opacity                                                | `100`      |
| `nucleicStyle`                | `backbone`, `silhouette`                                                | `backbone` |
| `shading`                     | Solid highlights and shadows                                            | `true`     |
| `highlightSize`, `shadowSize` | `0–200`; effect size as a percentage                                    | `100` each |
| `outline`                     | Draw outlines                                                           | `true`     |
| `dashedBoundaries`            | Dash differently colored regions’ shared borders within a protein chain | `false`    |
| `tintedOutline`               | Derive darker outlines from component colors                            | `true`     |
| `transparent`                 | Transparent background                                                  | `true`     |

`minimumPieceSize 0` keeps every piece; `100` keeps the largest pieces,
including ties. The comparison uses projected visible area within each
component, so small whole components survive. Backbones are unaffected.

`shading false` hides both lighting effects without resetting their sizes; a
size of `0` hides one effect. Backbones stay flat. Dashed boundaries require
`outline true`; exterior contours and borders between chains remain solid.

Set colors in ChimeraX before exporting. The illustration uses those source
colors for its component fills; lighting and tinted outlines derive their colors
from them.

## Source and components

| Argument         | Values or effect                                                                                                                        |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `atoms`          | Native ChimeraX atom specification: `#1` for a model, `#1/A` for a chain, `#1/A:10-40` for residues, or `sel` for the current selection |
| `components`     | Comma-separated list of `protein`, `nucleic`, `ligand`, `ion`                                                                           |
| `splitSelection` | Split selected residues from the rest of each protein or DNA/RNA chain; default `false`                                                 |

`atoms` uses the specified source atoms, including hidden atoms and models,
without changing the current selection. If a model has multiple instances, every
instance is included, even hidden ones. It replaces the panel’s source/model
filter and component visibility masks. Water and hydrogen are always excluded;
an atom specification with no eligible atoms fails without writing a file.

`components` selects which types to include and overrides all inherited
component visibility, including individually hidden entries. For example,
`components protein,ligand` includes proteins and ligands; `components nucleic`
includes DNA/RNA only. Combined with `atoms`, the list filters that explicit
atom set.

Without either argument, exports preserve the panel’s source and component
choices. Without a panel, the source is visible atoms and residues with visible
cartoons, and only proteins are shown. An explicit `components` list keeps the
inherited source filter; an explicit `atoms` specification includes every
eligible component type unless `components` further restricts it. Use
`components protein,nucleic,ligand,ion` to include all four types from the
current source.

`splitSelection` follows the current ChimeraX selection, including when `atoms`
specifies a different source. Specifying `atoms` does not select those atoms.

Protein residues automatically group by source color within each chain,
independently of `splitSelection`. DNA/RNA uses one representative color per
component. Single-atom nonpolymer residues are ions; larger ones are ligands.
See the [figure workflow](usage.md) for source colors and selection controls.

## Canvas and files

| Argument              | Values                                                      | Default                                 |
| --------------------- | ----------------------------------------------------------- | --------------------------------------- |
| `width`, `height`     | `64–16384` pixels; at most 64 million pixels total          | Derived from print size                 |
| `widthMm`, `heightMm` | `5–1000` mm                                                 | `89 × 66.75`                            |
| `dpi`                 | Integer `72–1200`                                           | `450`                                   |
| `strokeWidth`         | `0–64` pixels                                               | `2.5` at the default size and DPI       |
| `strokeWidthPt`       | `0–10` points                                               | `0.4`                                   |
| `margin`              | `0–25`; percentage of the shorter canvas dimension per side | `6`                                     |
| `framing`             | `fit`, `camera`                                             | Panel framing, or `fit` without a panel |

`width` or `height` selects pixel sizing; `widthMm` or `heightMm` selects
millimeter sizing. Mixing the two is an error. With no dimensions, the current
sizing mode is preserved; `dpi` alone does not switch it. Supplying one
dimension infers the other from the current canvas aspect ratio, using the exact
millimeter ratio when the current sizing mode is Print. For example,
`width 2400` gives `2400 × 1800` with the default canvas. Print dimensions and
DPI determine pixel dimensions, subject to the same pixel limits.

Use either `strokeWidth` or `strokeWidthPt`, never both. Either works with pixel
or millimeter dimensions; widths convert using the final export DPI (1 point =
1/72 inch). The converted width must also fit the active mode’s limit: 64 pixels
or 10 points. Omitted size and outline settings inherit the panel, or use the
defaults above.

Switching between pixel and millimeter sizing preserves the current outline
width in pixels unless you specify a width. Changing DPI while keeping
millimeter sizing preserves the outline width in points.

`framing fit` uses an orthographic projection of the view direction and fits
visible artwork to the canvas with the requested margin. `framing camera`
matches ChimeraX’s camera and viewport without extra margin. Either explicit
choice overrides an inherited **Lock frame**. Omitting `framing` preserves the
panel’s framing choice.

Use `.svg`, `.pdf`, or `.png`. SVG and PDF preserve editable vectors; PDF
preserves physical page size. PNG uses the requested pixel dimensions. SVG works
headlessly; PNG and PDF require a Qt application.

Command exports replace existing files. The panel’s export dialog asks before
replacement.
