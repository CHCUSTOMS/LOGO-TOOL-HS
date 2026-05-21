"""
compare_logos.py
----------------
Compares regular / light / dark logo variants to decide which to use on
a dark background.

Dark mode is treated as the default — it almost always has the best
contrast against black. The question is whether light or regular is worth
using instead because it preserves more brand colour, provided it still
has enough contrast to be readable.

Primary metric: WCAG contrast ratio against black (linearised sRGB).
Secondary metric: average chroma of non-white pixels (colour richness).

Recommendation logic:

  logos don't differ (diff < 1%)
      → "light fine"   (identical to dark, no reason not to use light)

  light_contrast >= contrast_floor
  AND light_color_gain >= min_color_gain
      → "use light"    (light is readable AND has meaningfully more colour)

  reg_contrast >= contrast_floor
  AND reg_color_gain >= min_color_gain
      → "use regular"  (light doesn't qualify but regular does)

  otherwise
      → "use dark mode"

colour_gain = chroma(light or reg) - chroma(dark), non-white pixels only.
Negative gain (dark has more colour) always loses to dark.

Output: logo_comparison.csv  (written next to this script)

Usage:
    python compare_logos.py [--contrast-floor 3.5] [--min-color-gain 0.15]
"""

import argparse
import csv
import os
import shutil
from pathlib import Path

import numpy as np
from PIL import Image

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE      = Path(r"C:\Users\Alex\PycharmProjects\marchmadness\marchmadness\web\static\images\protected\team_logos")
DIR_REG   = BASE
DIR_DARK  = BASE / "dark"
DIR_LIGHT = BASE / "light"

OUT_CSV      = Path(__file__).with_name("logo_comparison.csv")
DEFAULT_OUT_DIR = Path(r"C:\Users\Alex\Documents\Campus Hoops\logos\ncaam_dark")

DEFAULT_HIGH_FLOOR     = 4.0   # readable logo — modest colour gain is enough
DEFAULT_LOW_COLOR_GAIN = 0.15
DEFAULT_LOW_FLOOR      = 1.8   # dim logo — needs a large colour gain to justify it
DEFAULT_HIGH_COLOR_GAIN= 0.40
DEFAULT_MIN_RATIO      = 1.5   # condition A alternative: light_chr / max(dark_chr, floor)
DARK_CHROMA_FLOOR      = 0.05  # denominator floor so ratio doesn't explode near zero

# ── Manual overrides ──────────────────────────────────────────────────────────
# Force a specific variant for any team, bypassing the algorithm entirely.
# Keys are filenames (e.g. "331.png"), values are "use dark", "use light", or "use regular".
LOGO_OVERRIDES: dict[str, str] = {
    "331.png": "use dark",   # Eastern Washington — black parts invisible on dark bg
    "2649.png": "use dark",  # Toledo (rocket is a bit hidden in light logo)
    "2084.png": "use regular",  # Buffalo
    "179.png": "use dark",  # St. Bonaventure
    "254.png": "use regular",  # Utah
    "23.png": "use dark",  # San Jose St.
    "2539.png": "use light",  # San Francisco
}


# ── Image helpers ─────────────────────────────────────────────────────────────

def load_rgba(path: Path) -> np.ndarray:
    img = Image.open(path).convert("RGBA")
    return np.asarray(img, dtype=np.float32) / 255.0


def _mask(rgba: np.ndarray) -> np.ndarray:
    return rgba[:, :, 3] > 0.05


def _linearize(c: np.ndarray) -> np.ndarray:
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def avg_luminance(rgba: np.ndarray) -> float:
    """Raw Rec. 709 luminance — kept for reference only."""
    m = _mask(rgba)
    if not m.any():
        return float("nan")
    rgb = rgba[:, :, :3][m]
    return float((0.2126 * rgb[:, 0] + 0.7152 * rgb[:, 1] + 0.0722 * rgb[:, 2]).mean())


