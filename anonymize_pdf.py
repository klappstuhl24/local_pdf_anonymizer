#!/usr/bin/env python3
"""Anonymize PDFs containing sensitive data using a local Ollama model.

Pipeline:
  1. Extract (PyMuPDF): text spans, images/logos and vector graphics with
     exact positions, font sizes and colors. Scanned pages are detected
     automatically and read via Tesseract OCR with word coordinates.
  2. Detect (Ollama): a local LLM builds a replacement table that maps
     every sensitive value (names, IBANs, addresses, ...) to an invented
     but format-preserving substitute.
  3. Rebuild (LaTeX): every element is placed at its original position
     via a TikZ overlay, so layout, icons and logos are preserved.
     For scans, the page image stays as background and sensitive spots
     are covered with background-colored patches plus replacement text.
  4. Compile: the LaTeX file is compiled back to a PDF
     (tectonic / latexmk / pdflatex).

Usage:
  python anonymize_pdf.py contracts_dir -o results   # batch mode
  python anonymize_pdf.py single_file.pdf -o results # single file

All processing runs 100% locally; no document data ever leaves the machine.
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import fitz  # PyMuPDF
import requests

try:
    import pytesseract
    from PIL import Image
    HAS_OCR = True
except ImportError:
    HAS_OCR = False

OLLAMA_URL_DEFAULT = "http://localhost:11434"
MODEL_DEFAULT = "mistral-small3.2:24b"
CHUNK_CHARS = 12000       # characters of document text per LLM request
OCR_DPI = 300             # render resolution for scanned pages
OCR_LANG = "deu+eng"
SCAN_TEXT_THRESHOLD = 20  # fewer extractable chars => page is treated as scan

SYSTEM_PROMPT = """You are a document anonymization tool.
You receive the full text of a document and produce a replacement table
for ALL sensitive or personally identifiable information.

Sensitive information includes in particular:
- First and last names of persons
- Company names (except generic terms)
- Streets, house numbers, postal codes, cities
- Phone and fax numbers, e-mail addresses, websites
- IBAN, BIC, account and card numbers, tax numbers, VAT IDs
- Birth dates, ID / social security numbers
- Customer, contract, personnel and file reference numbers
- Names in signatures

Rules for the replacement values:
- Invent plausible but entirely fictitious values.
- A replacement must keep the same format and roughly the same length
  (e.g. IBAN -> valid-looking invented IBAN with the same country prefix,
  date -> another valid date in the same format, name -> another name).
- Replacement values must match the language of the document.
- Identical original values must always get the same replacement.
- Do NOT replace ordinary words, legal clauses, statutory references,
  contract amounts or anything without personal reference.
- Quote original values exactly as they appear in the text
  (same casing, same whitespace).

