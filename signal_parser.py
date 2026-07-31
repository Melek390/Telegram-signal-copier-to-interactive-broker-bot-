"""
signal_parser.py — Classify Telegram signals and extract order details via OCR.

Pipeline:
  1. BUY/SELL classification — Arabic keywords in message text only
       كول / بوت  → BUY
       خفف        → SELL
       no image   → IGNORE
  2. Order extraction — Google Cloud Vision OCR (preferred) or pytesseract fallback
       Parses the IBKR position card format:
         TICKER    PRICE          (pytesseract layout)
         MON DD 'YY  STRIKE  Call/Put
       or:
         TICKER                   (Google Vision layout)
         MON DD 'YY  STRIKE  Call/Put
         PRICE

Set GOOGLE_VISION_API_KEY env var to use Google Vision; falls back to pytesseract.

Run: python signal_parser.py [--dir signal_examples]
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

# ── Arabic signal keywords ────────────────────────────────────────────────────
BUY_KEYWORDS  = ["كول", "بوت"]
SELL_KEYWORDS = ["خفف"]

_MONTH_MAP = {
    "JAN": "01", "FEB": "02", "MAR": "03", "APR": "04",
    "MAY": "05", "JUN": "06", "JUL": "07", "AUG": "08",
    "SEP": "09", "OCT": "10", "NOV": "11", "DEC": "12",
}


# ── Step 1: Classify from text ────────────────────────────────────────────────

def classify_text(text: str) -> str | None:
    """Return 'BUY', 'SELL', or None."""
    for kw in BUY_KEYWORDS:
        if kw in text:
            return "BUY"
    for kw in SELL_KEYWORDS:
        if kw in text:
            return "SELL"
    return None


# ── Step 2: OCR the image ─────────────────────────────────────────────────────

def _ocr_google_vision(image_path: Path, api_key: str) -> str:
    """Call Google Cloud Vision TEXT_DETECTION. Returns full detected text."""
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")
    payload = json.dumps({
        "requests": [{
            "image": {"content": image_b64},
            "features": [{"type": "TEXT_DETECTION"}],
        }]
    }).encode("utf-8")
    url = f"https://vision.googleapis.com/v1/images:annotate?key={api_key}"
    req = urllib.request.Request(url, data=payload,
                                  headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    annotations = result["responses"][0].get("textAnnotations", [])
    return annotations[0]["description"].strip() if annotations else ""


def _ocr_tesseract(image_path: Path) -> str:
    """Pytesseract fallback OCR."""
    import pytesseract
    from PIL import Image
    if sys.platform == "win32":
        pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    img = Image.open(image_path)
    w, h = img.size
    img2x = img.resize((w * 2, h * 2), Image.LANCZOS)
    return pytesseract.image_to_string(img2x, config="--psm 6 --oem 3").strip()


def ocr_image(image_path: Path) -> str:
    """OCR an image. Uses Google Vision if GOOGLE_VISION_API_KEY is set, else pytesseract."""
    api_key = os.getenv("GOOGLE_VISION_API_KEY", "")
    if api_key:
        return _ocr_google_vision(image_path, api_key)
    return _ocr_tesseract(image_path)


# ── Step 3: Parse order fields from OCR text ──────────────────────────────────

def parse_order(ocr_text: str) -> dict:
    """
    Parse the IBKR position card OCR output.

    Expected format (two lines):
      TICKER    PRICE
      MON DD 'YY  STRIKE  Call/Put

    Examples:
      "TSLA 1.73\nJUN 05 '26 500 Call"
      "NVDA 0.75\nJUN 05 '26 230 Call"
      "MSFT\nJUN 01 '26 430 Call"
    """
    upper = ocr_text.upper()
    order = {
        "ticker":      None,
        "option_type": None,
        "strike":      None,
        "expiry":      None,
        "entry_price": None,
    }

    # Ticker — first standalone 1–5 letter word that isn't a month or an option word.
    # Without this guard an OCR layout that emits the date line first yields ticker="JUN".
    _NOT_TICKERS = set(_MONTH_MAP) | {
        "CALL", "PUT", "BUY", "SELL", "QTY", "EXP", "AVG", "COST", "USD", "OPT",
    }
    for cand in re.findall(r"\b([A-Z]{1,5})\b", upper):
        if cand not in _NOT_TICKERS:
            order["ticker"] = cand
            break

    # Option type
    if "CALL" in upper:
        order["option_type"] = "C"
    elif "PUT" in upper:
        order["option_type"] = "P"

    # Expiry — MON DD 'YY
    months_pat = "|".join(_MONTH_MAP.keys())
    m = re.search(rf"\b({months_pat})\b\s+(\d{{1,2}})\s+'(\d{{2}})", upper)
    if m:
        mon  = _MONTH_MAP[m.group(1)]
        day  = m.group(2).zfill(2)
        year = "20" + m.group(3)
        order["expiry"] = f"{year}-{mon}-{day}"

    # Strike — number immediately before CALL/PUT
    m = re.search(rf"(\d+(?:\.\d+)?)\s+(?:CALL|PUT)", upper)
    if m:
        order["strike"] = float(m.group(1))

    # Entry price — decimal number on first or last line of card
    # Google Vision puts price on last line; pytesseract puts it on first line
    # Skip the strike value if it appears as a decimal
    lines = ocr_text.split("\n") if "\n" in ocr_text else [ocr_text]
    for line in [lines[0], lines[-1]]:
        m = re.search(r"\b(\d+\.\d+)\b", line)
        if m:
            candidate = float(m.group(1))
            if candidate != order.get("strike"):
                order["entry_price"] = candidate
                break

    return order


# ── Main pipeline ─────────────────────────────────────────────────────────────

def parse_signal_dir(signal_dir: Path) -> list[dict]:
    json_files = sorted(f for f in signal_dir.glob("*.json") if not f.name.startswith("_"))

    results = []
    seen_msg_ids: set[int] = set()

    for jf in json_files:
        with open(jf, encoding="utf-8") as f:
            msg = json.load(f)

        msg_id     = msg.get("message_id")
        text       = msg.get("text", "")
        has_image  = msg.get("has_image", False)
        image_file = msg.get("image_file")

        # Deduplicate by message_id (same message can be saved by multiple JSON files)
        if msg_id in seen_msg_ids:
            continue
        seen_msg_ids.add(msg_id)

        # No image → not a signal
        if not has_image:
            results.append({"message_id": msg_id, "classification": "IGNORE", "reason": "no image"})
            continue

        # Classify from text keywords
        direction = classify_text(text)
        if direction is None:
            results.append({
                "message_id":     msg_id,
                "classification": "UNCLASSIFIED",
                "reason":         "has image but no BUY/SELL keyword",
                "text_snippet":   text[:120],
            })
            continue

        # OCR the image
        image_path = signal_dir / image_file if image_file else None
        ocr_text = ""
        order = {}
        if image_path and image_path.exists():
            print(f"  OCR-ing {image_path.name}…", flush=True)
            ocr_text = ocr_image(image_path)
            order = parse_order(ocr_text)
        else:
            print(f"  WARNING: image not found: {image_file}", flush=True)

        results.append({
            "message_id":     msg_id,
            "classification": direction,
            "order":          order,
            "ocr_text":       ocr_text,
        })

    return results


# ── Report ────────────────────────────────────────────────────────────────────

def print_report(results: list[dict]):
    print("\n" + "=" * 60)
    print("  SIGNAL CLASSIFICATION REPORT")
    print("=" * 60)

    for r in results:
        mid = r["message_id"]
        cls = r["classification"]

        if cls == "IGNORE":
            print(f"\n[msg {mid}]  IGNORE  — {r['reason']}")

        elif cls == "UNCLASSIFIED":
            print(f"\n[msg {mid}]  UNCLASSIFIED  — {r['reason']}")
            print(f"  text: {r.get('text_snippet', '')!r}")

        else:
            o   = r["order"]
            opt = {"C": "Call", "P": "Put"}.get(o.get("option_type"), "?")
            print(f"\n[msg {mid}]  {cls}")
            print(f"  ticker:      {o['ticker']}")
            print(f"  option_type: {o['option_type']}  ({opt})")
            print(f"  strike:      {o['strike']}")
            print(f"  expiry:      {o['expiry']}")
            print(f"  entry_price: {o['entry_price']}")
            print(f"  ocr_raw:     {r['ocr_text']!r}")

    print("\n" + "=" * 60)
    counts: dict[str, int] = {}
    for r in results:
        counts[r["classification"]] = counts.get(r["classification"], 0) + 1
    for cls, n in sorted(counts.items()):
        print(f"  {cls}: {n}")
    print("=" * 60 + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="signal_examples")
    args = ap.parse_args()

    signal_dir = Path(args.dir)
    if not signal_dir.exists():
        print(f"ERROR: {signal_dir} not found", file=sys.stderr)
        sys.exit(1)

    print(f"Scanning {signal_dir.resolve()} …")
    results = parse_signal_dir(signal_dir)
    print_report(results)

    out_path = signal_dir / "_classification_results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"Results saved -> {out_path}")


if __name__ == "__main__":
    main()
