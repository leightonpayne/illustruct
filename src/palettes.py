"""Nature-branded scientific illustration swatches.

Source: Nature's scientific illustration author guide, page 1, downloaded
2026-10-15. https://www.nature.com/documents/Nature_scientific_illustration_author_guide.pdf

The public guide supplies an ICC-tagged swatch image, not numeric color codes.
These sRGB values are median samples from the interiors of its published
swatches after color-managed rendering. They are not official hexadecimal
specifications. The duplicate Red, Blue and Yellow rows in the extended chart
are represented once, under Main accents. No interpolated shades are added.
"""

NATURE_PALETTE_GROUPS = (
    ("Backgrounds", (
        ("Stone", ("#f9f5ef", "#e5dfcd", "#c7c2a5", "#aaa486", "#898365", "#635f4a")),
        ("Grey", ("#e7e5ec", "#c9cdd9", "#99a2b3", "#717c91", "#4b566d", "#273246")),
    )),
    ("Main accents", (
        ("Red", ("#f9cfcd", "#eea3a7", "#df6765", "#cb3d3d", "#9d2825", "#751b17")),
        ("Blue", ("#cbe6f7", "#9fcae9", "#5b99cf", "#1072b2", "#0e4f90", "#1a2f5a")),
        ("Yellow", ("#fff2c3", "#f7db88", "#ecc74e", "#cda02f", "#9d782c", "#6c5622")),
    )),
    ("Extended", (
        ("Olive", ("#f4f2b2", "#e1dd66", "#c8c62d", "#9ba539", "#677833", "#384621")),
        ("Green", ("#dbe7c6", "#a2ca78", "#62b246", "#469543", "#257239", "#1d3b1f")),
        ("Teal", ("#cee6ed", "#98d2d3", "#4cbcbc", "#109aa4", "#126879", "#0b394e")),
        ("Purple", ("#ebd6e6", "#d4aacc", "#bb7cb1", "#aa4e93", "#7a2d74", "#491b50")),
        ("Orange", ("#fddfbc", "#fbc07f", "#f49a45", "#eb6e23", "#b75026", "#84341b")),
        ("Skin tones", ("#f9e5d6", "#debda2", "#c1997c", "#956b57", "#775040", "#462e20")),
    )),
)
