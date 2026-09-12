# Historical Files — No Longer In Use

> **Screenshotting is now done manually by a human**, so automated smart-crop screenshotting is deprecated. Files in this folder are retained only for reference and must not be imported by active code.

## Instructions — Read Before Adding Any File

1. **Do not delete** — move (copy) the file here, do not re-add active imports.
2. **Add a description entry below** for every file you put in this folder. Use the format:
   ```
   ### <filename>
   - **Original path:** `<original path>`
   - **Date archived:** YYYY-MM-DD
   - **Purpose (what it used to do):** 1–2 sentence summary of its role.
   - **Why archived:** Reason it is no longer needed (e.g. replaced by human screenshotting).
   - **Dependencies / callers that were updated:** List files that previously imported it.
   ```
3. Keep this README as the single source of truth for deprecated screenshotting code.
4. If you are unsure whether a file belongs here, ask before moving it.

---

## Archived Files

### extraction_chatter_ocr_engine.py
- **Original path:** `extraction_chatter/backend/ocr_engine.py`
- **Date archived:** 2026-09-10
- **Purpose:** Provided `OCREngine` with `render_pdf_pages_as_images`, `extract_from_pdf`, `segment_questions`, and `smart_crop_diagram` (AI bounding-box hint + OpenCV/Azure fallback). Used to rasterize PDFs and auto-crop question/choice diagrams into `OUTPUT_DIR/diagrams`.
- **Why archived:** Auto smart-crop is no longer needed — human now provides screenshots directly. All AI/contour cropping logic is deprecated.
- **Callers updated:** `extraction_chatter/app.py` (removed smart-crop loop), `unified_app/app.py`.

### diagram_finder_ocr_engine.py
- **Original path:** `diagram_finder/backend/ocr_engine.py`
- **Date archived:** 2026-09-10
- **Purpose:** Duplicate of the OCREngine above for the standalone `diagram_finder` service. Same rendering and smart-crop responsibilities isolated to that app.
- **Why archived:** Same reason — human screenshotting replaces automated cropping. Duplicate logic retired.
- **Callers updated:** `diagram_finder/app.py`.

### diagram_finder.py
- **Original path:** `diagram_finder/backend/diagram_finder.py`
- **Date archived:** 2026-09-10
- **Purpose:** Heuristic diagram detection / finder that scanned rendered page images to locate likely diagram regions and feed them to `smart_crop_diagram`.
- **Why archived:** Detection step is obsolete when a human selects the screenshot region manually.
- **Callers updated:** `diagram_finder/app.py`, `diagram_finder/run_finder.py`.

### run_bbox.py
- **Original path:** `extraction_chatter/run_bbox.py`
- **Date archived:** 2026-09-10
- **Purpose:** CLI helper to run the bounding-box pipeline stand-alone (render → detect bbox → crop) for local debugging of smart-crop.
- **Why archived:** Debugging harness for the now-removed auto-crop path; no longer exercised.
- **Callers updated:** None (stand-alone script).

### run_batch.py
- **Original path:** `extraction_chatter/run_batch.py`
- **Date archived:** 2026-09-10
- **Purpose:** Batch runner that drove the OCREngine + AI provider pipeline over a folder of PDFs, invoking smart-crop per block.
- **Why archived:** Superseded by human-screenshot workflow; batch path that auto-cropped is deprecated.
- **Callers updated:** `extraction_chatter/app.py` (job worker).

### run_finder.py
- **Original path:** `diagram_finder/run_finder.py`
- **Date archived:** 2026-09-10
- **Purpose:** Entry point for the `diagram_finder` service that executed render → find → crop loop from the command line.
- **Why archived:** Finder loop unnecessary under manual screenshotting.
- **Callers updated:** `diagram_finder/app.py`.

### diagram_finder/ (entire folder)
- **Original path:** `diagram_finder/` (all remaining `app.py`, `backend/*`, `static/*`, `tests/*`, etc.)
- **Date archived:** 2026-09-10
- **Purpose:** Stand-alone service for automated diagram detection and smart cropping. Provided its own Flask app, batch scheduler, and frontend to isolate visual QA from `extraction_chatter`.
- **Why archived:** Not used for human screenshotting. All active screenshotting now handled by `extraction_chatter/app.py` human upload endpoints (`/diagram-review/crop`). Folder moved wholesale to historical for reference.
- **Callers updated:** None — no active imports remain; references removed from docs.