Answer EXCLUSIVELY with a JSON object of this form:
{"mapping": {"original value 1": "replacement 1", "original value 2": "replacement 2"}}
"""


# ----------------------------------------------------------------------------
# Step 1: extract PDF content
# ----------------------------------------------------------------------------

def ocr_scan_page(page, page_index: int, assets_dir: Path) -> dict:
    """Render a scanned page and OCR it with word-level coordinates.

    The rendered page image is kept as the page background later on.
    """
    pix = page.get_pixmap(dpi=OCR_DPI)
    img_name = f"p{page_index}_scan.png"
    pix.save(assets_dir / img_name)

    image = Image.open(assets_dir / img_name)
    data = pytesseract.image_to_data(
        image, lang=OCR_LANG, output_type=pytesseract.Output.DICT
    )

    scale = page.rect.width / pix.width  # pixels -> PDF points
    lines: dict = {}
    for i in range(len(data["text"])):
        word = data["text"][i].strip()
        if not word or int(data["conf"][i]) < 0:
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        x, y = data["left"][i], data["top"][i]
        w, h = data["width"][i], data["height"][i]
        lines.setdefault(key, []).append({
            "text": word,
            "bbox": (x * scale, y * scale, (x + w) * scale, (y + h) * scale),
        })

    return {
        "file": img_name,
        "scale": 1.0 / scale,  # PDF points -> pixels (for color sampling)
        "lines": [lines[k] for k in sorted(lines)],
    }


def extract_pdf(pdf_path: Path, assets_dir: Path) -> list:
    """Extract all pages as text spans, images, vector drawings or OCR data."""
    doc = fitz.open(pdf_path)
    assets_dir.mkdir(parents=True, exist_ok=True)
    pages = []

    for page_index, page in enumerate(doc):
        page_data = {
            "width": page.rect.width,
            "height": page.rect.height,
            "spans": [],
            "images": [],
            "drawings": [],
            "scan": None,
        }

        # Scanned page? (barely any extractable text but images present)
        plain_text = page.get_text().strip()
        if len(plain_text) < SCAN_TEXT_THRESHOLD and page.get_images(full=True):
            if not HAS_OCR:
                raise RuntimeError(
                    f"Page {page_index + 1} is a scan but OCR is unavailable. "
                    "Install it via: brew install tesseract tesseract-lang "
                    "&& pip install pytesseract Pillow"
                )
            print(f"    Page {page_index + 1}: scan detected, running OCR ...")
            page_data["scan"] = ocr_scan_page(page, page_index, assets_dir)
            pages.append(page_data)
            continue

        # Text with position, font, size and color
        text_dict = page.get_text("dict")
        for block in text_dict["blocks"]:
            if block["type"] != 0:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    if not span["text"].strip():
                        continue
                    page_data["spans"].append({
                        "text": span["text"],
                        "origin": span["origin"],  # baseline start point
                        "size": span["size"],
                        "font": span["font"],
                        "flags": span["flags"],
                        "color": span["color"],
                    })

        # Save embedded images / logos / icons as PNG
        seen_xrefs = set()
        for img in page.get_images(full=True):
            xref = img[0]
            if xref in seen_xrefs:
                continue
            seen_xrefs.add(xref)
            rects = page.get_image_rects(xref)
            if not rects:
                continue
            img_name = f"p{page_index}_img{xref}.png"
            img_path = assets_dir / img_name
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.colorspace and pix.colorspace.n > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                pix.save(img_path)
            except Exception as exc:
                print(f"  Warning: could not extract image {xref}: {exc}")
                continue
            for rect in rects:
                page_data["images"].append({
                    "file": img_name,
                    "bbox": (rect.x0, rect.y0, rect.x1, rect.y1),
                })

        # Vector graphics (lines, frames, table rules, backgrounds)
        for drawing in page.get_drawings():
            page_data["drawings"].append({
                "items": drawing["items"],
                "stroke": drawing.get("color"),
                "fill": drawing.get("fill"),
                "width": drawing.get("width") or 1.0,
                "stroke_opacity": drawing.get("stroke_opacity", 1) or 1,
                "fill_opacity": drawing.get("fill_opacity", 1) or 1,
            })

        pages.append(page_data)

    doc.close()
    return pages


# ----------------------------------------------------------------------------
# Step 2: ask Ollama for the replacement table
# ----------------------------------------------------------------------------

def collect_document_text(pages) -> str:
    """Concatenate all extracted (or OCR'd) text of the document."""
    lines = []
    for page in pages:
        if page["scan"]:
            for words in page["scan"]["lines"]:
                lines.append(" ".join(w["text"] for w in words))
        else:
            for span in page["spans"]:
                lines.append(span["text"])
    return "\n".join(lines)


def ask_ollama_for_mapping(text: str, model: str, ollama_url: str) -> dict:
    """Query the local model (in chunks if needed) for the replacement table."""
    chunks = [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)]
    mapping: dict = {}

    for i, chunk in enumerate(chunks, 1):
        print(f"  Querying model '{model}' (part {i}/{len(chunks)}) ...")
        user_prompt = ""
        if mapping:
            user_prompt += (
                "Replacements already decided (reuse them consistently):\n"
                + json.dumps(mapping, ensure_ascii=False)
                + "\n\n"
            )
        user_prompt += "Document text:\n---\n" + chunk + "\n---"

        chunk_mapping = None
        for attempt in range(3):
            try:
                resp = requests.post(
                    f"{ollama_url}/api/chat",
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        "format": "json",
                        "stream": False,
                        "options": {"temperature": 0.3, "num_ctx": 16384},
                    },
                    timeout=600,
                )
                resp.raise_for_status()
                content = resp.json()["message"]["content"]
                parsed = json.loads(content)
                candidate = parsed.get("mapping", parsed)
                if isinstance(candidate, dict):
                    chunk_mapping = {
                        str(k): str(v)
                        for k, v in candidate.items()
                        if str(k).strip() and str(k) != str(v)
                    }
                    break
            except (requests.RequestException, json.JSONDecodeError, KeyError) as exc:
                print(f"  Attempt {attempt + 1} failed: {exc}")
        if chunk_mapping is None:
            raise RuntimeError(
                "Ollama did not return a valid replacement table. "
                "Is the Ollama server running (ollama serve)?"
            )
        mapping.update(chunk_mapping)

    return mapping


