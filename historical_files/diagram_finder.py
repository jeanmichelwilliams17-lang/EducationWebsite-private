import json
import re
import asyncio
from pathlib import Path
from typing import List, Dict, Any

from .ai_provider import NotebookLMProvider
from .ocr_engine import OCREngine

import logging
log = logging.getLogger("diagram_finder")

def slugify(hint: str) -> str:
    hint = re.sub(r"\[.*?\]", "", hint)
    hint = hint.lower()
    hint = re.sub(r"[^a-z0-9]+", "-", hint)
    hint = hint.strip("-")
    return hint[:40] or "diagram"

async def find_diagrams(pdf_path: Path, out_dir: Path, profile: str = "Slave 9", dpi: int = 150) -> List[Dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    provider = NotebookLMProvider(profile=profile)
    questions = await provider.extract_document_questions(pdf_path, chunk_size=1000)
    try:
        import fitz
        doc = fitz.open(str(pdf_path))
    except Exception as e:
        log.warning("PyMuPDF open failed: %s", e)
        doc = None

    ocr = OCREngine()
    manifest = []
    for q in questions:
        info = q.get("diagram_info", {}) or {}
        qnum = q.get("question_number")
        label = q.get("label_path", str(qnum))
        for part in ["question","A","B","C","D"]:
            pinfo = info.get(part, {})
            if not isinstance(pinfo, dict) or not pinfo.get("present"):
                continue
            page = pinfo.get("page") or q.get("source_page_number") or 1
            hint = pinfo.get("hint") or ""
            slug = slugify(hint)
            part_slug = part.lower() if part != "question" else "question"
            filename = f"Q{qnum}_{part_slug}_{slug}_p{page}.png"
            dest = out_dir / filename
            # render page
            if doc is not None:
                idx = max(0, min(len(doc)-1, page-1))
                pix = doc[idx].get_pixmap(dpi=dpi)
                tmp = out_dir / f"_tmp_p{page}.png"
                pix.save(str(tmp))
                res = ocr.smart_crop_diagram(tmp, out_dir, f"Q{qnum}_{part_slug}_{slug}_p{page}", hint=hint, part=part_slug)
                # smart_crop already saves with its own naming, rename to our slug
                if res and res.get("diagram_path"):
                    src = Path(res["diagram_path"])
                    if src.exists() and src != dest:
                        try:
                            src.rename(dest)
                        except Exception:
                            pass
                        res["diagram_path"] = str(dest)
                        res["diagram_file"] = dest.name
                    manifest.append({"question_number": qnum, "label_path": label, "part": part, "page": page, "hint": hint, "bbox": res.get("bounding_box"), "method": res.get("method"), "file": dest.name, "path": str(dest)})
                else:
                    # fallback: save full page crop
                    tmp.rename(dest)
                    manifest.append({"question_number": qnum, "label_path": label, "part": part, "page": page, "hint": hint, "bbox": None, "method": "full_page", "file": dest.name, "path": str(dest)})
            else:
                manifest.append({"question_number": qnum, "label_path": label, "part": part, "page": page, "hint": hint, "bbox": None, "method": "no_pdf", "file": filename, "path": str(dest)})
    # save manifest
    man_path = out_dir.parent / "diagram_manifest.json"
    man_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
