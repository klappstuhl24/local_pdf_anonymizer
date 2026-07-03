#!/usr/bin/env python3
"""Anonymize PDFs containing sensitive data using a local Ollama model.

Pipeline:
  1. Extract (PyMuPDF): text spans, images/logos and vector graphics with
     exact positions, font sizes and colors. Vector path clusters are
     rasterized to PNG in assets/. Scanned pages are detected automatically
     and read via Tesseract OCR with word coordinates.
  2. Filter signatures (YOLOv8): extracted images and vector PNGs are
     checked for handwritten signatures; matches are removed from the output.
  3. Detect (Ollama): a local LLM builds a replacement table that maps
     every sensitive value (names, IBANs, addresses, ...) to an invented
     but format-preserving substitute.
  4. Rebuild (LaTeX): every element is placed at its original position
     via a TikZ overlay, so layout, icons and logos are preserved.
     For scans, the page image stays as background and sensitive spots
     are covered with background-colored patches plus replacement text.
  5. Compile: the LaTeX file is compiled back to a PDF
     (tectonic / latexmk / pdflatex).

Usage:
  python anonymize_pdf.py contracts_dir -o results   # batch mode
  python anonymize_pdf.py single_file.pdf -o results # single file

All processing runs 100% locally; no document data ever leaves the machine.
"""

import argparse
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

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
# OCR misreads (wrong -> correct), applied to the final LaTeX output.
OCR_CHAR_FIXES = {"ğ": "§"}
SCAN_TEXT_THRESHOLD = 20  # fewer extractable chars => page is treated as scan
# True: scan pages use white background + OCR text only (no scan PNG in PDF).
# Avoids ghost text when overlay text is slightly misaligned vs. the scan image.
SCAN_OCR_ONLY = True
# Many scanned PDFs embed a full-page image plus an invisible OCR text layer.
# When extractable spans exist, drop background images covering this much of the page.
FULLPAGE_IMAGE_COVERAGE = 0.85
DROP_FULLPAGE_SCAN_BACKGROUNDS = True

SIGNATURE_MODEL_REPO = "tech4humans/yolov8s-signature-detector"
SIGNATURE_MODEL_FILE = "yolov8s.pt"
SIGNATURE_MODEL_FALLBACK_REPO = "Mels22/Signature-Detection-Verification"
SIGNATURE_MODEL_FALLBACK_FILE = "detector_yolo_1cls.pt"
SIGNATURE_CONF_DEFAULT = 0.22  # lower = more signatures, more false positives
VECTOR_CLUSTER_MERGE_PAD = 12   # PDF points: merge nearby drawing paths
VECTOR_RASTER_DPI = 200
VECTOR_MIN_CLUSTER_AREA = 80    # PDF points²; skip specks and huge fills

# Inference backend for YOLO and Ollama GPU layers: "gpu" or "cpu".
# "gpu" uses Apple MPS on Mac or NVIDIA CUDA when available (default: MPS/CUDA).
INFERENCE_DEVICE = "gpu"

OLLAMA_SYSTEM_PROMPT_PATH = (
    Path(__file__).resolve().parent / "prompts" / "ollama_system_prompt.md"
)

_signature_model = None
_run_logger: Optional[logging.Logger] = None


def _logger() -> logging.Logger:
    if _run_logger is not None:
        return _run_logger
    fallback = logging.getLogger("anonymize_pdf")
    if not fallback.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        fallback.addHandler(handler)
        fallback.setLevel(logging.INFO)
    return fallback


def log_info(msg: str) -> None:
    _logger().info(msg)


def log_warning(msg: str) -> None:
    _logger().warning(msg)


def log_error(msg: str) -> None:
    _logger().error(msg)


def setup_run_logger(outdir: Path, stem: str, pdf_path: Path, **config) -> Path:
    """Attach console + file logging for one PDF run; return the log file path."""
    global _run_logger
    outdir.mkdir(parents=True, exist_ok=True)
    log_path = outdir / f"{stem}_run.log.txt"

    logger = logging.getLogger(f"anonymize_pdf.{stem}")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(console)

    log_file = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    log_file.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(log_file)

    _run_logger = logger
    log_info(f"Run log: {log_path}")
    log_info(f"Input: {pdf_path.resolve()}")
    log_info(f"Output: {outdir.resolve()}")
    log_info(f"Started: {datetime.now().isoformat(timespec='seconds')}")
    for key, value in config.items():
        log_info(f"  {key}: {value}")
    return log_path


def teardown_run_logger() -> None:
    global _run_logger
    if _run_logger is None:
        return
    for handler in _run_logger.handlers[:]:
        handler.close()
        _run_logger.removeHandler(handler)
    _run_logger = None


