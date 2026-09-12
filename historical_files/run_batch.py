"""
run_batch.py
Headless CLI batch pipeline for question extraction, structuring,
and KaTeX validation.
"""

import argparse
import asyncio
import datetime
import json
import logging
import sys
import uuid
from pathlib import Path

from backend.ai_provider import SimulationProvider, get_ai_provider
from backend.batch_scheduler import MultiProfilePoolManager, chunk_into_batches
from backend.ocr_engine import OCREngine
from backend.validator import validate_question_dict

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("extraction.cli")


async def process_question_block(provider, ocr: OCREngine, block: dict, diagrams_dir: Path) -> dict:
    """Process a single OCR question block through structuring, Smart Crop, and validation."""
    ocr_text = block["ocr_text"]
    q_num = block.get("question_number", 1)
    src_file = block.get("source_file", "unknown")
    q_id = str(uuid.uuid4())

    diagram_desc = None
    diagram_type = None
    diagram_file = None

    # Step A3: Azure Smart Crop if diagram or figure is detected
    if block.get("has_diagram") or block.get("image_path"):
        img_p = Path(block.get("image_path", ""))
        if img_p.exists():
            crop_res = ocr.smart_crop_diagram(img_p, diagrams_dir, q_id)
            if crop_res:
                diagram_file = crop_res.get("diagram_file")
                # Query NotebookLM for dense verbatim description
                desc_res = await provider.describe_diagram(crop_res.get("diagram_path"))
                diagram_desc = desc_res.get("full_description")
                diagram_type = desc_res.get("diagram_type")

    # Step A4: Structure raw OCR text through NotebookLM
    structured = await provider.extract_and_structure(ocr_text, diagram_desc or "")

    raw_stem = structured.get("raw_stem", ocr_text)
    raw_choices = structured.get("raw_choices", [])
    visible_ans = structured.get("visible_correct_answer")

    # Step B5a: Light rewording & KaTeX normalization through NotebookLM
    reworded = await provider.reword_and_clean(raw_stem, raw_choices, visible_ans)

    clean_stem = reworded.get("reworded_stem", raw_stem)
    clean_choices = reworded.get("reworded_choices", raw_choices)
    correct_idx = reworded.get("correct_choice_index")

    # Step C6 (Early): Deterministic KaTeX validation
    q_record = {
        "id": q_id,
        "question_number": q_num,
        "source_type": "own_pdf",
        "source_file": src_file,
        "raw_stem": raw_stem,
        "reworded_stem": clean_stem,
        "choices": clean_choices,
        "correct_choice_index": correct_idx,
        "diagram_file": diagram_file,
        "diagram_description": diagram_desc or structured.get("diagram_description_verbatim"),
        "diagram_type": diagram_type or structured.get("diagram_type"),
        "needs_human_review": structured.get("needs_human_review", False),
        "extraction_notes": structured.get("extraction_notes", ""),
        "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }

    is_valid, validation_errors = validate_question_dict(q_record)
    q_record["validation_status"] = "valid" if is_valid else "flagged_katex"
    q_record["validation_errors"] = validation_errors

    if not is_valid or reworded.get("possible_error_flag"):
        q_record["needs_human_review"] = True

    return q_record


async def process_question_batch(
    provider, ocr: OCREngine, blocks: list, diagrams_dir: Path, src_file: str
) -> list:
    """Process a batch of >= 10 question blocks via a single batched AI call for KaTeX normalization."""
    if not blocks:
        return []

    q_nums = [b.get("question_number", i + 1) for i, b in enumerate(blocks)]
    log.info("Processing batch Q%d–Q%d (%d questions)...", q_nums[0], q_nums[-1], len(blocks))

    # Build batch payload for batch KaTeX rewording
    batch_payload = []
    for block in blocks:
        batch_payload.append({
            "question_number": block.get("question_number", 1),
            "context_latex": block.get("context_latex"),
            "raw_stem": block.get("ocr_text", ""),
            "raw_choices": block.get("raw_choices", []),
            "visible_correct_answer": block.get("visible_correct_answer"),
        })

    try:
        reworded_results = await provider.reword_and_clean_batch(batch_payload)
        reword_map = {
            item.get("question_number"): item
            for item in reworded_results
            if isinstance(item, dict)
        }
    except Exception as e:
        log.warning("Batch reword failed: %s. Using simulation fallback.", e)
        reword_map = {}

    sim = SimulationProvider()

    file_questions = []
    for block in blocks:
        q_num = block.get("question_number", 1)
        q_id = str(uuid.uuid4())
        ocr_text = block.get("ocr_text", "")
        raw_choices = block.get("raw_choices", [])

        reworded = reword_map.get(q_num)
        if not reworded:
            reworded = sim.reword_and_clean_sync(ocr_text, raw_choices, None)

        q_record = {
            "id": q_id,
            "question_number": q_num,
            "source_type": "own_pdf",
            "source_file": src_file,
            "raw_stem": ocr_text,
            "reworded_stem": reworded.get("reworded_stem", ocr_text),
            "choices": reworded.get("reworded_choices", raw_choices),
            "correct_choice_index": reworded.get("correct_choice_index"),
            "diagram_file": None,
            "diagram_description": block.get("diagram_description"),
            "diagram_type": None,
            "needs_human_review": reworded.get("possible_error_flag", False),
            "extraction_notes": "",
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        }

        is_valid, validation_errors = validate_question_dict(q_record)
        q_record["validation_status"] = "valid" if is_valid else "flagged_katex"
        q_record["validation_errors"] = validation_errors

        # Flagged fix loop: send question + images + original doc + reason into new notebook chat, then revalidate
        if not is_valid or reworded.get("possible_error_flag"):
            critique_parts = []
            if validation_errors:
                critique_parts.append(f"KaTeX issues: {'; '.join(validation_errors)}")
            if reworded.get("possible_error_note"):
                critique_parts.append(reworded.get("possible_error_note"))
            critique_text = " | ".join(critique_parts) or "Formatting ambiguity detected."
            original_excerpt = ocr_text or block.get("raw_stem", "")
            image_hints = f"diagram_file={block.get('diagram_file')} diagram_description={block.get('diagram_description')} page={block.get('source_page_number')}"
            try:
                refined = await provider.critique_and_refine(q_record, critique_text, original_doc_excerpt=original_excerpt, image_hints=image_hints)
                if refined and isinstance(refined, dict):
                    if refined.get("reworded_stem"):
                        q_record["reworded_stem"] = refined["reworded_stem"]
                    if refined.get("reworded_choices"):
                        q_record["choices"] = refined["reworded_choices"]
                    if refined.get("explanation_latex"):
                        q_record["explanation_latex"] = refined["explanation_latex"]
                    if refined.get("refinement_notes"):
                        q_record["extraction_notes"] = refined.get("refinement_notes")
                is_valid, validation_errors = validate_question_dict(q_record)
                q_record["validation_status"] = "valid" if is_valid else "flagged_katex"
                q_record["validation_errors"] = validation_errors
                q_record["needs_human_review"] = not is_valid
            except Exception as e:
                log.warning("Refine failed Q%d: %s", q_num, e)
                q_record["needs_human_review"] = True
        else:
            q_record["needs_human_review"] = False

        file_questions.append(q_record)

    log.info("Batch Q%d–Q%d: %d/%d valid.", q_nums[0], q_nums[-1],
             sum(1 for q in file_questions if q["validation_status"] == "valid"), len(file_questions))
    return file_questions


async def main_async(args):
    input_dir = Path(args.input_dir)
    out_dir = Path(args.out_dir)
    diagrams_dir = out_dir / "diagrams"
    out_dir.mkdir(parents=True, exist_ok=True)
    diagrams_dir.mkdir(parents=True, exist_ok=True)

    batch_size = max(10, args.batch_size)

    ocr = OCREngine()
    provider = get_ai_provider(args.provider)

    files = list(input_dir.glob("*.pdf")) + list(input_dir.glob("*.txt"))
    if not files:
        log.warning("No .pdf or .txt files found in %s", input_dir)
        return

    log.info("Found %d file(s) to process via %s (batch_size=%d)", len(files), provider.__class__.__name__, batch_size)
    all_extracted_questions = []

    for f in files:
        log.info("--- Extracting file: %s ---", f.name)
        if f.suffix.lower() == ".pdf":
            blocks = ocr.extract_from_pdf(str(f))
        else:
            text = f.read_text(encoding="utf-8")
            blocks = ocr.segment_questions(text, filename=f.name)

        log.info("Found %d question block(s) in %s — chunking into batches of %d", len(blocks), f.name, batch_size)

        file_questions = []
        chunks = chunk_into_batches(blocks, batch_size, min_batch=10)
        if len(chunks) > 1 and args.provider in ("notebooklm", "auto"):
            pool_mgr = MultiProfilePoolManager()

            async def _run_chunk(chunk, profile_name):
                from backend.ai_provider import NotebookLMProvider
                prof_provider = NotebookLMProvider(profile=profile_name)
                return await process_question_batch(prof_provider, ocr, chunk, diagrams_dir, f.name)

            results = await pool_mgr.map_parallel(chunks, _run_chunk)
            for batch_res in results:
                if isinstance(batch_res, list):
                    file_questions.extend(batch_res)
        else:
            for chunk_idx, chunk in enumerate(chunks, 1):
                log.info("  Batch %d/%d (%d questions)...", chunk_idx, len(chunks), len(chunk))
                batch_res = await process_question_batch(provider, ocr, chunk, diagrams_dir, f.name)
                file_questions.extend(batch_res)

        out_file = out_dir / f"extracted_{f.stem}.json"
        out_file.write_text(json.dumps(file_questions, indent=2), encoding="utf-8")
        log.info("Saved %d questions to %s", len(file_questions), out_file.name)
        all_extracted_questions.extend(file_questions)

    if args.merge and all_extracted_questions:
        merge_path = Path(args.merge)
        merge_path.write_text(json.dumps(all_extracted_questions, indent=2), encoding="utf-8")
        log.info("Merged %d total questions into %s", len(all_extracted_questions), merge_path)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Headless batch question extraction & KaTeX validation via NotebookLM")
    parser.add_argument("--input-dir", default="./input_pdfs", help="Folder containing raw question PDFs or text files")
    parser.add_argument("--out-dir", default="./output", help="Folder to write structured JSON files to")
    parser.add_argument("--provider", default="notebooklm", choices=["notebooklm", "openai", "simulation", "auto"], help="AI provider (default: notebooklm)")
    parser.add_argument("--merge", default="./output/all_extracted.json", help="Path for merged JSON output")
    parser.add_argument("--batch-size", type=int, default=12, help="Number of questions per batch for KaTeX normalization (minimum 10, default 12)")
    parser.add_argument("--list-profiles", action="store_true", help="List all NotebookLM accounts and rate-limit statuses")
    parser.add_argument("--sync-profiles", action="store_true", help="Sequentially sync/login all NotebookLM account profiles")
    parser.add_argument("--login-profile", type=str, default=None, help="Log in or refresh cookies for a specific profile name")
    parser.add_argument("--browser", type=str, default="chrome", choices=["chrome", "msedge"], help="Browser to launch for login")
    args = parser.parse_args()

    if args.list_profiles:
        from backend.notebooklm_client import list_profiles
        profiles = list_profiles()
        print(f"\nFound {len(profiles)} NotebookLM Profile(s):")
        for p in profiles:
            status = "[ACTIVE]" if p.get("authenticated") else "[NO SESSION]"
            rl = p.get("rate_limited")
            rl_str = f" [Rate-limited: {rl.get('remaining_human')}]" if rl else ""
            print(f"  - {p['name']} ({p.get('email') or 'no email'}): {status}{rl_str}")
        print("")
        return

    if args.sync_profiles:
        from backend.notebooklm_client import batch_login_profiles
        print(f"\nStarting batch login / sync across all NotebookLM profiles using {args.browser}...")
        results = batch_login_profiles(browser_choice=args.browser)
        print(f"\nBatch sync complete. Results ({len(results)}):")
        for r in results:
            stat = "[OK]" if r.get("ok") else "[FAILED]"
            print(f"  - {r.get('name')}: {stat} ({r.get('message')})")
        print("")
        return


    if args.login_profile:
        from backend.notebooklm_client import login_profile
        print(f"\nLaunching {args.browser} to login profile '{args.login_profile}'...")
        res = login_profile(args.login_profile, browser_choice=args.browser)
        print("Result:", res)
        return

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()

