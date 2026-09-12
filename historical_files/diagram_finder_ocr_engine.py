"""
backend/ocr_engine.py
Handles splitting PDFs into question images / text blocks, extracting diagrams
with Azure Computer Vision Smart Crop, and segmenting questions.
"""

import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("extraction.ocr")


def _load_env_file():
    """Lightweight loader for .env in extraction_chatter directory."""
    env_path = Path(__file__).parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


_load_env_file()


class OCREngine:
    """PDF Text & Image OCR Extractor with Azure Smart Crop."""

    def __init__(self, azure_endpoint: Optional[str] = None, azure_key: Optional[str] = None):
        _load_env_file()
        self.endpoint = azure_endpoint or os.environ.get("AZURE_CV_ENDPOINT", "").rstrip("/")
        self.key = azure_key or os.environ.get("AZURE_CV_KEY", "")


    def smart_crop_diagram(
        self,
        image_path: Path,
        output_dir: Path,
        question_id: str,
        hint: Optional[str] = None,
        part: str = "diagram",
    ) -> Optional[Dict[str, Any]]:
        """
        Crop diagrams, charts, or geometric figures from a question or choice image.
        Supports:
        1. AI bounding box hints [ymin, xmin, ymax, xmax] (normalized 0-1000 scale).
        2. Azure Computer Vision Image Analysis (objects / smart crop).
        3. Local contour / non-text drawing bounding box detection.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        suffix = f"_{part}.png" if not part.endswith(".png") else f"_{part}"
        cropped_filename = f"{question_id}{suffix}"
        cropped_path = output_dir / cropped_filename

        from PIL import Image, ImageChops, ImageFilter
        if not image_path.exists():
            return None

        # 1. Check if hint contains explicit bounding box coordinates [ymin, xmin, ymax, xmax]
        if hint:
            bbox_coords = re.findall(r"\[\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*\]", hint)
            if bbox_coords:
                try:
                    ymin_r, xmin_r, ymax_r, xmax_r = [int(v) for v in bbox_coords[0]]
                    with Image.open(image_path) as img:
                        w, h = img.size
                        # If scale is 0-1000 normalized
                        if max(ymin_r, xmin_r, ymax_r, xmax_r) <= 1000:
                            ymin = int((ymin_r / 1000.0) * h)
                            xmin = int((xmin_r / 1000.0) * w)
                            ymax = int((ymax_r / 1000.0) * h)
                            xmax = int((xmax_r / 1000.0) * w)
                        else:
                            ymin, xmin, ymax, xmax = ymin_r, xmin_r, ymax_r, xmax_r

                        xmin = max(0, min(xmin, w - 10))
                        ymin = max(0, min(ymin, h - 10))
                        xmax = min(w, max(xmax, xmin + 10))
                        ymax = min(h, max(ymax, ymin + 10))
                        roi = img.crop((xmin, ymin, xmax, ymax))
                        # Hybrid: tighten ROI with local OpenCV contours (no Azure cost)
                        try:
                            import cv2
                            import numpy as np
                            cv_roi = cv2.cvtColor(np.array(roi), cv2.COLOR_RGB2GRAY)
                            _, th = cv2.threshold(cv_roi, 200, 255, cv2.THRESH_BINARY_INV)
                            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
                            th = cv2.dilate(th, kernel, iterations=1)
                            contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                            candidates = []
                            roi_area = roi.size[0] * roi.size[1]
                            for cnt in contours:
                                x, y, cw, ch = cv2.boundingRect(cnt)
                                area = cw * ch
                                if area < roi_area * 0.03 or area > roi_area * 0.95:
                                    continue
                                if cw < 20 or ch < 20:
                                    continue
                                candidates.append((area, (x, y, x + cw, y + ch)))
                            if candidates:
                                candidates.sort(reverse=True)
                                tx1, ty1, tx2, ty2 = candidates[0][1]
                                pad = 6
                                tx1 = max(0, tx1 - pad); ty1 = max(0, ty1 - pad)
                                tx2 = min(roi.size[0], tx2 + pad); ty2 = min(roi.size[1], ty2 + pad)
                                tight = roi.crop((tx1, ty1, tx2, ty2))
                                tight.save(cropped_path, format="PNG")
                                log.info("Hybrid bbox+OpenCV tight crop: %s ROI %s -> %s", cropped_filename, [xmin,ymin,xmax,ymax], [xmin+tx1, ymin+ty1, xmin+tx2, ymin+ty2])
                                return {"diagram_file": cropped_filename, "diagram_path": str(cropped_path), "bounding_box": [xmin+tx1, ymin+ty1, tx2-tx1, ty2-ty1], "method": "hybrid_bbox_opencv"}
                        except Exception as he:
                            log.warning("Hybrid tighten failed: %s", he)
                        roi.save(cropped_path, format="PNG")
                        log.info("Cropped diagram via AI bounding box hint (no tighten): %s", cropped_filename)
                        return {"diagram_file": cropped_filename, "diagram_path": str(cropped_path), "bounding_box": [xmin, ymin, xmax - xmin, ymax - ymin], "method": "ai_bounding_box_hint"}
                except Exception as e:
                    log.warning("Failed parsing bounding box from hint '%s': %s", hint, e)

        # 2. If Azure endpoint and key are configured, use Azure Computer Vision API
        if self.endpoint and self.key:
            try:
                import httpx
                headers = {
                    "Ocp-Apim-Subscription-Key": self.key,
                    "Content-Type": "application/octet-stream",
                }
                url = f"{self.endpoint}/computervision/imageanalysis:analyze?api-version=2023-10-01&features=smartCrops,objects"
                image_data = image_path.read_bytes()

                with httpx.Client(timeout=30.0) as client:
                    resp = client.post(url, headers=headers, content=image_data)
                    if resp.status_code == 200:
                        analysis = resp.json()
                        smart_crops = analysis.get("smartCropsResult", {}).get("values", [])
                        objects = analysis.get("objectsResult", {}).get("values", [])

                        bbox = None
                        if objects:
                            obj = objects[0].get("boundingBox", {})
                            bbox = (obj.get("x", 0), obj.get("y", 0), obj.get("w", 0), obj.get("h", 0))
                        elif smart_crops:
                            crop = smart_crops[0].get("boundingBox", {})
                            bbox = (crop.get("x", 0), crop.get("y", 0), crop.get("w", 0), crop.get("h", 0))

                        if bbox and bbox[2] > 20 and bbox[3] > 20:
                            with Image.open(image_path) as img:
                                x, y, w_box, h_box = bbox
                                # Only crop if bounding box is not the entire page (>95% page area)
                                if w_box < img.width * 0.95 or h_box < img.height * 0.95:
                                    cropped = img.crop((x, y, x + w_box, y + h_box))
                                    cropped.save(cropped_path, format="PNG")
                                    log.info("Azure Smart Crop extracted diagram to %s", cropped_filename)
                                    return {
                                        "diagram_file": cropped_filename,
                                        "diagram_path": str(cropped_path),
                                        "bounding_box": bbox,
                                        "method": "azure_smart_crop",
                                    }
            except Exception as e:
                log.warning("Azure Smart Crop API call failed: %s.", e)

        # 3. Local Smart Contour / High-Contrast Region Crop (avoids whole-page dumps)
        try:
            with Image.open(image_path) as img:
                w, h = img.size
                try:
                    import cv2
                    import numpy as np
                    cv_img = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
                    _, thresh = cv2.threshold(cv_img, 200, 255, cv2.THRESH_BINARY_INV)
                    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
                    dilated = cv2.dilate(thresh, kernel, iterations=2)
                    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
                    candidates = []
                    page_area = w * h
                    for cnt in contours:
                        x, y, cw, ch = cv2.boundingRect(cnt)
                        area = cw * ch
                        if area < page_area * 0.02 or area > page_area * 0.85:
                            continue
                        if cw < 40 or ch < 40:
                            continue
                        aspect = max(cw, ch) / max(1, min(cw, ch))
                        if aspect > 10:
                            continue
                        candidates.append((area, (x, y, x + cw, y + ch)))
                    if candidates:
                        candidates.sort(reverse=True)
                        bx1, by1, bx2, by2 = candidates[0][1]
                        pad = 12
                        crop_box = (max(0, bx1 - pad), max(0, by1 - pad), min(w, bx2 + pad), min(h, by2 + pad))
                        cropped = img.crop(crop_box)
                        cropped.save(cropped_path, format="PNG")
                        log.info("Local OpenCV contour crop: %s", cropped_filename)
                        return {"diagram_file": cropped_filename, "diagram_path": str(cropped_path), "bounding_box": list(crop_box), "method": "local_opencv_contour"}
                except Exception as cv_e:
                    log.warning("OpenCV contour crop failed: %s", cv_e)
                gray = img.convert("L")
                inv = ImageChops.invert(gray)
                bg = Image.new(inv.mode, inv.size, inv.getpixel((0, 0)))
                diff = ImageChops.difference(inv, bg)
                bbox = diff.getbbox()
                if bbox:
                    bx1, by1, bx2, by2 = bbox
                    # Reject whole-page bbox (text covers >90% area) -> fallback to center 60% crop using hint
                    bw = bx2 - bx1
                    bh = by2 - by1
                    if bw > w * 0.9 and bh > h * 0.9:
                        cx, cy = w // 2, h // 2
                        crop_box = (max(0, cx - w//3), max(0, cy - h//3), min(w, cx + w//3), min(h, cy + h//3))
                        log.info("Whole-page bbox rejected, using center crop for hint: %s", hint)
                    else:
                        pad = 10
                        crop_box = (max(0, bx1 - pad), max(0, by1 - pad), min(w, bx2 + pad), min(h, by2 + pad))
                    cropped = img.crop(crop_box)
                    cropped.save(cropped_path, format="PNG")
                    return {"diagram_file": cropped_filename, "diagram_path": str(cropped_path), "bounding_box": list(crop_box), "method": "local_contour_crop"}
                else:
                    img.save(cropped_path, format="PNG")
                    return {"diagram_file": cropped_filename, "diagram_path": str(cropped_path), "method": "local_fallback_copy"}
        except Exception as e:
            log.warning("Local diagram crop failed: %s", e)
            return None

    def render_pdf_pages_as_images(self, pdf_path: str, output_dir: Path) -> List[Path]:
        """
        Convert every page of a PDF into high-res PNG images using PyMuPDF.
        This provides the exact image inputs required by Azure Smart Crop.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        image_paths = []
        p = Path(pdf_path)

        try:
            import fitz  # PyMuPDF
            doc = fitz.open(str(p))
            for i, page in enumerate(doc):
                pix = page.get_pixmap(dpi=200)
                page_img_path = output_dir / f"{p.stem}_page_{i+1}.png"
                pix.save(str(page_img_path))
                image_paths.append(page_img_path)
            log.info("Rendered %d page image(s) from PDF %s", len(image_paths), p.name)
        except Exception as e:
            log.warning("PyMuPDF page rendering failed: %s", e)

        return image_paths

    def extract_from_pdf(self, pdf_path: str, diagrams_dir: Optional[Path] = None) -> List[Dict[str, Any]]:
        """
        Extract raw text and diagrams from a PDF file.
        Renders pages to images first so Azure Smart Crop receives image inputs.
        """
        p = Path(pdf_path)
        if not p.exists():
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")

        extracted_text = ""
        # 1. Render pages as images for Smart Crop
        temp_pages_dir = p.parent / "temp_pages"
        page_images = self.render_pdf_pages_as_images(str(p), temp_pages_dir)

        # 2. Extract text per page
        try:
            import fitz
            doc = fitz.open(str(p))
            for i, page in enumerate(doc):
                text = page.get_text() or ""
                extracted_text += f"\n--- PAGE {i+1} ---\n" + text
        except Exception:
            raw_bytes = p.read_bytes()
            extracted_text = raw_bytes.decode("utf-8", errors="ignore")

        # 3. Segment raw text into individual question blocks and associate exact page images
        blocks = self.segment_questions(extracted_text, filename=p.name)
        
        # Attach the corresponding page image to each block based on page markers
        if page_images:
            for b in blocks:
                ocr_t = b.get("ocr_text", "")
                page_match = re.search(r"--- PAGE (\d+) ---", ocr_t)
                page_idx = 0
                if page_match:
                    p_num = int(page_match.group(1))
                    if 1 <= p_num <= len(page_images):
                        page_idx = p_num - 1
                b["image_path"] = str(page_images[page_idx])
                b["source_page_number"] = page_idx + 1

        return blocks


    def segment_questions(self, full_text: str, filename: str = "") -> List[Dict[str, Any]]:
        """
        Split a block of text into distinct questions using regex delimiters.
        """
        q_pattern = re.compile(
            r"(?:\n|^)(?:Question\s+|Q)?(\d+)[\.\)]\s+", re.IGNORECASE
        )

        matches = list(q_pattern.finditer(full_text))
        if not matches:
            paras = [p.strip() for p in full_text.split("\n\n") if p.strip()]
            return [
                {
                    "question_number": i + 1,
                    "source_file": filename,
                    "ocr_text": para,
                    "has_diagram": "diagram" in para.lower() or "figure" in para.lower() or "graph" in para.lower(),
                }
                for i, para in enumerate(paras)
            ]

        blocks = []
        for i, match in enumerate(matches):
            q_num = match.group(1)
            start_pos = match.start()
            end_pos = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
            chunk = full_text[start_pos:end_pos].strip()

            has_diag = "diagram" in chunk.lower() or "figure" in chunk.lower() or "graph" in chunk.lower()

            blocks.append({
                "question_number": int(q_num),
                "source_file": filename,
                "ocr_text": chunk,
                "has_diagram": has_diag,
            })

        return blocks