def load_ollama_system_prompt() -> str:
    """Load the Ollama system prompt from prompts/ollama_system_prompt.md."""
    if not OLLAMA_SYSTEM_PROMPT_PATH.is_file():
        raise FileNotFoundError(
            f"Ollama system prompt not found: {OLLAMA_SYSTEM_PROMPT_PATH}"
        )
    return OLLAMA_SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip()


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
        "ocr_only": SCAN_OCR_ONLY,
        "lines": [lines[k] for k in sorted(lines)],
    }


def _bbox_page_coverage(bbox: tuple, page_width: float, page_height: float) -> float:
    x0, y0, x1, y1 = bbox
    area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    page_area = page_width * page_height
    return area / page_area if page_area else 0.0


def _is_fullpage_background(bbox: tuple, page_width: float, page_height: float) -> bool:
    return _bbox_page_coverage(bbox, page_width, page_height) >= FULLPAGE_IMAGE_COVERAGE


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
            "drawing_clusters": [],
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
            log_info(f"    Page {page_index + 1}: scan detected, running OCR ...")
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
                        "bbox": tuple(span["bbox"]),
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
            image_rects = []
            for rect in rects:
                bbox = (rect.x0, rect.y0, rect.x1, rect.y1)
                if (
                    DROP_FULLPAGE_SCAN_BACKGROUNDS
                    and page_data["spans"]
                    and _is_fullpage_background(
                        bbox, page_data["width"], page_data["height"]
                    )
                ):
                    continue
                image_rects.append(bbox)
            if not image_rects:
                log_info(
                    f"    Page {page_index + 1}: dropped full-page scan background "
                    f"(text layer present, xref {xref})"
                )
                continue
            img_name = f"p{page_index}_img{xref}.png"
            img_path = assets_dir / img_name
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.colorspace and pix.colorspace.n > 3:
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                pix.save(img_path)
            except Exception as exc:
                log_warning(f"  could not extract image {xref}: {exc}")
                continue
            for bbox in image_rects:
                page_data["images"].append({"file": img_name, "bbox": bbox})

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

        page_data["drawing_clusters"] = rasterize_vector_clusters(
            page_index, page_data["drawings"],
            page_data["width"], page_data["height"], assets_dir,
        )

        pages.append(page_data)

    doc.close()
    return pages


def _drawing_bbox(drawing) -> Optional[tuple]:
    """Axis-aligned bounds of one vector drawing in PDF points."""
    xs, ys = [], []
    for item in drawing["items"]:
        kind = item[0]
        if kind == "l":
            for pt in (item[1], item[2]):
                xs.append(pt.x)
                ys.append(pt.y)
        elif kind == "re":
            rect = item[1]
            xs.extend((rect.x0, rect.x1))
            ys.extend((rect.y0, rect.y1))
        elif kind == "qu":
            quad = item[1]
            for pt in (quad.ul, quad.ur, quad.lr, quad.ll):
                xs.append(pt.x)
                ys.append(pt.y)
        elif kind == "c":
            for pt in (item[1], item[2], item[3], item[4]):
                xs.append(pt.x)
                ys.append(pt.y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_area(bbox) -> float:
    return max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])


def _union_bbox(a, b) -> tuple:
    return min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])


def _bboxes_near(a, b, pad: float) -> bool:
    return not (
        a[2] + pad < b[0] - pad or b[2] + pad < a[0] - pad
        or a[3] + pad < b[1] - pad or b[3] + pad < a[1] - pad
    )


def _skip_drawing_for_raster(bbox, page_width: float, page_height: float) -> bool:
    """Ignore page-filling backgrounds and ruler lines."""
    if bbox is None:
        return True
    x0, y0, x1, y1 = bbox
    w, h = x1 - x0, y1 - y0
    area = w * h
    page_area = page_width * page_height
    if area < VECTOR_MIN_CLUSTER_AREA:
        return True
    if area > page_area * 0.35:
        return True
    if h < 1.5 and w > page_width * 0.6:
        return True
    if w < 1.5 and h > page_height * 0.6:
        return True
    return False


def _cluster_drawing_indices(bboxes, pad: float) -> list:
    """Group drawing indices whose bounding boxes touch or are near each other."""
    n = len(bboxes)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    for i in range(n):
        if bboxes[i] is None:
            continue
        for j in range(i + 1, n):
            if bboxes[j] is None:
                continue
            if _bboxes_near(bboxes[i], bboxes[j], pad):
                union(i, j)

    groups: dict = {}
    for i in range(n):
        if bboxes[i] is None:
            continue
        root = find(i)
        groups.setdefault(root, []).append(i)
    return list(groups.values())