def sample_background_color(image, bbox_px):
    """Return the most frequent color in a crop, i.e. the scan's paper color."""
    x0, y0, x1, y1 = (int(v) for v in bbox_px)
    pad = 3
    crop = image.crop((
        max(0, x0 - pad), max(0, y0 - pad),
        min(image.width, x1 + pad), min(image.height, y1 + pad),
    )).convert("RGB")
    colors = crop.getcolors(maxcolors=crop.width * crop.height)
    if not colors:
        return (255, 255, 255)
    return max(colors, key=lambda c: c[0])[1]


def build_scan_patch(scan, image, page_width, words, word_indices, text, orig_text):
    """Build one patch (cover rectangle + replacement text) for words of a line."""
    group_words = [words[i] for i in word_indices]
    x0 = min(w["bbox"][0] for w in group_words)
    y0 = min(w["bbox"][1] for w in group_words)
    x1 = max(w["bbox"][2] for w in group_words)
    y1 = max(w["bbox"][3] for w in group_words)

    # Available width: up to the next original word in the line,
    # otherwise up to the (estimated) right page margin
    next_idx = word_indices[-1] + 1
    if next_idx < len(words):
        available = words[next_idx]["bbox"][0] - 4 - x0
    else:
        available = page_width * 0.92 - x0
    s = scan["scale"]
    bg = sample_background_color(image, (x0 * s, y0 * s, x1 * s, y1 * s))

    # Derive the font size from the box geometry: the box spans from the
    # top of the tallest glyph to the bottom of the deepest one.
    # Descenders (g, j, p, ...) push the baseline up; caps set the cap height.
    height = y1 - y0
    has_desc = any(c in "gjpqyQß();,[]" for c in orig_text)
    baseline = y1 - 0.22 * height if has_desc else y1
    cap_height = baseline - y0
    font_size = cap_height / 0.72

    # Replacement too wide? Shrink the font (approx. 0.5 em per character).
    est_width = len(text) * font_size * 0.5
    if est_width > available > 0:
        font_size *= max(available / est_width, 0.55)

    return {
        "text": text,
        "bbox": (x0, y0, x1, y1),
        "baseline": baseline,
        "size": font_size,
        "bg": bg,
    }


