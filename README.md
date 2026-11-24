# WTX nameplate generator (A2 rating plate, 65 x 35 mm)

Generates the IoTech **A2 rating plate** (Page 2 of the "Product Name Plates 70x35mm" design) as laser-ready SVG files, in two polarities:

| File | Content |
|---|---|
| `<name>_positive.svg` | Engraving marks in black, no background |
| `<name>_negative.svg` | Solid black plate, marks knocked out in white |

## Setup

```bash
./setup.sh            # creates .venv and installs requirements.txt
```

## Usage

```bash
# From a JSON file
.venv/bin/python make_nameplate.py --config example.json

# From CLI flags only
.venv/bin/python make_nameplate.py \
  --product-name "WTX-200" --product-type "Wireless Transmitter" \
  --model WTX-200-ZB --serial WTX2611000123 \
  --supply "3.6 V DC" --protection IP66 --temp "-40 … +85 °C" --mfg-date 11/2026 \
  --qr-data "https://register.iotechglobal.io/WTX2611000123" \
  --barcode-data WTX2611000123

# JSON plus overrides (CLI wins over JSON)
.venv/bin/python make_nameplate.py --config example.json --serial WTX2611000124 --barcode-data WTX2611000124 --no-ex
```

Output goes to this folder by default (`--out-dir` to change). File base name defaults to `nameplate-A2_<serial>` (`--output-name` to change).

`example.json` holds sample values only.

## Parameters

| CLI flag | JSON key | Default |
|---|---|---|
| `--product-name` | `product_name` | `[Product name]` |
| `--product-type` | `product_type` | `[Product type]` (printed uppercase) |
| `--model` | `model` | `[MODEL NO.]` |
| `--serial` | `serial` | `[SERIAL NO.]` |
| `--supply` | `supply` | `[V DC]` |
| `--protection` | `protection` | `[IP RATING]` |
| `--temp` | `temp` | `[MIN … MAX °C]` |
| `--mfg-date` | `mfg_date` | `[MM/YYYY]` |
| `--qr-data` | `qr_data` | none: dashed placeholder box |
| `--qr-error` | `qr_error` | `m` (l, m, q, h) |
| `--barcode-data` | `barcode_data` | none: design's dummy bars |
| `--barcode-text` | `barcode_text` | same as barcode data |
| `--footer-1` | `footer_1` | `IoTech LLC` |
| `--footer-2` | `footer_2` | `Dokki, Giza, Egypt` |
| `--footer-3` | `footer_3` | `iotechglobal.io` |
| `--ce` / `--no-ce` | `ce_mark` | on |
| `--ex` / `--no-ex` | `ex_mark` | on |
| `--hole-diameter-mm` | `hole_diameter_mm` | `3.5` |
| `--out-dir` | `out_dir` | this folder |
| `--output-name` | `output_name` | `nameplate-A2_<serial>` |
| `--strict` | `strict` | off |

Footer segments are joined with ` · `. An empty segment is dropped along with its separator.

The QR code and the barcode are independent: the serial number is not encoded anywhere unless you pass it in `--barcode-data` or `--qr-data`.

## SVG structure

- Units: `width="65mm" height="35mm"`, `viewBox="0 0 650 350"` (10 units = 1 mm), so it imports at true size.
- Layer `engrave`: all vector marks in one filled path (`#marks`), plus the logo as an embedded PNG (`#logo`). The negative file adds `#plate-fill`.
- Layer `cut`: plate outline (R1) and the two mounting holes (centres 4.5 mm from the short edges), red `#ff0000` hairlines, no fill.
- All text is converted to outlines (Inter, shaped with HarfBuzz, kerning applied). No fonts are needed on the laser PC.
- CE mark, Ex hexagon, divider and row rules are filled shapes, not strokes, so stroke-width is never lost on import.
- QR: 9 x 9 mm, no quiet-zone border inside the box. Barcode: Code 128, 30 x 4 mm.

## Warnings and checks

The script prints warnings (stderr) when:

- `qr_data` or `barcode_data` is missing (placeholder drawn).
- The QR module would be smaller than 0.25 mm, or the Code 128 module smaller than 0.15 mm.
- Any text overflows its area (value colliding with its label, product name into the logo, footer into the CE/Ex marks). Add `--strict` to stop without writing files in that case.

## Known limits

- The logo is the raster PNG from the design (`assets/iotech-logo.png`, 676 x 244 px). It is recoloured to a single tone through its alpha channel, the same way the design's CSS mask does. At 16.6 mm wide this is about 1000 dpi.
- The dashed QR placeholder uses live text and strokes. It is a preview aid only, not for production.

## Files

```
make_nameplate.py         generator
requirements.txt          fonttools, uharfbuzz, segno, python-barcode, pillow
setup.sh                  venv setup
example.json              sample values
assets/iotech-logo.png    logo from the design
assets/fonts/             Inter variable fonts (SIL OFL, see OFL.txt)
```