def _draw_item_on_shape(shape, item, ox: float, oy: float) -> None:
    """Replay one PyMuPDF drawing item on a shape (offset into local coords)."""
    kind = item[0]
    if kind == "l":
        p1, p2 = item[1], item[2]
        shape.draw_line(
            fitz.Point(p1.x - ox, p1.y - oy),
            fitz.Point(p2.x - ox, p2.y - oy),
        )
    elif kind == "re":
        rect = item[1]
        shape.draw_rect(fitz.Rect(
            rect.x0 - ox, rect.y0 - oy, rect.x1 - ox, rect.y1 - oy,
        ))
    elif kind == "qu":
        quad = item[1]
        local = fitz.Quad(
            fitz.Point(quad.ul.x - ox, quad.ul.y - oy),
            fitz.Point(quad.ur.x - ox, quad.ur.y - oy),
            fitz.Point(quad.lr.x - ox, quad.lr.y - oy),
            fitz.Point(quad.ll.x - ox, quad.ll.y - oy),
        )
        shape.draw_quad(local)
    elif kind == "c":
        p1, c1, c2, p2 = item[1], item[2], item[3], item[4]
        shape.draw_bezier(
            fitz.Point(p1.x - ox, p1.y - oy),
            fitz.Point(c1.x - ox, c1.y - oy),
            fitz.Point(c2.x - ox, c2.y - oy),
            fitz.Point(p2.x - ox, p2.y - oy),
        )


def _render_drawings_to_png(
    drawings, indices, bbox, out_path: Path,
) -> None:
    """Rasterize selected vector drawings onto a white canvas."""
    pad = 4.0
    x0, y0, x1, y1 = bbox
    x0 -= pad
    y0 -= pad
    x1 += pad
    y1 += pad
    width = max(1.0, x1 - x0)
    height = max(1.0, y1 - y0)

    doc = fitz.open()
    try:
        page = doc.new_page(width=width, height=height)
        for idx in indices:
            drawing = drawings[idx]
            shape = page.new_shape()
            for item in drawing["items"]:
                _draw_item_on_shape(shape, item, x0, y0)
            color = drawing["stroke"] or drawing["fill"] or (0, 0, 0)
            shape.finish(
                color=color,
                width=drawing["width"],
                fill=drawing["fill"],
                fill_opacity=drawing["fill_opacity"],
                stroke_opacity=drawing["stroke_opacity"],
                closePath=False,
            )
            shape.commit()
        pix = page.get_pixmap(dpi=VECTOR_RASTER_DPI, alpha=False)
        pix.save(out_path)
    finally:
        doc.close()


def rasterize_vector_clusters(
    page_index: int,
    drawings,
    page_width: float,
    page_height: float,
    assets_dir: Path,
) -> list:
    """Cluster vector paths, render each cluster to PNG in assets/."""
    if not drawings:
        return []

    bboxes = [_drawing_bbox(d) for d in drawings]
    eligible = [
        i for i, bbox in enumerate(bboxes)
        if not _skip_drawing_for_raster(bbox, page_width, page_height)
    ]
    if not eligible:
        return []

    eligible_bboxes = [bboxes[i] for i in eligible]
    local_groups = _cluster_drawing_indices(eligible_bboxes, VECTOR_CLUSTER_MERGE_PAD)

    clusters = []
    for cluster_no, local_group in enumerate(local_groups):
        indices = [eligible[i] for i in local_group]
        cluster_bbox = bboxes[indices[0]]
        for idx in indices[1:]:
            cluster_bbox = _union_bbox(cluster_bbox, bboxes[idx])

        img_name = f"p{page_index}_vec{cluster_no}.png"
        out_path = assets_dir / img_name
        try:
            _render_drawings_to_png(drawings, indices, cluster_bbox, out_path)
        except Exception as exc:
            log_warning(f"  could not rasterize vector cluster {img_name}: {exc}")
            continue

        clusters.append({
            "file": img_name,
            "bbox": cluster_bbox,
            "drawing_indices": indices,
            "is_signature": False,
        })
    return clusters


# ----------------------------------------------------------------------------
# Inference device (GPU / Apple Silicon MPS / CPU)
# ----------------------------------------------------------------------------

def get_inference_device() -> str:
    """Resolve INFERENCE_DEVICE to a concrete PyTorch/Ollama backend string."""
    if INFERENCE_DEVICE == "cpu":
        return "cpu"
    import torch
    if torch.cuda.is_available():
        return "0"
    if getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
        return "mps"
    log_warning("INFERENCE_DEVICE='gpu' but no GPU found, falling back to CPU")
    return "cpu"