def apply_mapping_scan_page(scan, ordered, image, page_width) -> int:
    """Create patches for a scanned page: regions painted over in the paper
    color and overwritten with replacement text.

    Matching runs over the continuous text of the whole page (line breaks
    count as spaces), so values spanning a line break are found as well.
    """
    flat = []  # (line index, word index within line, word)
    for li, words in enumerate(scan["lines"]):
        for wi, w in enumerate(words):
            flat.append((li, wi, w))

    offsets, pos = [], 0
    for _, _, w in flat:
        offsets.append((pos, pos + len(w["text"])))
        pos += len(w["text"]) + 1
    page_text = " ".join(w["text"] for _, _, w in flat)

    # Collect word indices (in the flat index) affected by any replacement
    hit_words = set()
    for original, _ in ordered:
        start = 0
        while True:
            idx = page_text.find(original, start)
            if idx == -1:
                break
            end = idx + len(original)
            hit_words.update(
                i for i, (s, e) in enumerate(offsets) if s < end and e > idx
            )
            start = end

    patches = []
    if hit_words:
        # Merge into contiguous word groups
        indices = sorted(hit_words)
        groups, current = [], [indices[0]]
        for i in indices[1:]:
            if i == current[-1] + 1:
                current.append(i)
            else:
                groups.append(current)
                current = [i]
        groups.append(current)

        for group in groups:
            orig_text = " ".join(flat[i][2]["text"] for i in group)
            text = orig_text
            for original, replacement in ordered:
                text = text.replace(original, replacement)

            # Split the group by line; the full replacement text goes into
            # the first segment, following segments are only painted over.
            segments: list = []
            for i in group:
                li, wi, _ = flat[i]
                if segments and segments[-1][0] == li:
                    segments[-1][1].append(wi)
                else:
                    segments.append((li, [wi]))
            for seg_no, (li, word_indices) in enumerate(segments):
                seg_text = text if seg_no == 0 else ""
                patches.append(build_scan_patch(
                    scan, image, page_width, scan["lines"][li],
                    word_indices, seg_text, orig_text,
                ))

    scan["patches"] = patches
    return len(patches)


def apply_mapping(pages, mapping: dict, assets_dir: Path) -> int:
    """Apply replacements to all text spans / scan pages (longest first)."""
    ordered = sorted(mapping.items(), key=lambda kv: len(kv[0]), reverse=True)
    replaced_count = 0
    for page in pages:
        if page["scan"]:
            image = Image.open(assets_dir / page["scan"]["file"])
            replaced_count += apply_mapping_scan_page(
                page["scan"], ordered, image, page["width"]
            )
            continue
        for span in page["spans"]:
            new_text = span["text"]
            for original, replacement in ordered:
                if original in new_text:
                    new_text = new_text.replace(original, replacement)
            if new_text != span["text"]:
                replaced_count += 1
                span["text"] = new_text
    return replaced_count


# ----------------------------------------------------------------------------
# Step 3: generate LaTeX
# ----------------------------------------------------------------------------

TEX_ESCAPES = {
    "\\": r"\textbackslash{}",
    "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}",
    "~": r"\textasciitilde{}", "^": r"\textasciicircum{}",
    "ß": r"\ss{}",  # XeTeX would otherwise render the UTF-8 ß as "SS"
}


def tex_escape(text: str) -> str:
    """Escape LaTeX special characters."""
    return "".join(TEX_ESCAPES.get(ch, ch) for ch in text)


def int_to_rgb(color_int: int):
    """Convert a PyMuPDF packed integer color to an RGB tuple (0..255)."""
    return (color_int >> 16) & 255, (color_int >> 8) & 255, color_int & 255


def rgb_tuple(color):
    """Convert a PyMuPDF float color (0..1 per channel) to RGB (0..255)."""
    return tuple(round(c * 255) for c in color[:3])


