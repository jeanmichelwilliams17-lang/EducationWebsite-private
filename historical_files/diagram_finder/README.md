# Diagram Finder (`diagram_finder`)

> **NotebookLM-Driven Diagram Extraction, Hint-Named Smart Crop & Hybrid OpenCV Tightening**

---

## ⚠️ Maintenance & Flowchart Synchronization Rule

> [!IMPORTANT]
> **MANDATORY FLOWCHART UPDATE POLICY**:
> Whenever any feature, prompt, schema field, AI provider, or pipeline step in `diagram_finder` is added, modified, or deleted, the Mermaid flowchart in [`flowchart.md`](./flowchart.md) **MUST be updated immediately** to stay synchronized with the codebase.

---

## Overview

`diagram_finder` is a standalone sister tool to `extraction_chatter`, focused purely on **diagram discovery and cropping**. It reuses the same NotebookLM multi-account pool and Azure/local hybrid crop engine, but its sole output is **tightly-cropped, hint-named diagram assets**.

It takes a PDF exam paper, asks NotebookLM (Studio `generate_report` + `output.txt` + bbox) for every `Question Diagram Present:true` with `Page + Hint + [ymin,xmin,ymax,xmax] 0-1000`, then crops each diagram via **NotebookLM bbox → OpenCV ROI tighten** (zero Azure credit) and saves with NotebookLM-derived names.

---

## Key Pipeline Features

1. **NotebookLM Multi-Account Pool & Auto-Sync (`backend/notebooklm_client.py`)**:
   - Reused from `extraction_chatter` — 10-account pool (`Slave 1`..`Slave 10`), `batch_login_profiles()`, rate-limit cooldown, hot-swap.
   - Whole-doc Studio generation (`generate_report` → `wait_for_completion` → `download_report`) with `output.txt` bbox hints.

2. **Hint-Named Hybrid Smart Crop (`backend/ocr_engine.py`)**:
   - **Step 1 — AI bbox hint**: Parse `[ymin,xmin,ymax,xmax]` 0-1000 from `diagram_info[].hint` (NotebookLM vision after reading page image).
   - **Step 2 — Hybrid tighten**: Crop to bbox ROI, then run local OpenCV `threshold` + `findContours` inside ROI to shave whitespace (no Azure cost).
   - **Fallback**: Azure CV `smartCrops/objects` if `AZURE_CV_KEY` set, else center 60% crop.
   - Output: `output/diagrams/Q{N}_{part}_{slug}.png` where `slug` is derived from hint (e.g. `Q10_question_venn-diagram.png`).

3. **Bbox-Enriched Tilde Extraction (`backend/ai_provider.py` + `prompts/whole_pdf_extraction_tilde.txt`)**:
   - Same tilde schema as `extraction_chatter` but `Question Diagram Hint` now requires `"<Desc> [ymin,xmin,ymax,xmax]"`.
   - `chunk_size=1000` triggers whole-doc Studio `output.txt` (61 lines, 56k chars tested).

4. **Deterministic Naming & Validation**:
   - `label_path` + `question_number` + `hint slug` + `page` → filesystem-safe names.
   - `validator.py` KaTeX whitelist not needed for images, but `diagram_info.present` cross-checked against crop success.

---

## Directory Structure

```
diagram_finder/
├── README.md                      # This documentation
├── flowchart.md                   # Live Mermaid pipeline flowchart (Must be kept synced)
├── requirements.txt               # Shared with extraction_chatter (PyMuPDF, Pillow, Flask, httpx, opencv-python)
├── app.py                         # Flask Web UI: PDF upload → NotebookLM bbox → Hybrid crop → ZIP download
├── run_finder.py                  # Headless CLI batch runner
├── backend/
│   ├── ai_provider.py             # NotebookLM bbox-enriched extraction (whole-doc Studio)
│   ├── ocr_engine.py              # Hybrid bbox+OpenCV crop engine
│   ├── notebooklm_client.py       # Pool & session manager (reused)
│   ├── batch_scheduler.py         # Cooldown & hot-swap (reused)
│   ├── diagram_finder.py          # Core orchestration: extract → crop → name
│   └── validator.py               # (reused, unused for pure images)
├── prompts/
│   ├── whole_pdf_extraction_tilde.txt
│   └── diagram_description.txt
├── templates/
│   └── index.html                 # Dark-mode UI (ported from extraction_chatter + failbetterscreenshoter)
├── input_pdfs/                    # Upload directory
└── output/
    └── diagrams/                  # Cropped, hint-named PNGs
```

---

## How to Run

### 1. Launch Web UI
```powershell
cd C:\Users\ralme\Documents\EducationWebsite\diagram_finder
python app.py
# http://127.0.0.1:5001
```

### 2. CLI Batch
```powershell
python run_finder.py --input-dir ./input_pdfs --out-dir ./output --profile "Slave 9" --pdf 2025allproblems_1.pdf
```

### 3. Environment
```ini
# .env (same as extraction_chatter)
AZURE_CV_ENDPOINT=https://ralmimageextraction.cognitiveservices.azure.com
AZURE_CV_KEY=6s8JxJvCQHmbm5UMOxkBZ454LfyNKKK6fdSpISQFPiFo2kfLHOzGJQQJ99CIAC4f1cMXJ3w3AAAFACOGvhla
```

---

## Naming Convention

`Q{number}_{part}_{hint-slug}_p{page}.png`

- `part`: `question`, `choice_a`..`choice_d`
- `hint-slug`: `diagram_info[part].hint` lowercased, bbox stripped, slugified (e.g. `Venn diagram [20,240,330,760]` → `venn-diagram`)
- Example: `Q10_question_venn-diagram_p4.png`, `Q19_question_linear-inequality-graph_p8.png`

---

## Relation to `failbetterscreenshoter`

`failbetterscreenshoter` (Video → Frames → PDF) remains standalone. `diagram_finder` borrows its file-drop UI pattern and `work/` temp handling, but replaces video logic with PDF → NotebookLM → crop.