def describe_inference_device(device: str) -> str:
    if device == "cpu":
        return "CPU"
    if device == "mps":
        return "Apple GPU (MPS)"
    if device.isdigit() or device.startswith("cuda"):
        return f"CUDA GPU ({device})"
    return device


def ollama_inference_options() -> dict:
    """Ollama options; num_gpu=-1 offloads as many layers as possible to GPU."""
    options = {"temperature": 0.3, "num_ctx": 16384}
    if get_inference_device() == "cpu":
        options["num_gpu"] = 0
    else:
        options["num_gpu"] = -1
    return options


# ----------------------------------------------------------------------------
# Step 2: filter signatures with YOLO (Ultralytics)
# ----------------------------------------------------------------------------

def load_signature_model(model_path: Optional[Path] = None):
    """Load the YOLO signature detector (cached after first call).

    Uses tech4humans/yolov8s-signature-detector when available; falls back
    to Mels22/Signature-Detection-Verification if the gated repo is inaccessible.
    """
    global _signature_model
    if _signature_model is not None:
        return _signature_model

    from ultralytics import YOLO

    device = get_inference_device()

    if model_path and model_path.exists():
        _signature_model = YOLO(str(model_path))
        log_info(f"    YOLO device: {describe_inference_device(device)}")
        return _signature_model

    local_default = Path(__file__).resolve().parent / "models" / SIGNATURE_MODEL_FILE
    if local_default.exists():
        _signature_model = YOLO(str(local_default))
        log_info(f"    YOLO device: {describe_inference_device(device)}")
        return _signature_model

    from huggingface_hub import hf_hub_download

    candidates = [
        (SIGNATURE_MODEL_REPO, SIGNATURE_MODEL_FILE),
        (SIGNATURE_MODEL_FALLBACK_REPO, SIGNATURE_MODEL_FALLBACK_FILE),
    ]
    last_error = None
    for repo_id, filename in candidates:
        try:
            weights = hf_hub_download(repo_id=repo_id, filename=filename)
            if repo_id != SIGNATURE_MODEL_REPO:
                log_info(
                    f"  Note: using fallback signature model {repo_id}/{filename} "
                    f"(accept the license at huggingface.co/{SIGNATURE_MODEL_REPO} "
                    f"for the YOLOv8 model)"
                )
            _signature_model = YOLO(weights)
            log_info(f"    YOLO device: {describe_inference_device(device)}")
            return _signature_model
        except Exception as exc:
            last_error = exc
    raise RuntimeError(
        "Could not load a signature detection model. Either place "
        f"models/{SIGNATURE_MODEL_FILE} in the project directory, log in to "
        f"Hugging Face and accept the license for {SIGNATURE_MODEL_REPO}, or "
        "ensure network access for the fallback model download."
    ) from last_error


def detect_signature_boxes(image_path: Path, model, conf: float) -> list:
    """Return signature bounding boxes as (x0, y0, x1, y1) in pixel coords."""
    results = model(
        str(image_path), conf=conf, verbose=False, device=get_inference_device(),
    )
    boxes = []
    for box in results[0].boxes:
        x0, y0, x1, y1 = box.xyxy[0].tolist()
        boxes.append((x0, y0, x1, y1))
    return boxes


def image_contains_signature(image_path: Path, model, conf: float) -> bool:
    """True when YOLO finds at least one signature in the image."""
    return bool(detect_signature_boxes(image_path, model, conf))


def redact_signatures_on_scan(scan, assets_dir: Path, model, conf: float) -> int:
    """Cover signature regions on a scanned page background."""
    image_path = assets_dir / scan["file"]
    boxes = detect_signature_boxes(image_path, model, conf)
    if not boxes:
        return 0

    image = Image.open(image_path)
    # scan["scale"] converts PDF points -> image pixels; YOLO boxes are in pixels.
    px_to_pt = 1.0 / scan["scale"]
    covers = []
    for x0, y0, x1, y1 in boxes:
        pdf_bbox = (
            x0 * px_to_pt, y0 * px_to_pt, x1 * px_to_pt, y1 * px_to_pt,
        )
        # Sample paper color from a margin around the box, not from the ink itself.
        pad_px = max(4, int(0.05 * max(x1 - x0, y1 - y0)))
        bg = sample_background_color(image, (
            max(0, x0 - pad_px), max(0, y0 - pad_px),
            min(image.width, x1 + pad_px), min(image.height, y1 + pad_px),
        ))
        covers.append({"bbox": pdf_bbox, "bg": bg})
    scan["signature_covers"] = covers
    return len(covers)