def span_to_tikz(span) -> str:
    """Place a text span as a TikZ node anchored at its original baseline."""
    x, y = span["origin"]
    r, g, b = int_to_rgb(span["color"])
    size = span["size"]
    flags = span["flags"]

    styles = []
    if flags & 2 ** 2:        # serif font
        styles.append(r"\rmfamily")
    elif flags & 2 ** 3:      # monospace
        styles.append(r"\ttfamily")
    else:
        styles.append(r"\sffamily")
    if flags & 2 ** 4:        # bold
        styles.append(r"\bfseries")
    if flags & 2 ** 1:        # italic
        styles.append(r"\itshape")

    font_cmd = (
        f"\\fontsize{{{size:.2f}bp}}{{{size * 1.2:.2f}bp}}\\selectfont"
        + "".join(styles)
    )
    text = tex_escape(span["text"])
    return (
        f"\\node[anchor=base west, inner sep=0, text depth=0pt, "
        f"text={{rgb,255:red,{r};green,{g};blue,{b}}}] "
        f"at ({x:.2f},{y:.2f}) {{{font_cmd} {text}}};"
    )


def image_to_tikz(image) -> str:
    """Place an extracted image at its original bounding box."""
    x0, y0, x1, y1 = image["bbox"]
    w, h = x1 - x0, y1 - y0
    return (
        f"\\node[anchor=north west, inner sep=0] at ({x0:.2f},{y0:.2f}) "
        f"{{\\includegraphics[width={w:.2f}bp,height={h:.2f}bp]"
        f"{{assets/{image['file']}}}}};"
    )


def drawing_to_tikz(drawing) -> list:
    """Translate vector lines, rectangles, quads and curves to TikZ paths."""
    commands = []
    opts = []
    if drawing["stroke"]:
        r, g, b = rgb_tuple(drawing["stroke"])
        opts.append(f"draw={{rgb,255:red,{r};green,{g};blue,{b}}}")
        opts.append(f"line width={drawing['width']:.2f}bp")
        if drawing["stroke_opacity"] < 1:
            opts.append(f"draw opacity={drawing['stroke_opacity']:.2f}")
    if drawing["fill"]:
        r, g, b = rgb_tuple(drawing["fill"])
        opts.append(f"fill={{rgb,255:red,{r};green,{g};blue,{b}}}")
        if drawing["fill_opacity"] < 1:
            opts.append(f"fill opacity={drawing['fill_opacity']:.2f}")
    if not opts:
        return commands
    opt_str = ", ".join(opts)

    for item in drawing["items"]:
        kind = item[0]
        if kind == "l":      # line
            p1, p2 = item[1], item[2]
            commands.append(
                f"\\path[{opt_str}] ({p1.x:.2f},{p1.y:.2f}) -- ({p2.x:.2f},{p2.y:.2f});"
            )
        elif kind == "re":   # rectangle
            rect = item[1]
            commands.append(
                f"\\path[{opt_str}] ({rect.x0:.2f},{rect.y0:.2f}) "
                f"rectangle ({rect.x1:.2f},{rect.y1:.2f});"
            )
        elif kind == "qu":   # quad
            quad = item[1]
            pts = [quad.ul, quad.ur, quad.lr, quad.ll]
            path = " -- ".join(f"({p.x:.2f},{p.y:.2f})" for p in pts)
            commands.append(f"\\path[{opt_str}] {path} -- cycle;")
        elif kind == "c":    # Bezier curve
            p1, c1, c2, p2 = item[1], item[2], item[3], item[4]
            commands.append(
                f"\\path[{opt_str}] ({p1.x:.2f},{p1.y:.2f}) "
                f".. controls ({c1.x:.2f},{c1.y:.2f}) and ({c2.x:.2f},{c2.y:.2f}) "
                f".. ({p2.x:.2f},{p2.y:.2f});"
            )
    return commands


