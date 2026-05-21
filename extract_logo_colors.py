"""
extract_logo_colors.py
----------------------
Given a CSV of teams, finds any row where primaryColor is the default (#888888)
and extracts primary + secondary colors from the team's local logo PNG.

Usage:
    python extract_logo_colors.py <input.csv> [output.csv]

    input.csv   must have columns: id, primaryColor, secondaryColor
    output.csv  defaults to <input>_colors_updated.csv

Logo folder is hardcoded below — change LOGOS_DIR if needed.
"""

import csv
import sys
from io import BytesIO
from pathlib import Path

try:
    from colorthief import ColorThief
    from PIL import Image
    HAS_DEPS = True
except ImportError:
    HAS_DEPS = False

LOGOS_DIR    = Path(r"C:\Users\Alex\Documents\Campus Hoops\logos\mn")
DEFAULT_PRIMARY   = "#888888"
DEFAULT_SECONDARY = "#FFFFFF"


# ── Color extraction ──────────────────────────────────────────────────────────

def _rgb_hex(r: int, g: int, b: int) -> str:
    return f"#{r:02X}{g:02X}{b:02X}"


def _useful(r: int, g: int, b: int) -> bool:
    brightness  = (r + g + b) / 3
    saturation  = max(r, g, b) - min(r, g, b)
    return 30 < brightness < 220 and saturation > 15


def extract_colors(png_path: Path) -> tuple[str, str]:
    """Return (primary, secondary) hex colors from a PNG logo."""
    if not HAS_DEPS:
        raise RuntimeError("Install Pillow and colorthief:  pip install Pillow colorthief")

    img = Image.open(png_path)
    # Flatten transparency onto white before color analysis
    bg = Image.new("RGB", img.size, (255, 255, 255))
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1])
    else:
        bg.paste(img.convert("RGB"))

    buf = BytesIO()
    bg.save(buf, format="PNG")
    buf.seek(0)

    ct      = ColorThief(buf)
    palette = ct.get_palette(color_count=8, quality=1)
    useful  = [c for c in palette if _useful(*c)]

    primary   = useful[0] if useful else palette[0]
    secondary = useful[1] if len(useful) > 1 else (255, 255, 255)
    return _rgb_hex(*primary), _rgb_hex(*secondary)


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    if len(sys.argv) < 2:
        print("Usage: python extract_logo_colors.py <input.csv> [output.csv]")
        sys.exit(1)

    input_path  = Path(sys.argv[1])
    output_path = Path(sys.argv[2]) if len(sys.argv) > 2 else \
                  input_path.with_stem(input_path.stem + "_colors_updated")

    if not input_path.exists():
        print(f"File not found: {input_path}")
        sys.exit(1)

    rows = list(csv.DictReader(input_path.open(encoding="utf-8")))
    if not rows:
        print("CSV is empty.")
        sys.exit(1)

    required = {"id", "primaryColor", "secondaryColor"}
    if not required.issubset(rows[0].keys()):
        missing = required - set(rows[0].keys())
        print(f"CSV is missing columns: {missing}")
        sys.exit(1)

    updated = skipped = not_found = 0

    for row in rows:
        team_id = row["id"].strip()
        primary = row["primaryColor"].strip().upper()

        if primary != DEFAULT_PRIMARY.upper():
            continue  # already has real colors

        logo_path = LOGOS_DIR / f"{team_id}.png"
        if not logo_path.exists():
            print(f"  [no logo]  {team_id}")
            not_found += 1
            continue

        try:
            p, s = extract_colors(logo_path)
            row["primaryColor"]   = p
            row["secondaryColor"] = s
            print(f"  [updated]  {team_id:<45}  {p}  {s}")
            updated += 1
        except Exception as e:
            print(f"  [error]    {team_id}: {e}")
            skipped += 1

    # Write output
    fieldnames = list(rows[0].keys())
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{updated} updated  |  {not_found} no logo  |  {skipped} errors")
    print(f"Saved → {output_path}")


if __name__ == "__main__":
    main()