def filter_signatures(
    pages,
    assets_dir: Path,
    model,
    conf: float,
) -> tuple[int, int, int, list[str]]:
    """Drop signature images/vectors and redact signatures on scan pages."""
    scan_files = {page["scan"]["file"] for page in pages if page["scan"]}
    embedded_files = {
        img["file"] for page in pages for img in page["images"]
    }
    vector_files = {
        cluster["file"]
        for page in pages
        for cluster in page.get("drawing_clusters", [])
    }
    files_to_check = sorted((embedded_files | vector_files) - scan_files)

    signature_files: set[str] = set()
    for img_name in files_to_check:
        path = assets_dir / img_name
        if not path.exists():
            continue
        if image_contains_signature(path, model, conf):
            signature_files.add(img_name)
            path.unlink(missing_ok=True)

    removed_placements = 0
    for page in pages:
        before = len(page["images"])
        page["images"] = [
            img for img in page["images"] if img["file"] not in signature_files
        ]
        removed_placements += before - len(page["images"])

    removed_vector_clusters = 0
    for page in pages:
        for cluster in page.get("drawing_clusters", []):
            if cluster["file"] not in signature_files:
                continue
            cluster["is_signature"] = True
            removed_vector_clusters += 1

    scan_redactions = 0
    for page in pages:
        if not page["scan"]:
            continue
        scan_redactions += redact_signatures_on_scan(
            page["scan"], assets_dir, model, conf,
        )

    return removed_placements, scan_redactions, removed_vector_clusters, sorted(signature_files)


# ----------------------------------------------------------------------------
# Step 3: ask Ollama for the replacement table
# ----------------------------------------------------------------------------

def collect_document_text(pages) -> str:
    """Concatenate all extracted (or OCR'd) text of the document."""
    lines = []
    for page in pages:
        if page["scan"]:
            for words in page["scan"]["lines"]:
                lines.append(" ".join(w["text"] for w in words))
        else:
            for line in group_spans_by_line(page["spans"]):
                lines.append(build_line_text(line))
    return "\n".join(lines)


def group_spans_by_line(spans, y_tolerance: float = 3.0) -> list:
    """Group spans into reading-order lines (similar baseline y)."""
    indexed = list(enumerate(spans))
    indexed.sort(
        key=lambda t: (round(t[1]["origin"][1] / y_tolerance), t[1]["origin"][0])
    )
    lines: list = []
    current: list = []
    current_y: Optional[float] = None
    for idx, span in indexed:
        y = span["origin"][1]
        if current_y is None or abs(y - current_y) <= y_tolerance:
            current.append((idx, span))
            if current_y is None:
                current_y = y
        else:
            current.sort(key=lambda t: t[1]["origin"][0])
            lines.append(current)
            current = [(idx, span)]
            current_y = y
    if current:
        current.sort(key=lambda t: t[1]["origin"][0])
        lines.append(current)
    return lines


def _span_x1(span) -> float:
    if "bbox" in span:
        return span["bbox"][2]
    x, _ = span["origin"]
    return x + len(span["text"]) * span["size"] * 0.5


def build_line_text(line) -> str:
    """Join spans on one line; insert a space when glyphs are visibly separated."""
    parts: list = []
    prev_x1: Optional[float] = None
    for _, span in line:
        x0 = span["origin"][0]
        if prev_x1 is not None and x0 - prev_x1 > span["size"] * 0.15:
            parts.append(" ")
        parts.append(span["text"])
        prev_x1 = _span_x1(span)
    return "".join(parts)


def build_line_text_with_offsets(line) -> tuple:
    """Return (line text, list of (char_start, char_end, flat_index))."""
    parts: list = []
    offsets: list = []
    prev_x1: Optional[float] = None
    pos = 0
    for flat_i, (_, span) in enumerate(line):
        x0 = span["origin"][0]
        if prev_x1 is not None and x0 - prev_x1 > span["size"] * 0.15:
            parts.append(" ")
            pos += 1
        start = pos
        parts.append(span["text"])
        pos += len(span["text"])
        offsets.append((start, pos, flat_i))
        prev_x1 = _span_x1(span)
    return "".join(parts), offsets


def _span_union_bbox(spans) -> tuple:
    bboxes = [s["bbox"] if "bbox" in s else None for _, s in spans]
    bboxes = [b for b in bboxes if b]
    if not bboxes:
        x, y = spans[0][1]["origin"]
        size = spans[0][1]["size"]
        w = sum(len(s[1]["text"]) for s in spans) * size * 0.5
        return (x, y - size * 0.8, x + w, y + size * 0.2)
    return (
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    )