def scan_page_to_tikz(page) -> list:
    """Scanned page: original scan as background plus color-matched cover
    rectangles carrying the replacement text."""
    scan = page["scan"]
    parts = [
        f"\\node[anchor=north west, inner sep=0] at (0,0) "
        f"{{\\includegraphics[width={page['width']:.2f}bp,"
        f"height={page['height']:.2f}bp]{{assets/{scan['file']}}}}};"
    ]
    for patch in scan.get("patches", []):
        x0, y0, x1, y1 = patch["bbox"]
        r, g, b = patch["bg"]
        pad = 1.5
        parts.append(
            f"\\fill[fill={{rgb,255:red,{r};green,{g};blue,{b}}}] "
            f"({x0 - pad:.2f},{y0 - pad:.2f}) rectangle "
            f"({x1 + pad:.2f},{y1 + pad:.2f});"
        )
        if not patch["text"]:
            continue
        size = patch["size"]
        text = tex_escape(patch["text"])
        parts.append(
            f"\\node[anchor=base west, inner sep=0, text depth=0pt, "
            f"text=black] at ({x0:.2f},{patch['baseline']:.2f}) "
            f"{{\\fontsize{{{size:.2f}bp}}{{{size * 1.2:.2f}bp}}"
            f"\\selectfont\\sffamily {text}}};"
        )
    return parts


def build_latex(pages) -> str:
    """Build the full LaTeX document as one TikZ overlay per page."""
    width = pages[0]["width"]
    height = pages[0]["height"]
    parts = [
        r"\documentclass{article}",
        f"\\usepackage[paperwidth={width:.2f}bp,paperheight={height:.2f}bp,margin=0pt]{{geometry}}",
        r"\usepackage{tikz}",
        r"\usepackage{graphicx}",
        r"\usepackage[T1]{fontenc}",
        r"\usepackage[utf8]{inputenc}",
        r"\pagestyle{empty}",
        r"\setlength{\parindent}{0pt}",
        r"\begin{document}",
    ]

    for i, page in enumerate(pages):
        parts.append(r"\begin{tikzpicture}[remember picture, overlay,")
        parts.append(r"  shift={(current page.north west)}, x=1bp, y=-1bp]")
        if page["scan"]:
            parts.extend(scan_page_to_tikz(page))
        else:
            # Z-order: images at the bottom, then vector graphics, text on top
            for image in page["images"]:
                parts.append(image_to_tikz(image))
            for drawing in page["drawings"]:
                parts.extend(drawing_to_tikz(drawing))
            for span in page["spans"]:
                parts.append(span_to_tikz(span))
        parts.append(r"\end{tikzpicture}")
        parts.append(r"\phantom{x}")  # page must not be empty
        if i < len(pages) - 1:
            parts.append(r"\clearpage")

    parts.append(r"\end{document}")
    return "\n".join(parts)


# ----------------------------------------------------------------------------
# Step 4: compile LaTeX -> PDF
# ----------------------------------------------------------------------------

