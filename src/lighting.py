"""Solid lighting colours calibrated to adjacent Nature palette swatches.

Each swatch supplies a one-step change in Oklab lightness, log chroma and
hue. Inverse-distance interpolation transfers those changes to arbitrary
component colours without snapping them to a palette family. The published
row ends have no outward neighbour; extend their nearest step instead.

Oklab conversion matrices: Björn Ottosson's public-domain reference,
https://bottosson.github.io/posts/oklab/ (2021-01-25 matrices).
Only the Python standard library is required.
"""

from functools import lru_cache
import math
import re

from .palettes import NATURE_PALETTE_GROUPS


def _oklab(color):
    rgb = [int(color[index:index+2], 16)/255 for index in (1, 3, 5)]
    r, g, b = [v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in rgb]
    l = (.4122214708*r + .5363325363*g + .0514459929*b)**(1/3)
    m = (.2119034982*r + .6806995451*g + .1073969566*b)**(1/3)
    s = (.0883024619*r + .2817188376*g + .6299787005*b)**(1/3)
    return (.2104542553*l + .7936177850*m - .0040720468*s,
            1.9779984951*l - 2.4285922050*m + .4505937099*s,
            .0259040371*l + .7827717662*m - .8086757660*s)


def _linear_rgb(lab):
    lightness, a, b = lab
    l = (lightness + .3963377774*a + .2158037573*b)**3
    m = (lightness - .1055613458*a - .0638541728*b)**3
    s = (lightness - .0894841775*a - 1.2914855480*b)**3
    return (4.0767416621*l - 3.3077115913*m + .2309699292*s,
            -1.2684380046*l + 2.6097574011*m - .3413193965*s,
            -.0041960863*l - .7034186147*m + 1.7076147010*s)


def _display_color(lab):
    """Fit sRGB by reducing chroma, keeping the intended lightness and hue."""
    lightness, a, b = lab
    lightness = min(1., max(0., lightness))
    rgb = _linear_rgb((lightness, a, b))
    if not all(-1e-7 <= v <= 1+1e-7 for v in rgb):
        low, high = 0., 1.
        for _ in range(24):
            scale = (low+high)/2
            candidate = _linear_rgb((lightness, a*scale, b*scale))
            if all(0 <= v <= 1 for v in candidate):
                low = scale
            else:
                high = scale
        rgb = _linear_rgb((lightness, a*low, b*low))
    srgb = [12.92*v if v <= .0031308 else 1.055*v**(1/2.4)-.055 for v in rgb]
    return '#' + ''.join(f'{round(255*min(1., max(0., v))):02x}' for v in srgb)


def _step(source, target):
    source_chroma, target_chroma = math.hypot(*source[1:]), math.hypot(*target[1:])
    hue = math.atan2(target[2], target[1])-math.atan2(source[2], source[1])
    return (target[0]-source[0], math.log(target_chroma/source_chroma),
            math.atan2(math.sin(hue), math.cos(hue)))


def _calibration():
    anchors, exact = [], {}
    for _, families in NATURE_PALETTE_GROUPS:
        for _, colors in families:
            labs = [_oklab(color) for color in colors]
            for index, (color, lab) in enumerate(zip(colors, labs)):
                lighter = (_step(lab, labs[index-1]) if index else
                           tuple(-v for v in _step(lab, labs[1])))
                darker = (_step(lab, labs[index+1]) if index+1 < len(labs) else
                          tuple(-v for v in _step(lab, labs[index-1])))
                anchors.append((lab, lighter, darker))
                exact[color] = (colors[index-1] if index else None,
                                colors[index+1] if index+1 < len(colors) else None)
    return tuple(anchors), exact


_ANCHORS, _EXACT = _calibration()


def lighting_colors(color):
    """Return (highlight, shadow) hex colours for a valid #RGB or #RRGGBB.

    Nature colours use the exact adjacent swatches wherever they exist.
    Custom, source and preset colours all use the same calibrated transform.
    Neutral inputs stay neutral; black/white saturate at the display limits.
    """
    if not isinstance(color, str) or not re.fullmatch(r'#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?', color):
        raise ValueError('Lighting colours require a #RGB or #RRGGBB colour.')
    color = color.lower()
    if len(color) == 4:
        color = '#' + ''.join(v*2 for v in color[1:])
    return _lighting_colors(color)


@lru_cache(maxsize=512)
def _lighting_colors(color):
    lab = _oklab(color)
    distances = [sum((v-a)**2 for v, a in zip(lab, anchor[0])) for anchor in _ANCHORS]
    nearest = min(distances)
    # d^-4 gives nearby palette steps precedence without discontinuous
    # nearest-family boundaries. Normalization avoids huge inverse weights.
    weights = ([float(d == 0) for d in distances] if nearest == 0 else
               [(nearest/d)**2 for d in distances])
    total = sum(weights)
    chroma = math.hypot(*lab[1:])
    hue = math.atan2(lab[2], lab[1])
    result = []
    for direction, exact in enumerate(_EXACT.get(color, (None, None)), 1):
        if exact is not None:
            result.append(exact)
            continue
        delta_l, log_scale, rotation = (
            sum(weight*anchor[direction][axis] for weight, anchor in zip(weights, _ANCHORS))/total
            for axis in range(3))
        # The reference matrices leave tiny numerical chroma in greys.
        scaled = chroma*math.exp(log_scale) if chroma > 1e-7 else 0.
        result.append(_display_color((lab[0]+delta_l, scaled*math.cos(hue+rotation),
                                      scaled*math.sin(hue+rotation))))
    return tuple(result)