def _build_text_page_patch(line, flat_indices, replacement_text) -> dict:
    group = [line[i] for i in flat_indices]
    x0, y0, x1, y1 = _span_union_bbox(group)
    first_span = group[0][1]
    return {
        "text": replacement_text,
        "bbox": (x0, y0, x1, y1),
        "baseline": first_span["origin"][1],
        "size": first_span["size"],
        "bg": (255, 255, 255),
    }


def _contiguous_groups(sorted_indices: list) -> list:
    if not sorted_indices:
        return []
    groups, current = [], [sorted_indices[0]]
    for idx in sorted_indices[1:]:
        if idx == current[-1] + 1:
            current.append(idx)
        else:
            groups.append(current)
            current = [idx]
    groups.append(current)
    return groups


def apply_mapping_text_page(page, ordered) -> int:
    """Apply replacements on text pages (matches phrases split across spans)."""
    spans = page["spans"]
    hidden = page.setdefault("hidden_span_indices", set())
    patches = page.setdefault("patches", [])
    replaced_count = 0

    for line in group_spans_by_line(spans):
        line_text, offsets = build_line_text_with_offsets(line)
        if not line_text:
            continue

        hit_flat: set = set()
        for original, _ in ordered:
            start = 0
            while True:
                idx = line_text.find(original, start)
                if idx == -1:
                    break
                end = idx + len(original)
                hit_flat.update(
                    flat_i
                    for s, e, flat_i in offsets
                    if s < end and e > idx
                )
                start = end

        if not hit_flat:
            continue

        for group in _contiguous_groups(sorted(hit_flat)):
            orig_text = "".join(
                line[fi][1]["text"] for fi in group
            )
            # Reconstruct the matched substring from line_text when possible.
            starts = [offsets[fi][0] for fi in group]
            ends = [offsets[fi][1] for fi in group]
            segment = line_text[min(starts):max(ends)]

            new_text = segment
            for original, replacement in ordered:
                new_text = new_text.replace(original, replacement)
            if new_text == segment:
                new_text = orig_text
                for original, replacement in ordered:
                    new_text = new_text.replace(original, replacement)
            if new_text == segment and new_text == orig_text:
                continue

            patches.append(_build_text_page_patch(line, group, new_text))
            for fi in group:
                hidden.add(line[fi][0])
            replaced_count += 1

    for i, span in enumerate(spans):
        if i in hidden:
            continue
        new_text = span["text"]
        for original, replacement in ordered:
            if original in new_text:
                new_text = new_text.replace(original, replacement)
        if new_text != span["text"]:
            span["text"] = new_text
            replaced_count += 1

    return replaced_count


def ask_ollama_for_mapping(text: str, model: str, ollama_url: str) -> dict:
    """Query the local model (in chunks if needed) for the replacement table."""
    chunks = [text[i:i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)]
    mapping: dict = {}
    device = get_inference_device()
    ollama_options = ollama_inference_options()
    if device == "cpu":
        log_info("    Ollama: CPU-only (num_gpu=0)")
    else:
        log_info(f"    Ollama: GPU ({describe_inference_device(device)}, num_gpu=-1)")

    system_prompt = load_ollama_system_prompt()

    for i, chunk in enumerate(chunks, 1):
        log_info(f"  Querying model '{model}' (part {i}/{len(chunks)}) ...")
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
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "format": "json",
                        "stream": False,
                        "options": ollama_options,
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
                log_warning(f"  Attempt {attempt + 1} failed: {exc}")
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