def compile_latex(tex_path: Path) -> Path:
    """Compile a .tex file to PDF using tectonic, latexmk or pdflatex."""
    tex_path = tex_path.resolve()
    workdir = tex_path.parent
    pdf_path = tex_path.with_suffix(".pdf")

    if shutil.which("tectonic"):
        cmd = ["tectonic", "--outdir", str(workdir), str(tex_path)]
    elif shutil.which("latexmk"):
        cmd = ["latexmk", "-pdf", f"-output-directory={workdir}",
               "-interaction=nonstopmode", str(tex_path)]
    elif shutil.which("pdflatex"):
        cmd = ["pdflatex", f"-output-directory={workdir}",
               "-interaction=nonstopmode", str(tex_path)]
    else:
        raise RuntimeError(
            "No LaTeX compiler found. Install one, e.g.: brew install tectonic"
        )

    print(f"  Compiling with: {cmd[0]}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=workdir)
    if result.returncode != 0 or not pdf_path.exists():
        log = (result.stdout + result.stderr)[-3000:]
        raise RuntimeError(f"LaTeX compilation failed:\n{log}")
    return pdf_path


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------

def anonymize(pdf_path: Path, outdir: Path, model: str, ollama_url: str,
              use_llm: bool = True) -> Path:
    """Run the full pipeline for one PDF; return the anonymized PDF path."""
    outdir.mkdir(parents=True, exist_ok=True)
    assets_dir = outdir / "assets"
    stem = pdf_path.stem

    print("1/4 Extracting PDF content ...")
    pages = extract_pdf(pdf_path, assets_dir)
    n_spans = sum(len(p["spans"]) for p in pages)
    n_imgs = sum(len(p["images"]) for p in pages)
    n_scans = sum(1 for p in pages if p["scan"])
    print(f"    {len(pages)} pages ({n_scans} scanned), "
          f"{n_spans} text elements, {n_imgs} images")

    if not use_llm:
        print("2/4 Skipped (--no-llm)")
    else:
        print("2/4 Detecting sensitive data via Ollama ...")
        text = collect_document_text(pages)
        mapping = ask_ollama_for_mapping(text, model, ollama_url)
        print(f"    {len(mapping)} replacements found:")
        for original, replacement in mapping.items():
            print(f"      {original!r} -> {replacement!r}")
        mapping_path = outdir / f"{stem}_mapping.json"
        mapping_path.write_text(
            json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        changed = apply_mapping(pages, mapping, assets_dir)
        print(f"    {changed} text elements changed "
              f"(table saved to {mapping_path})")

    print("3/4 Generating LaTeX ...")
    tex_path = outdir / f"{stem}_anonymized.tex"
    tex_path.write_text(build_latex(pages), encoding="utf-8")
    print(f"    {tex_path}")

    print("4/4 Compiling PDF ...")
    pdf_out = compile_latex(tex_path)
    print(f"    {pdf_out}")
    return pdf_out


def main():
    parser = argparse.ArgumentParser(
        description="Anonymize PDFs with a local Ollama model. "
                    "Accepts a single PDF or a directory of PDFs."
    )
    parser.add_argument("input", type=Path,
                        help="input PDF file or directory containing PDFs")
    parser.add_argument("-o", "--outdir", type=Path, default=Path("results"),
                        help="results directory (default: results/)")
    parser.add_argument("--model", default=MODEL_DEFAULT,
                        help=f"Ollama model (default: {MODEL_DEFAULT})")
    parser.add_argument("--ollama-url", default=OLLAMA_URL_DEFAULT,
                        help=f"Ollama server (default: {OLLAMA_URL_DEFAULT})")
    parser.add_argument("--no-llm", action="store_true",
                        help="layout reconstruction only, no anonymization "
                             "(for testing the LaTeX pipeline)")
    args = parser.parse_args()

    if not args.input.exists():
        sys.exit(f"Input not found: {args.input}")

    if args.input.is_dir():
        pdf_files = sorted(args.input.glob("*.pdf"))
        if not pdf_files:
            sys.exit(f"No PDFs found in {args.input}")
    else:
        pdf_files = [args.input]

    pdfs_dir = args.outdir / "pdfs"
    pdfs_dir.mkdir(parents=True, exist_ok=True)

    succeeded, failed = [], []
    for i, pdf_path in enumerate(pdf_files, 1):
        outdir = args.outdir / f"output_{i}"
        print(f"\n=== [{i}/{len(pdf_files)}] {pdf_path.name} -> {outdir} ===")
        try:
            pdf_out = anonymize(
                pdf_path, outdir, args.model, args.ollama_url,
                use_llm=not args.no_llm,
            )
            final_path = pdfs_dir / pdf_out.name
            shutil.copy2(pdf_out, final_path)
            succeeded.append((pdf_path.name, final_path))
        except Exception as exc:
            print(f"  ERROR while processing {pdf_path.name}: {exc}")
            failed.append(pdf_path.name)

    print(f"\n{'=' * 60}")
    print(f"Done: {len(succeeded)} succeeded, {len(failed)} failed")
    for name, final_path in succeeded:
        print(f"  {name} -> {final_path}")
    for name in failed:
        print(f"  FAILED: {name}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