def avg_wcag_contrast_vs_black(rgba: np.ndarray) -> float:
    """Mean WCAG contrast ratio of non-transparent pixels against black."""
    m = _mask(rgba)
    if not m.any():
        return float("nan")
    rgb = _linearize(rgba[:, :, :3][m])
    L = 0.2126 * rgb[:, 0] + 0.7152 * rgb[:, 1] + 0.0722 * rgb[:, 2]
    return float(((L + 0.05) / 0.05).mean())


def avg_chroma(rgba: np.ndarray, white_thresh: float = 0.75) -> float:
    """Mean chroma of non-transparent pixels, with near-white pixels zeroed out.

    White pixels contribute 0 to the numerator but stay in the denominator,
    so a logo that is half white scores lower than one that is fully colourful —
    even if the non-white portions are equally saturated.

    This prevents a yellow+white logo from outscoring a yellow+green logo just
    because the white was excluded and only the high-chroma yellow was averaged.
    """
    m = _mask(rgba)
    if not m.any():
        return float("nan")
    rgb = rgba[:, :, :3][m]
    chroma = rgb.max(axis=1) - rgb.min(axis=1)
    chroma[rgb.min(axis=1) > white_thresh] = 0.0   # zero out near-white, keep in denominator
    return float(chroma.mean())


def pixel_diff(a: np.ndarray, b: np.ndarray) -> float:
    """Mean absolute luminance diff over pixels non-transparent in either image."""
    if a.shape != b.shape:
        img_b = Image.fromarray((b * 255).astype(np.uint8), "RGBA")
        img_b = img_b.resize((a.shape[1], a.shape[0]), Image.LANCZOS)
        b = np.asarray(img_b, dtype=np.float32) / 255.0
    mask = (a[:, :, 3] > 0.05) | (b[:, :, 3] > 0.05)
    if not mask.any():
        return float("nan")
    lum_a = 0.2126 * a[:, :, 0] + 0.7152 * a[:, :, 1] + 0.0722 * a[:, :, 2]
    lum_b = 0.2126 * b[:, :, 0] + 0.7152 * b[:, :, 1] + 0.0722 * b[:, :, 2]
    return float(np.abs(lum_a[mask] - lum_b[mask]).mean())