def build_scan_patch(scan, image, page_width, words, word_indices, text, orig_text,
                     bg_override=None):
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
    if bg_override is not None:
        bg = bg_override
    else:
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

    When scan["ocr_only"] is True, every OCR word is re-rendered on a white
    page (no scan background) so original image text cannot show through.
    """
    flat = []  # (line index, word index within line, word)
    for li, words in enumerate(scan["lines"]):
        for wi, w in enumerate(words):
            flat.append((li, wi, w))

    if not flat:
        scan["patches"] = []
        return 0

    offsets, pos = [], 0
    for _, _, w in flat:
        offsets.append((pos, pos + len(w["text"])))
        pos += len(w["text"]) + 1
    page_text = " ".join(w["text"] for _, _, w in flat)

    ocr_only = scan.get("ocr_only", False)
    white = (255, 255, 255)
    bg_override = white if ocr_only else None

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
    handled = set()

    if hit_words:
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
                    word_indices, seg_text, orig_text, bg_override=bg_override,
                ))
            handled.update(group)

    if ocr_only:
        for i, (li, wi, w) in enumerate(flat):
            if i in handled:
                continue
            patches.append(build_scan_patch(
                scan, image, page_width, scan["lines"][li],
                [wi], w["text"], w["text"], bg_override=bg_override,
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
        replaced_count += apply_mapping_text_page(page, ordered)
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


def fix_ocr_chars(text: str) -> str:
    """Fix common OCR character misreads in LaTeX-bound text."""
    for wrong, right in OCR_CHAR_FIXES.items():
        text = text.replace(wrong, right)
    return text


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


def patch_to_tikz(patch) -> list:
    """Render one white-out patch plus replacement text."""
    parts = []
    x0, y0, x1, y1 = patch["bbox"]
    r, g, b = patch["bg"]
    pad = 1.5
    parts.append(
        f"\\fill[fill={{rgb,255:red,{r};green,{g};blue,{b}}}] "
        f"({x0 - pad:.2f},{y0 - pad:.2f}) rectangle "
        f"({x1 + pad:.2f},{y1 + pad:.2f});"
    )
    if not patch["text"]:
        return parts
    size = patch["size"]
    text = tex_escape(patch["text"])
    parts.append(
        f"\\node[anchor=base west, inner sep=0, text depth=0pt, "
        f"text=black] at ({x0:.2f},{patch['baseline']:.2f}) "
        f"{{\\fontsize{{{size:.2f}bp}}{{{size * 1.2:.2f}bp}}"
        f"\\selectfont\\sffamily {text}}};"
    )
    return parts


def scan_page_to_tikz(page) -> list:
    """Scanned page: OCR text layers, optionally on top of the scan image."""
    scan = page["scan"]
    ocr_only = scan.get("ocr_only", False)
    if ocr_only:
        parts = [
            f"\\fill[fill={{rgb,255:red,255;green,255;blue,255}}] "
            f"(0,0) rectangle ({page['width']:.2f},{page['height']:.2f});"
        ]
    else:
        parts = [
            f"\\node[anchor=north west, inner sep=0] at (0,0) "
            f"{{\\includegraphics[width={page['width']:.2f}bp,"
            f"height={page['height']:.2f}bp]{{assets/{scan['file']}}}}};"
        ]
        for cover in scan.get("signature_covers", []):
            x0, y0, x1, y1 = cover["bbox"]
            r, g, b = cover["bg"]
            pad = 2.0
            parts.append(
                f"\\fill[fill={{rgb,255:red,{r};green,{g};blue,{b}}}] "
                f"({x0 - pad:.2f},{y0 - pad:.2f}) rectangle "
                f"({x1 + pad:.2f},{y1 + pad:.2f});"
            )
    for patch in scan.get("patches", []):
        parts.extend(patch_to_tikz(patch))
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
            signature_drawing_indices = {
                idx
                for cluster in page.get("drawing_clusters", [])
                if cluster.get("is_signature")
                for idx in cluster["drawing_indices"]
            }
            for i, drawing in enumerate(page["drawings"]):
                if i in signature_drawing_indices:
                    continue
                parts.extend(drawing_to_tikz(drawing))
            for cluster in page.get("drawing_clusters", []):
                if not cluster.get("is_signature"):
                    continue
                x0, y0, x1, y1 = cluster["bbox"]
                pad = 2.0
                parts.append(
                    f"\\fill[fill={{rgb,255:red,255;green,255;blue,255}}] "
                    f"({x0 - pad:.2f},{y0 - pad:.2f}) rectangle "
                    f"({x1 + pad:.2f},{y1 + pad:.2f});"
                )
            hidden = page.get("hidden_span_indices", set())
            for patch in page.get("patches", []):
                parts.extend(patch_to_tikz(patch))
            for si, span in enumerate(page["spans"]):
                if si in hidden:
                    continue
                parts.append(span_to_tikz(span))
        parts.append(r"\end{tikzpicture}")
        parts.append(r"\phantom{x}")  # page must not be empty
        if i < len(pages) - 1:
            parts.append(r"\clearpage")

    parts.append(r"\end{document}")
    return fix_ocr_chars("\n".join(parts))


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

    log_info(f"  Compiling with: {cmd[0]}")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=workdir)
    if result.returncode != 0 or not pdf_path.exists():
        log = (result.stdout + result.stderr)[-3000:]
        raise RuntimeError(f"LaTeX compilation failed:\n{log}")
    return pdf_path


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------

def anonymize(pdf_path: Path, outdir: Path, model: str, ollama_url: str,
              use_llm: bool = True, use_signature_filter: bool = True,
              signature_conf: float = SIGNATURE_CONF_DEFAULT,
              signature_model: Optional[Path] = None,
              batch_label: Optional[str] = None) -> Path:
    """Run the full pipeline for one PDF; return the anonymized PDF path."""
    outdir.mkdir(parents=True, exist_ok=True)
    assets_dir = outdir / "assets"
    stem = pdf_path.stem

    setup_run_logger(
        outdir, stem, pdf_path,
        model=model,
        ollama_url=ollama_url,
        use_llm=use_llm,
        use_signature_filter=use_signature_filter,
        signature_conf=signature_conf,
        inference_device=INFERENCE_DEVICE,
        signature_model=signature_model or "(HF default)",
    )
    if batch_label:
        log_info(batch_label)

    try:
        log_info("1/5 Extracting PDF content ...")
        pages = extract_pdf(pdf_path, assets_dir)
        n_spans = sum(len(p["spans"]) for p in pages)
        n_imgs = sum(len(p["images"]) for p in pages)
        n_vec = sum(len(p.get("drawing_clusters", [])) for p in pages)
        n_scans = sum(1 for p in pages if p["scan"])
        log_info(f"    {len(pages)} pages ({n_scans} scanned), "
                 f"{n_spans} text elements, {n_imgs} images, {n_vec} vector PNGs")

        if use_signature_filter:
            log_info("2/5 Filtering signatures (YOLO) ...")
            sig_model = load_signature_model(signature_model)
            removed, redacted, removed_vec, sig_files = filter_signatures(
                pages, assets_dir, sig_model, signature_conf,
            )
            if sig_files:
                log_info(f"    signature asset(s) removed: {', '.join(sig_files)}")
            if removed:
                log_info(f"    dropped {removed} embedded image placement(s)")
            if removed_vec:
                log_info(f"    redacted {removed_vec} vector signature cluster(s)")
            if redacted:
                log_info(f"    redacted {redacted} signature region(s) on scan pages")
            if not sig_files and not redacted and not removed_vec:
                log_info("    no signatures detected")
        else:
            log_info("2/5 Skipped (--no-signature-filter)")

        mapping: dict = {}
        if not use_llm:
            log_info("3/5 Skipped (--no-llm)")
        else:
            log_info("3/5 Detecting sensitive data via Ollama ...")
            text = collect_document_text(pages)
            mapping = ask_ollama_for_mapping(text, model, ollama_url)
            log_info(f"    {len(mapping)} replacements found:")
            for original, replacement in mapping.items():
                log_info(f"      {original!r} -> {replacement!r}")
            mapping_path = outdir / f"{stem}_mapping.json"
            mapping_path.write_text(
                json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        scan_ocr_only = any(
            p.get("scan", {}).get("ocr_only") for p in pages if p.get("scan")
        )
        if use_llm or scan_ocr_only:
            changed = apply_mapping(pages, mapping, assets_dir)
            if use_llm:
                log_info(f"    {changed} text elements changed "
                         f"(table saved to {mapping_path})")
            elif changed:
                log_info(f"    rendered {changed} OCR text layer(s) (no scan background)")

        log_info("4/5 Generating LaTeX ...")
        tex_path = outdir / f"{stem}_anonymized.tex"
        tex_path.write_text(build_latex(pages), encoding="utf-8")
        log_info(f"    {tex_path}")

        log_info("5/5 Compiling PDF ...")
        pdf_out = compile_latex(tex_path)
        log_info(f"    {pdf_out}")
        log_info(f"Finished OK: {pdf_out}")
        return pdf_out
    except Exception as exc:
        log_error(f"FAILED: {exc}")
        raise
    finally:
        teardown_run_logger()


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
    parser.add_argument("--no-signature-filter", action="store_true",
                        help="do not remove/redact detected signatures")
    parser.add_argument("--signature-conf", type=float,
                        default=SIGNATURE_CONF_DEFAULT,
                        help=f"YOLO confidence threshold "
                             f"(default: {SIGNATURE_CONF_DEFAULT})")
    parser.add_argument("--signature-model", type=Path, default=None,
                        help="path to a local YOLO .pt weights file "
                             f"(default: models/{SIGNATURE_MODEL_FILE} or HF download)")
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
        batch_label = f"=== [{i}/{len(pdf_files)}] {pdf_path.name} -> {outdir} ==="
        print(f"\n{batch_label}")
        try:
            pdf_out = anonymize(
                pdf_path, outdir, args.model, args.ollama_url,
                use_llm=not args.no_llm,
                use_signature_filter=not args.no_signature_filter,
                signature_conf=args.signature_conf,
                signature_model=args.signature_model,
                batch_label=batch_label,
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