def _fmt(v: float) -> str:
    return f"{v:.4f}" if v == v else ""


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--high-floor",      type=float, default=DEFAULT_HIGH_FLOOR,
                        help="Contrast floor for condition A (readable logo, modest colour gain enough)")
    parser.add_argument("--low-color-gain",  type=float, default=DEFAULT_LOW_COLOR_GAIN,
                        help="Min colour gain required when contrast >= high-floor")
    parser.add_argument("--low-floor",       type=float, default=DEFAULT_LOW_FLOOR,
                        help="Contrast floor for condition B (dim logo, needs large colour gain)")
    parser.add_argument("--high-color-gain", type=float, default=DEFAULT_HIGH_COLOR_GAIN,
                        help="Min colour gain required when contrast >= low-floor")
    parser.add_argument("--min-ratio",       type=float, default=DEFAULT_MIN_RATIO,
                        help="Condition A alternative: light_chr/max(dark_chr,0.05) >= this (handles white dark logos)")
    parser.add_argument("--output-dir",      type=Path,  default=DEFAULT_OUT_DIR,
                        help="Folder to copy best logos into (created/overwritten on each run)")
    parser.add_argument("--no-copy",         action="store_true",
                        help="Skip copying logos — analysis only")
    args = parser.parse_args()
    high_floor      = args.high_floor
    low_color_gain  = args.low_color_gain
    low_floor       = args.low_floor
    high_color_gain = args.high_color_gain
    min_ratio       = args.min_ratio
    output_dir      = args.output_dir
    do_copy         = not args.no_copy

    reg_names   = {f for f in os.listdir(DIR_REG)   if os.path.isfile(DIR_REG / f)   and f.lower().endswith(".png")}
    dark_names  = {f for f in os.listdir(DIR_DARK)  if os.path.isfile(DIR_DARK / f)  and f.lower().endswith(".png")}
    light_names = {f for f in os.listdir(DIR_LIGHT) if os.path.isfile(DIR_LIGHT / f) and f.lower().endswith(".png")}

    all_names = sorted(
        reg_names | dark_names | light_names,
        key=lambda s: int(s.split(".")[0]) if s.split(".")[0].lstrip("-").isdigit() else s,
    )

    if do_copy:
        output_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for i, fname in enumerate(all_names, 1):
        has_reg   = fname in reg_names
        has_dark  = fname in dark_names
        has_light = fname in light_names

        reg_lum = dark_lum = light_lum                = float("nan")
        reg_contrast = dark_contrast = light_contrast = float("nan")
        reg_chr = dark_chr = light_chr                = float("nan")
        reg_vs_light = reg_vs_dark = light_vs_dark    = float("nan")

        reg_arr = dark_arr = light_arr = None

        if has_reg:
            reg_arr      = load_rgba(DIR_REG   / fname)
            reg_lum      = avg_luminance(reg_arr)
            reg_contrast = avg_wcag_contrast_vs_black(reg_arr)
            reg_chr      = avg_chroma(reg_arr)
        if has_dark:
            dark_arr      = load_rgba(DIR_DARK  / fname)
            dark_lum      = avg_luminance(dark_arr)
            dark_contrast = avg_wcag_contrast_vs_black(dark_arr)
            dark_chr      = avg_chroma(dark_arr)
        if has_light:
            light_arr      = load_rgba(DIR_LIGHT / fname)
            light_lum      = avg_luminance(light_arr)
            light_contrast = avg_wcag_contrast_vs_black(light_arr)
            light_chr      = avg_chroma(light_arr)

        if reg_arr   is not None and light_arr is not None:
            reg_vs_light  = pixel_diff(reg_arr, light_arr)
        if reg_arr   is not None and dark_arr  is not None:
            reg_vs_dark   = pixel_diff(reg_arr, dark_arr)
        if light_arr is not None and dark_arr  is not None:
            light_vs_dark = pixel_diff(light_arr, dark_arr)

        # Colour gain = how much more chroma light/reg has vs dark (positive = more colourful)
        light_color_gain = (
            light_chr - dark_chr
            if (light_chr == light_chr and dark_chr == dark_chr) else float("nan")
        )
        reg_color_gain = (
            reg_chr - dark_chr
            if (reg_chr == reg_chr and dark_chr == dark_chr) else float("nan")
        )
        # Colour ratio = light/reg chroma relative to dark (floors dark at DARK_CHROMA_FLOOR)
        light_color_ratio = (
            light_chr / max(dark_chr, DARK_CHROMA_FLOOR)
            if (light_chr == light_chr and dark_chr == dark_chr) else float("nan")
        )
        reg_color_ratio = (
            reg_chr / max(dark_chr, DARK_CHROMA_FLOOR)
            if (reg_chr == reg_chr and dark_chr == dark_chr) else float("nan")
        )

        # ── Recommendation (dark is default) ──────────────────────────────────
        logos_differ = (light_vs_dark == light_vs_dark) and light_vs_dark >= 0.01

        def _viable(contrast, color_gain, chroma, d_chr):
            if contrast != contrast or color_gain != color_gain:
                return False
            ratio = chroma / max(d_chr, DARK_CHROMA_FLOOR) if chroma == chroma else 0.0
            cond_a = contrast >= high_floor and (color_gain >= low_color_gain or ratio >= min_ratio)
            cond_b = contrast >= low_floor  and color_gain >= high_color_gain
            return cond_a or cond_b

        light_viable = _viable(light_contrast, light_color_gain, light_chr, dark_chr)
        reg_viable   = _viable(reg_contrast,   reg_color_gain,   reg_chr,   dark_chr)

        if fname in LOGO_OVERRIDES:
            rec = LOGO_OVERRIDES[fname]
            best_src = {
                "use dark":    DIR_DARK  / fname if has_dark  else None,
                "use light":   DIR_LIGHT / fname if has_light else None,
                "use regular": DIR_REG   / fname if has_reg   else None,
            }.get(rec)
        elif not has_dark:
            # No dark variant — fall back to light then regular
            rec = "no dark variant"
            best_src = (DIR_LIGHT / fname if has_light else
                        DIR_REG   / fname if has_reg   else None)
        elif not has_light:
            rec = "use dark"
            best_src = DIR_DARK / fname
        elif not logos_differ:
            rec = "use dark"         # identical — dark is the intentional dark asset
            best_src = DIR_DARK / fname
        elif light_viable:
            rec = "use light"        # readable + meaningful colour gain over dark
            best_src = DIR_LIGHT / fname
        elif reg_viable:
            rec = "use regular"      # light fails; regular independently qualifies
            best_src = DIR_REG / fname
        else:
            rec = "use dark"
            best_src = DIR_DARK / fname

        # ── Copy best logo to output dir ───────────────────────────────────────
        if do_copy and best_src is not None and best_src.exists():
            shutil.copy2(best_src, output_dir / fname)

        reg_eq_light = (
            "YES" if (reg_vs_light == reg_vs_light and reg_vs_light < 0.01)
            else ("NO" if reg_vs_light == reg_vs_light else "n/a")
        )

        rows.append({
            "file":               fname,
            "has_regular":        has_reg,
            "has_light":          has_light,
            "has_dark":           has_dark,
            "reg_luminance":      _fmt(reg_lum),
            "light_luminance":    _fmt(light_lum),
            "dark_luminance":     _fmt(dark_lum),
            "reg_wcag_contrast":  _fmt(reg_contrast),
            "light_wcag_contrast":_fmt(light_contrast),
            "dark_wcag_contrast": _fmt(dark_contrast),
            "reg_color_nw":       _fmt(reg_chr),
            "light_color_nw":     _fmt(light_chr),
            "dark_color_nw":      _fmt(dark_chr),
            "light_color_gain":   _fmt(light_color_gain),
            "reg_color_gain":     _fmt(reg_color_gain),
            "light_color_ratio":  _fmt(light_color_ratio),
            "reg_color_ratio":    _fmt(reg_color_ratio),
            "reg_vs_light_diff":  _fmt(reg_vs_light),
            "reg_vs_dark_diff":   _fmt(reg_vs_dark),
            "light_vs_dark_diff": _fmt(light_vs_dark),
            "reg_equals_light":   reg_eq_light,
            "recommendation":     rec + " [override]" if fname in LOGO_OVERRIDES else rec,
        })

        if i % 50 == 0 or i == len(all_names):
            print(f"  {i}/{len(all_names)} processed...")

    fieldnames = list(rows[0].keys())
    with OUT_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    recs = [r["recommendation"] for r in rows]
    print(f"\nDone -- {len(rows)} logos analysed")
    print(f"  high_floor={high_floor}  low_color_gain={low_color_gain}  low_floor={low_floor}  high_color_gain={high_color_gain}  min_ratio={min_ratio}")
    print(f"  use dark      : {recs.count('use dark')}")
    print(f"  use light     : {recs.count('use light')}  (readable + more colourful than dark)")
    print(f"  use regular   : {recs.count('use regular')}  (light fails; regular qualifies)")
    print(f"  no dark variant: {recs.count('no dark variant')}")
    print(f"  reg = light   : {sum(1 for r in rows if r['reg_equals_light'] == 'YES')}")
    if do_copy:
        print(f"\nCopied best logos -> {output_dir}")
    print(f"\nSaved -> {OUT_CSV}")


if __name__ == "__main__":
    main()
