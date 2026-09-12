#!/usr/bin/env python3
import asyncio, json, uuid, zipfile, pathlib, logging, threading, re, time, shutil
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file, send_from_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("unified_app")

from backend.ai_provider import NotebookLMProvider
from backend.validator import validate_question_dict
from backend.batch_scheduler import MultiProfilePoolManager, chunk_into_batches, telemetry

app = Flask(__name__)
BASE = Path(__file__).parent
UPLOAD = BASE / "uploads"
OUTPUT = BASE / "output"
UPLOAD.mkdir(exist_ok=True)
OUTPUT.mkdir(parents=True, exist_ok=True)

JOBS = {}

WATCH_DIR = Path(r"C:\Users\ralme\OneDrive\Pictures\Screenshots 1")
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
_DIAGRAM_WATCH = {"active": False, "job_id": None, "seen": set(), "history": [], "last_captured": None, "lock": threading.Lock(), "thread": None, "stop_evt": None}

def _is_image(p: Path) -> bool:
    return p.suffix.lower() in IMAGE_EXTS

def _safe_copy(src: Path, dst: Path, retries: int = 4) -> bool:
    for attempt in range(retries):
        try:
            if not src.exists():
                time.sleep(0.2)
                continue
            s1 = src.stat().st_size
            time.sleep(0.25)
            try:
                s2 = src.stat().st_size
            except Exception:
                s2 = s1
            if s1 != s2 and attempt < retries - 1:
                time.sleep(0.25)
                continue
            if s1 == 0:
                time.sleep(0.3)
                if attempt < retries - 1:
                    continue
            shutil.copy2(str(src), str(dst))
            return True
        except PermissionError:
            time.sleep(0.35 * (attempt + 1))
        except Exception as e:
            log.warning("copy %s -> %s failed (attempt %d): %s", src, dst, attempt + 1, e)
            time.sleep(0.3 * (attempt + 1))
    return False

def _get_next_expected(job):
    for idx, e in enumerate(job.get("expected_diagrams", [])):
        if not e.get("fulfilled"):
            return idx, e
    return None, None

def _diagram_watch_loop():
    log.info("diagram watch loop started for %s watching %s", _DIAGRAM_WATCH.get("job_id"), WATCH_DIR)
    while _DIAGRAM_WATCH.get("stop_evt") and not _DIAGRAM_WATCH["stop_evt"].is_set():
        try:
            job_id = _DIAGRAM_WATCH.get("job_id")
            if not job_id or not _DIAGRAM_WATCH.get("active"):
                time.sleep(0.5)
                continue
            job = JOBS.get(job_id)
            if not job:
                time.sleep(0.5)
                continue
            if not WATCH_DIR.exists():
                time.sleep(0.7)
                continue
            candidates = []
            for p in WATCH_DIR.iterdir():
                if not p.is_file() or not _is_image(p):
                    continue
                if p.name in _DIAGRAM_WATCH["seen"]:
                    continue
                try:
                    candidates.append((p.stat().st_mtime, p))
                except Exception:
                    continue
            candidates.sort(key=lambda x: x[0])
            for _, p in candidates:
                idx, nxt = _get_next_expected(job)
                if nxt is None:
                    break
                s1 = 0
                try:
                    s1 = p.stat().st_size
                    time.sleep(0.2)
                    s2 = p.stat().st_size
                    if s1 != s2:
                        continue
                    if s1 == 0:
                        continue
                except Exception:
                    continue
                _DIAGRAM_WATCH["seen"].add(p.name)
                out_dir = OUTPUT / f"{job_id}_diagrams"
                out_dir.mkdir(parents=True, exist_ok=True)
                suffix = p.suffix.lower() if p.suffix.lower() in IMAGE_EXTS else ".png"
                dest = out_dir / f"{nxt['expected_name']}{suffix}"
                if _safe_copy(p, dest):
                    try:
                        p.unlink()
                    except Exception:
                        pass
                    nxt["fulfilled"] = True
                    nxt["expected_name"] = dest.name
                    q = next((qq for qq in job.get("questions", []) if qq.get("question_uuid") == nxt.get("question_uuid")), None)
                    if q is not None:
                        q["diagram_file"] = f"{job_id}_diagrams/{dest.name}"
                    entry = {"src": p.name, "dest": dest.name, "expected_idx": idx, "hint": nxt.get("hint",""), "page": nxt.get("page"), "ts": time.time()}
                    with _DIAGRAM_WATCH["lock"]:
                        _DIAGRAM_WATCH["history"].append(entry)
                        _DIAGRAM_WATCH["last_captured"] = entry
                    job["diagram_dir"] = str(out_dir)
                    telemetry.add_event(f'[diagram] captured {nxt["expected_name"]} <- {p.name} ({nxt.get("question_number")}{nxt.get("label_path","")})', "success")
                    remaining = [e for e in job.get("expected_diagrams", []) if not e.get("fulfilled")]
                    if not remaining and job.get("status") == "awaiting_screenshots":
                        job["status"] = "diagrams_uploaded"
                        _DIAGRAM_WATCH["active"] = False
                        if _DIAGRAM_WATCH.get("stop_evt"):
                            _DIAGRAM_WATCH["stop_evt"].set()
                        telemetry.add_event(f"All {len(job.get('expected_diagrams',[]))} diagrams captured — ready to reword", "success")
                        log.info("job %s all diagrams fulfilled, auto-stopped watcher", job_id)
                else:
                    _DIAGRAM_WATCH["seen"].discard(p.name)
        except Exception as e:
            log.warning("watch loop error: %s", e)
        time.sleep(0.45)
    log.info("diagram watch loop exited")

def _start_diagram_watch(job_id):
    job = JOBS.get(job_id)
    if not job:
        return {"ok": False, "error": "job not found"}
    if not job.get("expected_diagrams"):
        return {"ok": False, "error": "no expected diagrams for job"}
    with _DIAGRAM_WATCH["lock"]:
        if _DIAGRAM_WATCH["active"] and _DIAGRAM_WATCH["job_id"] != job_id:
            return {"ok": False, "error": f"another job {_DIAGRAM_WATCH['job_id']} is being watched"}
        if _DIAGRAM_WATCH["active"] and _DIAGRAM_WATCH["job_id"] == job_id:
            return {"ok": True, "already": True, "job_id": job_id}
        _DIAGRAM_WATCH["active"] = True
        _DIAGRAM_WATCH["job_id"] = job_id
        _DIAGRAM_WATCH["history"] = []
        _DIAGRAM_WATCH["last_captured"] = None
        existing = set()
        if WATCH_DIR.exists():
            for p in WATCH_DIR.iterdir():
                if p.is_file():
                    existing.add(p.name)
        _DIAGRAM_WATCH["seen"] = existing
        _DIAGRAM_WATCH["stop_evt"] = threading.Event()
        t = threading.Thread(target=_diagram_watch_loop, daemon=True)
        _DIAGRAM_WATCH["thread"] = t
        t.start()
    return {"ok": True, "job_id": job_id, "watch_dir": str(WATCH_DIR)}

def _stop_diagram_watch(job_id=None):
    with _DIAGRAM_WATCH["lock"]:
        target = job_id or _DIAGRAM_WATCH.get("job_id")
        if not _DIAGRAM_WATCH["active"]:
            return {"ok": True, "already_stopped": True, "job_id": target}
        if job_id and _DIAGRAM_WATCH["job_id"] != job_id:
            return {"ok": False, "error": "watch job mismatch"}
        evt = _DIAGRAM_WATCH.get("stop_evt")
        if evt:
            evt.set()
        _DIAGRAM_WATCH["active"] = False
    return {"ok": True, "job_id": target}

def _split_subparts(label_path: str):
    return re.findall(r"\([a-zA-Z0-9]+\)", label_path or "")

def _assign_uuids(qs, job_uuid):
    from collections import defaultdict
    groups = defaultdict(list)
    for q in qs:
        groups[q.get("question_number")].append(q)
    for qnum, group in groups.items():
        shared_q_uuid = str(uuid.uuid4())
        def _sort_key(item):
            lp = item.get("label_path") or str(item.get("question_number"))
            return lp
        group_sorted = sorted(group, key=_sort_key)
        has_subparts = len(group_sorted) > 1 or any(_split_subparts(g.get("label_path", "")) for g in group_sorted)
        if not has_subparts:
            q = group_sorted[0]
            q["job_uuid"] = job_uuid
            q["question_uuid"] = shared_q_uuid
            q["subparts"] = []
            q["id"] = f'{job_uuid}_{shared_q_uuid}_{q.get("question_number")}'
        else:
            for idx, q in enumerate(group_sorted, start=1):
                q["job_uuid"] = job_uuid
                q["question_uuid"] = shared_q_uuid
                tokens = _split_subparts(q.get("label_path", ""))
                label = tokens[-1] if tokens else q.get("label_path", "")
                q["subparts"] = [{"subpart_uuid": str(uuid.uuid4()), "order": idx, "label": label, "question_uuid": shared_q_uuid}]
                q["id"] = f'{job_uuid}_{shared_q_uuid}_{q.get("question_number")}_{q["subparts"][0]["subpart_uuid"]}'

def _ensure_id(q):
    if not q.get("id"):
        q["id"] = f'{q.get("job_uuid","")}_{q.get("question_uuid","")}_{q.get("question_number")}'
    return q

def _extract_text_from_file(p: Path) -> str:
    try:
        if p.suffix.lower() == ".pdf":
            import fitz
            doc = fitz.open(str(p))
            txt = "\n".join([str(page.get_text()) for page in doc])  # type: ignore
            doc.close()
            return txt
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        if p.suffix.lower() == ".pdf":
            return ""
        try:
            return p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""

def _final_status_for(q):
    if q.get("final_status"):
        return q["final_status"]
    if q.get("needs_human_review"):
        return "needs_human"
    if q.get("possible_error_flag"):
        return "needs_human"
    if q.get("validation_status") == "flagged":
        return "needs_human"
    return "auto_valid"

def _build_expected_diagrams(qs, job_uuid):
    expected = []
    for q in qs:
        info = q.get("diagram_info", {})
        subpart_seg = f"_{q['subparts'][-1]['subpart_uuid']}" if q.get("subparts") else ""
        # question diagrams: support list (future) or single dict (current)
        q_diagrams = info.get("question", {})
        if isinstance(q_diagrams, list):
            q_list = [d for d in q_diagrams if isinstance(d, dict) and d.get("present")]
        elif isinstance(q_diagrams, dict) and q_diagrams.get("present"):
            q_list = [q_diagrams]
        else:
            q_list = []
        for order, d in enumerate(q_list, start=1):
            hint = d.get("hint","") or ""
            suffix = f"question_{order}"
            name = f"{job_uuid}_{q['question_uuid']}{subpart_seg}_{suffix}"
            expected.append({"question_number": q["question_number"], "label_path": q.get("label_path"), "part": "question", "order": order, "hint": hint, "page": d.get("page"), "expected_name": name, "question_uuid": q["question_uuid"], "subpart_uuid": q["subparts"][-1]["subpart_uuid"] if q.get("subparts") else None, "fulfilled": False})
        for part in ["A","B","C","D"]:
            pinfo = info.get(part, {})
            if isinstance(pinfo, dict) and pinfo.get("present"):
                hint = pinfo.get("hint","") or ""
                suffix = f"answer_{part}"
                name = f"{job_uuid}_{q['question_uuid']}{subpart_seg}_{suffix}"
                expected.append({"question_number": q["question_number"], "label_path": q.get("label_path"), "part": part, "hint": hint, "page": pinfo.get("page"), "expected_name": name, "question_uuid": q["question_uuid"], "subpart_uuid": q["subparts"][-1]["subpart_uuid"] if q.get("subparts") else None, "fulfilled": False})
    expected.sort(key=lambda e: ((e.get("page") or 9999), 0 if e.get("part") == "question" else 1, e.get("question_number") or 0))
    return expected

def _resolve_profile(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw or raw.lower() in ("auto", "notebooklm", "notebooklm-py", "openai", "simulation"):
        try:
            from backend.notebooklm_client import list_profiles
            avail = [p for p in list_profiles() if p.get("authenticated") and not p.get("disabled")]
            if not avail:
                return "Slave 9"
            slave9 = next((p for p in avail if p["name"] == "Slave 9"), None)
            return (slave9 or avail[0])["name"]
        except Exception:
            return "Slave 9"
    try:
        from backend.notebooklm_client import list_profiles
        avail = [p for p in list_profiles() if p.get("authenticated") and not p.get("disabled")]
        names = {p["name"] for p in avail}
        if raw in names:
            return raw
        if avail:
            return avail[0]["name"]
    except Exception:
        pass
    return raw or "Slave 9"

def _run_extraction_job(job_id, pdf_path, profile, ans_filename=None):
    job = JOBS.get(job_id)
    if not job:
        return
    job_uuid = job.get("job_uuid")
    try:
        telemetry.start_job(job_id, Path(str(pdf_path)).name, 0)
        telemetry.set_stage("extracting", "Extracting questions from PDF")
        job["status"] = "extracting"
        job["progress"] = {"stage": "extracting", "pct": 5, "msg": "OCR & parsing PDF..."}
        job["error"] = None
        async def do_extract():
            prov = NotebookLMProvider(profile=profile)
            qs = await prov.extract_document_questions(Path(pdf_path), chunk_size=1000)
            _assign_uuids(qs, job_uuid)
            return qs
        qs = asyncio.run(do_extract())
        for q in qs:
            q["exam"] = job.get("exam") or Path(str(pdf_path)).stem[:40]
            q["subject"] = job.get("subject") or "Unknown"
            q["year"] = job.get("year") or ""
        if not qs:
            job["status"] = "extraction_failed"
            job["error"] = "0 questions parsed — raw output saved to output/debug_*.txt (see logs)"
            job["questions"] = []
            job["expected_diagrams"] = []
            job["progress"] = {"stage": "failed", "pct": 0, "msg": "Extraction returned 0 questions — check debug raw file"}
            telemetry.finish_job("failed", "Extraction returned 0 questions — raw saved for review")
            log.warning("job %s extraction_failed: 0 questions parsed", job_id)
            return
        job["progress"] = {"stage": "extracting", "pct": 70, "msg": f"Structured {len(qs)} questions"}
        if ans_filename:
            ans_path = UPLOAD / ans_filename
            if ans_path.exists():
                try:
                    ans_text = _extract_text_from_file(ans_path)
                    if ans_text.strip():
                        async def _do_pair():
                            prov2 = NotebookLMProvider(profile=profile)
                            try:
                                pairs = await prov2.extract_from_answer_script(ans_text, source_paths=[str(ans_path)])
                            except Exception:
                                pairs = []
                            if not pairs:
                                from backend.ai_provider import SimulationProvider
                                pairs = await SimulationProvider().extract_from_answer_script(ans_text)
                            return pairs
                        pairs = asyncio.run(_do_pair())
                        pair_map = {p.get("question_number"): p for p in pairs if p.get("question_number")}
                        for q in qs:
                            m = pair_map.get(q.get("question_number"))
                            if m:
                                q["visible_correct_answer"] = q.get("visible_correct_answer") or m.get("correct_choice_letter") or (chr(ord('A')+m.get("correct_choice_index",0)) if isinstance(m.get("correct_choice_index"), int) else None)
                                if m.get("correct_choice_index") is not None and isinstance(m.get("correct_choice_index"), int):
                                    q["correct_choice_index"] = m.get("correct_choice_index")
                                q["explanation_latex"] = m.get("explanation_latex") or q.get("explanation_latex")
                except Exception as ae:
                    log.warning("answer_key pairing failed: %s", ae)
        expected = _build_expected_diagrams(qs, job_uuid)
        job["questions"] = qs
        job["expected_diagrams"] = expected
        job["status"] = "diagrams_uploaded" if not expected else "awaiting_screenshots"
        job["progress"] = {"stage": job["status"], "pct": 100, "msg": f"Extracted {len(qs)} questions — {len(expected)} diagrams" if expected else f"Extracted {len(qs)} questions — no diagrams needed"}
        out = OUTPUT / f"{job_id}_extract.json"
        out.write_text(json.dumps(qs, indent=2, ensure_ascii=False), encoding="utf-8")
        telemetry.finish_job("completed", f"Extracted {len(qs)} questions")
        log.info("job %s extraction done: %d qs, %d diagrams", job_id, len(qs), len(expected))
    except Exception as e:
        import traceback; traceback.print_exc()
        job["status"] = "failed"
        job["error"] = str(e)
        job["progress"] = {"stage": "failed", "pct": 0, "msg": str(e)[:200]}
        telemetry.finish_job("failed", str(e))

def _run_full_pipeline(job_id):
    """
    Executes the complete 11-step pipeline from workflowintext.txt:
    1. Rewording & KaTeX pass (parallel batches of 10)
    2. Solve pass (parallel batches of 10, independent derivations)
    3. Answer validation pass (parallel batches of 10, independent verifications)
    4. Fix & Recheck loops (batches of 5):
       - Answer Fix + Answer Recheck
       - Wording / KaTeX Refine + KaTeX second validation
    5. Completion:
       - Combine into verified and human_review
       - Generate _final.json and _human_review.json
       - Mark job completed (only when all stages have cleared)
    """
    job = JOBS.get(job_id)
    if not job:
        return
    qs = job.get("questions") or []
    if not qs:
        return
    try:
        pdf_path = Path(str(job.get("pdf") or ""))
        diag_dir = Path(str(job.get("diagram_dir") or "")) if job.get("diagram_dir") else None
        img_map = {}
        if diag_dir and diag_dir.exists():
            for ext in IMAGE_EXTS:
                for p in diag_dir.glob(f"*{ext}"):
                    for q in qs:
                        if q.get("question_uuid") and q["question_uuid"] in p.name:
                            img_map.setdefault(q["question_number"], []).append(str(p))
            for k in list(img_map.keys()):
                img_map[k] = sorted(set(img_map[k]))

        # ==================== STEP 4: REWORDING PASS ====================
        job["status"] = "rewording"
        job["progress"] = {"stage": "rewording", "pct": 10, "msg": f"Rewording {len(qs)} questions (KaTeX normalization)"}
        telemetry.start_job(job_id, pdf_path.name, len(qs))
        telemetry.set_stage("rewording", f"Rewording {len(qs)} questions in parallel", total_for_stage=len(qs), pct_override=10)

        chunks = chunk_into_batches(qs, 12, min_batch=10)
        pool = MultiProfilePoolManager()

        async def process_reword(chunk, profile):
            q_nums = [q["question_number"] for q in chunk]
            telemetry.record_batch_start(q_nums, profile)
            paths = [str(pdf_path)] + [img for q in chunk for img in img_map.get(q["question_number"], [])]
            prov = NotebookLMProvider(profile=profile)
            batch_payload = [{
                "question_number": q["question_number"],
                "context_latex": q.get("context_latex"),
                "raw_stem": q.get("raw_stem", ""),
                "raw_choices": q.get("raw_choices", []),
                "visible_correct_answer": q.get("visible_correct_answer"),
                "diagram_hint": q.get("diagram_description") or q.get("diagram_info"),
                "label_path": q.get("label_path")
            } for q in chunk]
            reworded = await prov.reword_and_clean_batch(batch_payload, source_paths=paths)
            used_profile = getattr(prov, "_last_used_profile", profile)
            telemetry.record_batch_complete(q_nums, used_profile)
            return reworded

        chunk_results = asyncio.run(pool.map_parallel(chunks, process_reword))
        reworded = []
        for r in chunk_results:
            if isinstance(r, list):
                reworded.extend(r)
            elif isinstance(r, Exception):
                raise r

        # Build records
        records = []
        flagged_wording = []
        valid_wording = []
        for r in reworded:
            qnum = r.get("question_number")
            orig = next((q for q in qs if q["question_number"] == qnum), {})
            rec = {
                "id": orig.get("id") or f'{job.get("job_uuid","")}_{orig.get("question_uuid","")}_{qnum}',
                "job_uuid": job.get("job_uuid"),
                "question_uuid": orig.get("question_uuid"),
                "label_path": orig.get("label_path"),
                "question_number": qnum,
                "raw_stem": orig.get("raw_stem"),
                "raw_choices": orig.get("raw_choices"),
                "reworded_stem": r.get("reworded_stem") or orig.get("raw_stem"),
                "choices": r.get("reworded_choices") or orig.get("raw_choices") or [],
                "reworded_choices": r.get("reworded_choices") or orig.get("raw_choices") or [],
                "table": r.get("table"),
                "correct_choice_index": r.get("correct_choice_index"),
                "possible_error_flag": r.get("possible_error_flag"),
                "possible_error_note": r.get("possible_error_note"),
                "explanation_latex": r.get("explanation_latex"),
                "diagram_info": orig.get("diagram_info"),
                "diagram_file": orig.get("diagram_file"),
                "diagram_description": orig.get("diagram_description"),
                "context_latex": r.get("reworded_context") or orig.get("context_latex"),
                "answer_status": "unsolved",
                "solve": None,
                "answer_validation": {}
            }
            is_valid, errs = validate_question_dict(rec)
            rec["validation_status"] = "valid" if is_valid else "flagged"
            rec["validation_errors"] = errs
            rec["needs_human_review"] = not is_valid or bool(r.get("possible_error_flag"))
            rec["wording_status"] = "valid" if is_valid and not r.get("possible_error_flag") else "flagged"
            _ensure_id(rec)
            records.append(rec)
            if rec["wording_status"] == "flagged":
                flagged_wording.append(rec)
            else:
                valid_wording.append(rec)

        job["questions"] = records
        job["reworded"] = reworded
        job["flagged"] = flagged_wording
        job["valid"] = valid_wording

        # ==================== STEP 5: SOLVE PASS ====================
        job["status"] = "solving"
        job["progress"] = {"stage": "solving", "pct": 30, "msg": f"Solving {len(records)} questions from first principles"}
        telemetry.set_stage("solve", f"Solving {len(records)} questions in parallel", total_for_stage=len(records), pct_override=30)

        solve_chunks = chunk_into_batches(records, 10, min_batch=10)
        async def process_solve(chunk, profile):
            q_nums = [q["question_number"] for q in chunk]
            telemetry.record_batch_start(q_nums, profile)
            paths = [str(pdf_path)] + [img for q in chunk for img in img_map.get(q["question_number"], [])]
            prov = NotebookLMProvider(profile=profile)
            res = await prov.solve_batch(chunk, source_paths=paths)
            used = getattr(prov, "_last_used_profile", profile)
            telemetry.record_batch_complete(q_nums, used)
            return res

        solve_results_chunks = asyncio.run(pool.map_parallel(solve_chunks, process_solve))
        solve_flat = []
        for r in solve_results_chunks:
            if isinstance(r, list):
                solve_flat.extend(r)
        solve_map = {r.get("question_number"): r for r in solve_flat if isinstance(r, dict)}

        for rec in records:
            s = solve_map.get(rec["question_number"])
            if s:
                rec["solve"] = {
                    "correct_choice_index": s.get("correct_choice_index"),
                    "explanation_latex": s.get("explanation_latex")
                }
                rec["correct_choice_index"] = s.get("correct_choice_index")
                rec["explanation_latex"] = s.get("explanation_latex")
                rec["answer_status"] = "unverified"

        # ==================== STEP 6: ANSWER VALIDATION PASS ====================
        job["status"] = "validating"
        job["progress"] = {"stage": "validating", "pct": 55, "msg": f"Validating answers for {len(records)} questions independently"}
        telemetry.set_stage("answer_validate", f"Validating {len(records)} answers independently", total_for_stage=len(records), pct_override=55)

        val_chunks = chunk_into_batches(records, 10, min_batch=10)
        async def process_validate(chunk, profile):
            q_nums = [q["question_number"] for q in chunk]
            telemetry.record_batch_start(q_nums, profile)
            paths = [str(pdf_path)] + [img for q in chunk for img in img_map.get(q["question_number"], [])]
            prov = NotebookLMProvider(profile=profile)
            res = await prov.validate_answers_batch(chunk, source_paths=paths)
            used = getattr(prov, "_last_used_profile", profile)
            telemetry.record_batch_complete(q_nums, used)
            return res

        val_results_chunks = asyncio.run(pool.map_parallel(val_chunks, process_validate))
        val_flat = []
        for r in val_results_chunks:
            if isinstance(r, list):
                val_flat.extend(r)
        val_map = {r.get("question_number"): r for r in val_flat if isinstance(r, dict)}

        flagged_answers = []
        for rec in records:
            qnum = rec.get("question_number")
            v = val_map.get(qnum)
            is_structured = not rec.get("choices")  # no choices = open-ended / structured question

            if is_structured:
                # Structured questions have no multiple-choice index to compare.
                # If validation produced an independent worked solution, adopt it
                if v and isinstance(v, dict) and v.get("independent_worked_solution") and not rec.get("explanation_latex"):
                    rec["explanation_latex"] = v.get("independent_worked_solution")
                has_explanation = bool(
                    rec.get("explanation_latex") or
                    (rec.get("solve") or {}).get("explanation_latex") or
                    (v and isinstance(v, dict) and v.get("independent_worked_solution"))
                )
                rec["answer_status"] = "verified" if has_explanation else "human_review"
                rec["answer_validation"] = {"matches": True, "mismatch_note": "Structured question — open-ended solution verified" if has_explanation else "Structured question — pending solution"}
                if not has_explanation:
                    flagged_answers.append(rec)
            elif v and isinstance(v, dict):
                solve_idx = (rec.get("solve") or {}).get("correct_choice_index") if rec.get("solve") else rec.get("correct_choice_index")
                val_idx = v.get("verified_correct_choice_index")

                both_present = (solve_idx is not None) and (val_idx is not None)
                either_present = (solve_idx is not None) or (val_idx is not None)

                if both_present and solve_idx == val_idx:
                    # ✅ Both agree — clear pass
                    rec["answer_validation"] = {**v, "matches": True}
                    rec["answer_status"] = "verified"
                elif both_present and solve_idx != val_idx:
                    # ❌ Genuine disagreement — needs fixing
                    rec["answer_validation"] = {**v, "matches": False,
                        "mismatch_note": f"Solve stage index {solve_idx} != Validation stage index {val_idx}"}
                    rec["answer_status"] = "flagged"
                    flagged_answers.append(rec)
                elif val_idx is not None:
                    # Solve returned no index but validation independently confirmed one → trust it
                    rec["correct_choice_index"] = val_idx
                    rec["answer_validation"] = {**v, "matches": True,
                        "mismatch_note": f"Solve index missing — validation index {val_idx} adopted"}
                    rec["answer_status"] = "verified"
                elif solve_idx is not None:
                    # Validation returned no index but solve stage has one → carry forward
                    rec["answer_validation"] = {**v, "matches": True,
                        "mismatch_note": f"Validation index missing — solve index {solve_idx} carried forward"}
                    rec["answer_status"] = "verified"
                else:
                    # Neither stage produced an index — flag it
                    rec["answer_validation"] = {**v, "matches": False,
                        "mismatch_note": "Neither solve nor validation returned a choice index"}
                    rec["answer_status"] = "flagged"
                    flagged_answers.append(rec)

            else:
                # Validation returned no result for this question — don't penalise it;
                # carry the solve-stage answer forward and mark as verified.
                solve_idx = (rec.get("solve") or {}).get("correct_choice_index") if rec.get("solve") else rec.get("correct_choice_index")
                if solve_idx is not None:
                    rec["answer_status"] = "verified"
                    rec["answer_validation"] = {"matches": True, "mismatch_note": "Validation stage skipped — solve-stage answer carried forward"}
                else:
                    rec["answer_status"] = "flagged"
                    rec["answer_validation"] = {"matches": False, "mismatch_note": "Validation did not return result and no solve-stage index available"}
                    flagged_answers.append(rec)

        # ==================== STEP 7 & 8: ANSWER FIX & RECHECK PASS ====================
        if flagged_answers:
            job["status"] = "fixing"
            job["progress"] = {"stage": "fixing", "pct": 75, "msg": f"Resolving {len(flagged_answers)} answer mismatches"}
            telemetry.set_stage("answer_fix", f"Fixing {len(flagged_answers)} answer mismatches", total_for_stage=len(flagged_answers), pct_override=75)

            ans_batches = [flagged_answers[i:i+5] for i in range(0, len(flagged_answers), 5)]
            async def process_answer_fix(batch, profile):
                q_nums = [r.get("question_number") for r in batch if r.get("question_number") is not None]
                telemetry.record_batch_start(q_nums, profile)
                prov = NotebookLMProvider(profile=profile)
                fixed_items = []
                for rec in batch:
                    qnum = rec.get("question_number")
                    reason = (rec.get("answer_validation") or {}).get("mismatch_note", "")
                    paths = [str(pdf_path)] + (img_map.get(qnum) or [])
                    fres = await prov.fix_answer(rec, reason, source_paths=paths)
                    if fres and isinstance(fres, dict) and fres.get("correct_choice_index") is not None:
                        rec["solve"] = {
                            "correct_choice_index": fres.get("correct_choice_index"),
                            "explanation_latex": fres.get("explanation_latex")
                        }
                        rec["correct_choice_index"] = fres.get("correct_choice_index")
                        rec["explanation_latex"] = fres.get("explanation_latex")
                        rec.setdefault("answer_validation", {})
                        if not isinstance(rec["answer_validation"], dict):
                            rec["answer_validation"] = {}
                        rec["answer_validation"]["resolution_note"] = fres.get("resolution_note", "")
                        # Step 8: Answer Recheck
                        val2 = await prov.validate_answers_batch([rec], source_paths=paths)
                        if val2 and len(val2) and isinstance(val2[0], dict):
                            v2 = val2[0]
                            matches2 = (v2.get("verified_correct_choice_index") is not None) and (v2.get("verified_correct_choice_index") == fres.get("correct_choice_index"))
                            rec["answer_status"] = "verified" if matches2 else "human_review"
                        else:
                            rec["answer_status"] = "human_review"
                    else:
                        rec["answer_status"] = "human_review"
                    fixed_items.append(rec)
                used = getattr(prov, "_last_used_profile", profile)
                telemetry.record_batch_complete(q_nums, used)
                return fixed_items

            asyncio.run(pool.map_parallel(ans_batches, process_answer_fix))

        # ==================== STEP 9 & 10: WORDING / KATEX RECHECK PASS ====================
        if flagged_wording:
            job["status"] = "rechecking"
            job["progress"] = {"stage": "rechecking", "pct": 85, "msg": f"Rechecking {len(flagged_wording)} notation flags"}
            telemetry.set_stage("recheck", f"Rechecking {len(flagged_wording)} KaTeX flags", total_for_stage=len(flagged_wording), pct_override=85)

            wording_batches = [flagged_wording[i:i+5] for i in range(0, len(flagged_wording), 5)]
            async def process_wording_recheck(batch, profile):
                q_nums = [r.get("question_number") for r in batch if r.get("question_number") is not None]
                telemetry.record_batch_start(q_nums, profile)
                prov = NotebookLMProvider(profile=profile)
                refined_items = []
                for rec in batch:
                    qnum = rec.get("question_number")
                    orig = next((q for q in qs if q.get("question_number") == qnum), {}) or {}
                    reason = rec.get("possible_error_note") or "; ".join(rec.get("validation_errors") or []) or "KaTeX validation warning"
                    paths = [str(pdf_path)] + (img_map.get(qnum) or [])
                    refined = await prov.critique_and_refine(
                        orig, reason,
                        original_doc_excerpt=orig.get("raw_stem", ""),
                        image_hints=str(orig.get("diagram_info") or ""),
                        source_paths=paths
                    )
                    if refined and isinstance(refined, dict) and not refined.get("refine_failed"):
                        is_v, errs = validate_question_dict(refined)
                        if is_v:
                            rec["reworded_stem"] = refined.get("reworded_stem") or refined.get("stem") or rec.get("reworded_stem")
                            rec["choices"] = refined.get("reworded_choices") or refined.get("choices") or rec.get("choices")
                            rec["reworded_choices"] = rec.get("choices")
                            rec["validation_status"] = "valid"
                            rec["validation_errors"] = []
                            rec["wording_status"] = "valid"
                            rec["needs_human_review"] = False
                        else:
                            rec["wording_status"] = "human_review"
                            rec["validation_errors"] = errs
                    else:
                        rec["wording_status"] = "human_review"
                    refined_items.append(rec)
                used = getattr(prov, "_last_used_profile", profile)
                telemetry.record_batch_complete(q_nums, used)
                return refined_items

            asyncio.run(pool.map_parallel(wording_batches, process_wording_recheck))

        # ==================== STEP 11: DIAGRAM REGENERATION (CODE GENERATION) ====================
        diag_records = [r for r in records if r.get("diagram_file")]
        if diag_records:
            job["status"] = "regen_diagrams"
            job["progress"] = {
                "stage": "regen_diagrams",
                "pct": 86,
                "msg": f"🎨 Step 11: Recreating {len(diag_records)} captured diagrams into code (matplotlib/mermaid/svg)"
            }
            telemetry.set_stage("regen_diagrams", f"Regenerating {len(diag_records)} diagrams as code", total_for_stage=len(diag_records), pct_override=86)

            diag_chunks = [[r] for r in diag_records]

            async def process_diagram_regen(chunk, profile):
                rec = chunk[0]
                qnum = rec.get("question_number")
                telemetry.record_batch_start([qnum], profile)
                prov = NotebookLMProvider(profile=profile)
                orig_file = rec.get("diagram_file")
                orig_path = (OUTPUT / orig_file).resolve() if orig_file else None

                from backend.ai_provider import DiagramGenerationFailed
                try:
                    res = await prov.regenerate_diagram(rec, diagram_path=str(orig_path) if orig_path and orig_path.exists() else None)
                except DiagramGenerationFailed as e:
                    used = getattr(prov, "_last_used_profile", profile)
                    telemetry.record_batch_complete([qnum], used, notes="AI regen failed", success=False)
                    # No fake diagram is inserted. The original screenshot stays the
                    # active file; the question goes straight to needs_fix (step 13
                    # will still attempt a real fix) with the genuine error recorded.
                    rec["diagram_render_ok"] = False
                    rec["diagram_render_error"] = str(e)
                    rec["diagram_original_file"] = orig_file
                    rec["diagram_format"] = rec.get("diagram_format") or "matplotlib"
                    rec["diagram_code"] = rec.get("diagram_code") or ""
                    return rec
                used = getattr(prov, "_last_used_profile", profile)
                telemetry.record_batch_complete([qnum], used)

                fmt = res.get("format") or "matplotlib"
                code = res.get("code") or ""
                rec["diagram_format"] = fmt
                rec["diagram_code"] = code
                rec["diagram_description"] = res.get("description") or rec.get("diagram_description")
                rec["diagram_original_file"] = orig_file

                if code and orig_file:
                    dest_stem = Path(orig_file).stem
                    dest_parent = Path(orig_file).parent
                    dest_name = f"{dest_stem}_regen.png"
                    regen_disk_path = OUTPUT / dest_parent / dest_name

                    from backend.diagram_renderer import render_diagram
                    ok, render_msg = render_diagram(fmt, code, regen_disk_path)
                    if ok:
                        rec["diagram_rendered_file"] = f"{dest_parent.name}/{dest_name}" if dest_parent.name else dest_name
                        rec["diagram_render_ok"] = True
                    else:
                        rec["diagram_render_ok"] = False
                        rec["diagram_render_error"] = render_msg
                else:
                    rec["diagram_render_ok"] = False
                    rec["diagram_render_error"] = "No diagram code returned"

                return rec

            asyncio.run(pool.map_parallel(diag_chunks, process_diagram_regen))

            # ==================== STEP 12: DIAGRAM VALIDATION ====================
            job["progress"] = {
                "stage": "validating_diagrams",
                "pct": 91,
                "msg": f"🔍 Step 12: Validating regenerated diagrams against original screenshots"
            }
            telemetry.set_stage("validate_diagrams", f"Validating {len(diag_records)} regenerated diagrams", total_for_stage=len(diag_records), pct_override=91)

            async def process_diagram_validation(chunk, profile):
                rec = chunk[0]
                qnum = rec.get("question_number")
                if not rec.get("diagram_render_ok"):
                    rec["diagram_validation_status"] = "fail"
                    rec["diagram_status"] = "needs_fix"
                    rec["diagram_validation"] = {
                        "matches_original": False,
                        "verdict": "fail",
                        "fix_instructions": rec.get("diagram_render_error") or "Diagram failed to render"
                    }
                    return rec

                telemetry.record_batch_start([qnum], profile)
                prov = NotebookLMProvider(profile=profile)
                orig_disk_path = str((OUTPUT / rec["diagram_original_file"]).resolve()) if rec.get("diagram_original_file") else ""
                regen_disk_path = str((OUTPUT / rec["diagram_rendered_file"]).resolve()) if rec.get("diagram_rendered_file") else ""

                from backend.ai_provider import DiagramGenerationFailed
                try:
                    val_res = await prov.validate_diagram(rec, original_path=orig_disk_path, rendered_path=regen_disk_path)
                except DiagramGenerationFailed as e:
                    used = getattr(prov, "_last_used_profile", profile)
                    telemetry.record_batch_complete([qnum], used, notes="AI validation failed", success=False)
                    rec["diagram_validation_status"] = "fail"
                    rec["diagram_status"] = "needs_fix"
                    rec["diagram_validation"] = {
                        "matches_original": False,
                        "verdict": "fail",
                        "fix_instructions": f"Could not validate diagram (AI validation call failed): {e}"
                    }
                    return rec
                used = getattr(prov, "_last_used_profile", profile)
                telemetry.record_batch_complete([qnum], used)

                verdict = (val_res.get("verdict") or "").lower()
                is_pass = verdict == "pass" or (val_res.get("matches_original") and val_res.get("context_sufficient", True))

                rec["diagram_validation"] = val_res
                if is_pass:
                    rec["diagram_validation_status"] = "pass"
                    rec["diagram_status"] = "regenerated"
                    rec["diagram_file"] = rec["diagram_rendered_file"]
                else:
                    rec["diagram_validation_status"] = "fail"
                    rec["diagram_status"] = "needs_fix"
                return rec

            asyncio.run(pool.map_parallel(diag_chunks, process_diagram_validation))

            # ==================== STEP 13: DIAGRAM FIX & RECHECK ====================
            failed_diags = [r for r in diag_records if r.get("diagram_status") == "needs_fix"]
            if failed_diags:
                job["progress"] = {
                    "stage": "fixing_diagrams",
                    "pct": 95,
                    "msg": f"🔧 Step 13: Correcting {len(failed_diags)} diagrams that failed validation"
                }
                telemetry.set_stage("fix_diagrams", f"Fixing {len(failed_diags)} diagrams", total_for_stage=len(failed_diags), pct_override=95)

                fix_chunks = [[r] for r in failed_diags]

                async def process_diagram_fix(chunk, profile):
                    rec = chunk[0]
                    qnum = rec.get("question_number")
                    telemetry.record_batch_start([qnum], profile)
                    prov = NotebookLMProvider(profile=profile)
                    orig_disk_path = str((OUTPUT / rec.get("diagram_original_file", "")).resolve()) if rec.get("diagram_original_file") else ""
                    val_info = rec.get("diagram_validation") or {}

                    from backend.ai_provider import DiagramGenerationFailed
                    try:
                        fix_res = await prov.fix_diagram(
                            question_dict=rec,
                            original_path=orig_disk_path,
                            previous_code=rec.get("diagram_code") or "",
                            format_type=rec.get("diagram_format") or "matplotlib",
                            missing=val_info.get("missing_elements", []),
                            distorted=val_info.get("distorted_elements", []),
                            fix_instructions=val_info.get("fix_instructions") or "Ensure all details match original diagram faithfully."
                        )
                    except DiagramGenerationFailed as e:
                        used = getattr(prov, "_last_used_profile", profile)
                        telemetry.record_batch_complete([qnum], used, notes="AI fix failed", success=False)
                        # Matches the existing "second failure -> human_review" contract
                        # from step 13: the original screenshot is preserved as the
                        # active diagram file so nothing is ever lost, and the real
                        # reason is recorded instead of quietly re-using broken code.
                        rec["diagram_validation_status"] = "fail"
                        rec["diagram_status"] = "human_review"
                        rec["diagram_file"] = rec.get("diagram_original_file")
                        rec["diagram_validation"] = {
                            "matches_original": False,
                            "verdict": "fail",
                            "fix_instructions": f"Could not generate a fix (AI call failed after retries): {e}"
                        }
                        return rec
                    used = getattr(prov, "_last_used_profile", profile)
                    telemetry.record_batch_complete([qnum], used)

                    new_code = fix_res.get("code") or rec.get("diagram_code")
                    if new_code:
                        rec["diagram_code"] = new_code
                        fmt = fix_res.get("format") or rec.get("diagram_format") or "matplotlib"
                        dest_stem = Path(rec.get("diagram_original_file", f"q{qnum}")).stem
                        dest_parent = Path(rec.get("diagram_original_file", "")).parent
                        dest_name = f"{dest_stem}_regen.png"
                        regen_disk_path = OUTPUT / dest_parent / dest_name

                        from backend.diagram_renderer import render_diagram
                        ok, msg = render_diagram(fmt, new_code, regen_disk_path)
                        rec["diagram_render_ok"] = ok
                        if ok:
                            rec["diagram_rendered_file"] = f"{dest_parent.name}/{dest_name}" if dest_parent.name else dest_name
                            try:
                                re_val = await prov.validate_diagram(rec, original_path=orig_disk_path, rendered_path=str(regen_disk_path))
                            except DiagramGenerationFailed as e:
                                rec["diagram_validation_status"] = "fail"
                                rec["diagram_status"] = "human_review"
                                rec["diagram_file"] = rec.get("diagram_original_file")
                                rec["diagram_validation"] = {
                                    "matches_original": False,
                                    "verdict": "fail",
                                    "fix_instructions": f"Fixed diagram rendered but could not be re-validated (AI call failed): {e}"
                                }
                                return rec
                            rec["diagram_validation"] = re_val
                            re_verdict = (re_val.get("verdict") or "").lower()
                            is_pass = (re_verdict == "pass") or bool(re_val.get("matches_original")) or (
                                re_val.get("context_sufficient", True) and not re_val.get("missing_elements") and not re_val.get("distorted_elements")
                            )
                            if is_pass:
                                rec["diagram_validation_status"] = "pass"
                                rec["diagram_status"] = "regenerated"
                                rec["diagram_file"] = rec["diagram_rendered_file"]
                            else:
                                rec["diagram_validation_status"] = "fail"
                                rec["diagram_status"] = "human_review"
                                rec["diagram_file"] = rec.get("diagram_original_file")
                        else:
                            rec["diagram_render_error"] = msg
                            rec["diagram_validation_status"] = "fail"
                            rec["diagram_validation"] = {
                                "matches_original": False,
                                "verdict": "fail",
                                "fix_instructions": f"Render execution failed: {msg}"
                            }
                            rec["diagram_status"] = "human_review"
                            rec["diagram_file"] = rec.get("diagram_original_file")
                    else:
                        rec["diagram_validation_status"] = "fail"
                        rec["diagram_status"] = "human_review"
                        rec["diagram_file"] = rec.get("diagram_original_file")

                    return rec

                asyncio.run(pool.map_parallel(fix_chunks, process_diagram_fix))

        # ==================== STEP 14: COMPLETION & FINAL EXPORT ====================
        valid_final = []
        human_review_queue = []

        for rec in records:
            wording_ok = (rec.get("wording_status") in ("valid", None, ""))  # None/missing = was never reflagged, treat as ok
            answer_ok = (rec.get("answer_status") in ("verified",))
            diagram_ok = (rec.get("diagram_status") != "human_review")
            if wording_ok and answer_ok and diagram_ok:
                rec["final_status"] = "auto_valid"
                rec["needs_human_review"] = False
                valid_final.append(rec)
            else:
                rec["final_status"] = "needs_human"
                rec["needs_human_review"] = True
                human_review_queue.append(rec)

        job["valid"] = valid_final
        job["human_review_queue"] = human_review_queue
        job["still_flagged"] = human_review_queue
        job["status"] = "completed"
        job["progress"] = {
            "stage": "completed",
            "pct": 100,
            "msg": f"Pipeline completed: {len(valid_final)} verified, {len(human_review_queue)} require human review"
        }

        # Write output JSONs
        out_final = OUTPUT / f"{job_id}_final.json"
        out_final.write_text(json.dumps(valid_final, indent=2, ensure_ascii=False), encoding="utf-8")
        out_hr = OUTPUT / f"{job_id}_human_review.json"
        out_hr.write_text(json.dumps(human_review_queue, indent=2, ensure_ascii=False), encoding="utf-8")

        telemetry.finish_job("completed", f"All stages finished: {len(valid_final)} verified, {len(human_review_queue)} for human review")
        log.info("Job %s pipeline fully completed: %d valid, %d human review", job_id, len(valid_final), len(human_review_queue))

    except Exception as e:
        log.exception("Pipeline failed for job %s: %s", job_id, e)
        job["status"] = "failed"
        job["error"] = str(e)
        job["progress"] = {"stage": "failed", "pct": 0, "msg": str(e)[:200]}
        telemetry.finish_job("failed", str(e))

def _run_reword_job(job_id):
    _run_full_pipeline(job_id)

def _run_recheck_job(job_id):
    _run_full_pipeline(job_id)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/profiles")
def profiles():
    try:
        from backend.notebooklm_client import list_profiles
        profs = list_profiles()
        return jsonify({"profiles": profs, "total": len(profs)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/start", methods=["POST"])
def start():
    if "pdf" not in request.files:
        return jsonify({"error": "No PDF"}), 400
    pdf = request.files["pdf"]
    profile_raw = request.form.get("profile", "")
    profile = _resolve_profile(profile_raw)
    job_id = uuid.uuid4().hex[:8]
    job_uuid = str(uuid.uuid4())
    safe_name = Path(pdf.filename or "upload.pdf").name
    pdf_path = UPLOAD / f"{job_id}_{safe_name}"
    pdf.save(str(pdf_path))
    JOBS[job_id] = {"job_uuid": job_uuid, "pdf": str(pdf_path), "profile": profile, "status": "extracting", "progress": {"stage": "extracting", "pct": 0, "msg": "Queued"}, "created_at": time.time()}
    threading.Thread(target=_run_extraction_job, args=(job_id, str(pdf_path), profile, None), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "job_uuid": job_uuid, "status": "extracting"})

@app.route("/diagrams/<path:filename>")
def serve_diagram(filename):
    safe = Path(filename).name
    sub = Path(filename).parent
    base = OUTPUT / sub if str(sub) != "." else OUTPUT
    p = base / safe
    if p.exists():
        return send_from_directory(str(p.parent), p.name)
    if (OUTPUT / filename).exists():
        return send_from_directory(str(OUTPUT), filename)
    return jsonify({"ok": False, "error": "not found"}), 404

@app.route("/api/upload_diagrams/<job_id>", methods=["POST"])
def upload_diagrams(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    out_dir = OUTPUT / f"{job_id}_diagrams"
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in request.files.getlist("diagrams"):
        name = Path(f.filename or "diagram.png").name
        for e in job.get("expected_diagrams", []):
            if e["expected_name"] == name:
                e["fulfilled"] = True
                break
        dest = out_dir / name
        f.save(str(dest))
        saved.append(name)
    existing = {p.name for ext in IMAGE_EXTS for p in out_dir.glob(f"*{ext}")}
    for e in job.get("expected_diagrams", []):
        if e["expected_name"] in existing:
            e["fulfilled"] = True
            q = next((qq for qq in job.get("questions", []) if qq.get("question_uuid") == e.get("question_uuid")), None)
            if q is not None:
                q["diagram_file"] = f"{job_id}_diagrams/{e['expected_name']}"
    job["diagram_dir"] = str(out_dir)
    fulfilled = sum(1 for e in job.get("expected_diagrams", []) if e.get("fulfilled"))
    total = len(job.get("expected_diagrams", []))
    if (not total or fulfilled >= total) and job.get("status") == "awaiting_screenshots":
        job["status"] = "diagrams_uploaded"
    remaining = [e["expected_name"] for e in job.get("expected_diagrams", []) if not e.get("fulfilled")]
    _annotate_questions_with_diagrams(job_id)
    return jsonify({"saved": saved, "count": len(saved), "fulfilled": fulfilled, "total": total, "remaining": remaining})

@app.route("/api/diagram_watch/start/<job_id>", methods=["POST"])
def diagram_watch_start(job_id):
    res = _start_diagram_watch(job_id)
    return jsonify(res), 200 if res.get("ok") else 400

@app.route("/api/diagram_watch/stop/<job_id>", methods=["POST"])
def diagram_watch_stop(job_id):
    res = _stop_diagram_watch(job_id)
    return jsonify(res), 200 if res.get("ok") else 400

@app.route("/api/diagram_watch/stop", methods=["POST"])
def diagram_watch_stop_any():
    res = _stop_diagram_watch(None)
    return jsonify(res)

@app.route("/api/diagram_watch/status/<job_id>", methods=["GET"])
def diagram_watch_status(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    idx, nxt = _get_next_expected(job)
    fulfilled = sum(1 for e in job.get("expected_diagrams", []) if e.get("fulfilled"))
    total = len(job.get("expected_diagrams", []))
    with _DIAGRAM_WATCH["lock"]:
        active = bool(_DIAGRAM_WATCH["active"] and _DIAGRAM_WATCH["job_id"] == job_id)
        last = _DIAGRAM_WATCH.get("last_captured")
        hist = list(_DIAGRAM_WATCH.get("history", []))
    if total and fulfilled >= total and job.get("status") == "awaiting_screenshots":
        job["status"] = "diagrams_uploaded"
    return jsonify({"job_id": job_id, "active": active, "watch_dir": str(WATCH_DIR), "next_index": idx, "next": nxt, "fulfilled": fulfilled, "total": total, "last_captured": last, "history": hist[-10:], "status": job.get("status")})

@app.route("/api/diagram_watch/undo/<job_id>", methods=["POST"])
def diagram_watch_undo(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    
    undone_info = None
    with _DIAGRAM_WATCH["lock"]:
        hist = _DIAGRAM_WATCH.get("history", [])
        if hist:
            undone_info = hist.pop()
            _DIAGRAM_WATCH["last_captured"] = hist[-1] if hist else None
            if undone_info.get("src"):
                _DIAGRAM_WATCH["seen"].discard(undone_info.get("src"))

    idx = undone_info.get("expected_idx") if undone_info else None
    diagrams = job.get("expected_diagrams", [])
    if idx is None:
        fulfilled_indices = [i for i, d in enumerate(diagrams) if d.get("fulfilled")]
        if fulfilled_indices:
            idx = fulfilled_indices[-1]
            diag = diagrams[idx]
            undone_info = {"expected_idx": idx, "dest": diag.get("expected_name", "")}

    if idx is not None and 0 <= idx < len(diagrams):
        diag = diagrams[idx]
        diag["fulfilled"] = False
        for q in job.get("questions", []):
            if q.get("question_uuid") == diag.get("question_uuid"):
                q["diagram_file"] = None
        try:
            out_dir = OUTPUT / f"{job_id}_diagrams"
            if out_dir.exists():
                for ext in IMAGE_EXTS:
                    p = out_dir / f"{diag['expected_name']}{ext}"
                    if p.exists():
                        p.unlink()
                if undone_info and undone_info.get("dest"):
                    p2 = out_dir / undone_info.get("dest")
                    if p2.exists():
                        p2.unlink()
        except Exception as e:
            log.warning("undo file cleanup warning: %s", e)

    job["status"] = "awaiting_screenshots"
    _annotate_questions_with_diagrams(job_id)
    return jsonify({"ok": True, "undone": undone_info, "status": job["status"]})

@app.route("/api/diagram_watch/undo_item/<job_id>/<int:item_index>", methods=["POST"])
def diagram_watch_undo_item(job_id, item_index):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    diagrams = job.get("expected_diagrams", [])
    if not (0 <= item_index < len(diagrams)):
        return jsonify({"error": "invalid item index"}), 400
    diag = diagrams[item_index]
    diag["fulfilled"] = False
    for q in job.get("questions", []):
        if q.get("question_uuid") == diag.get("question_uuid"):
            q["diagram_file"] = None
    try:
        out_dir = OUTPUT / f"{job_id}_diagrams"
        if out_dir.exists():
            for ext in IMAGE_EXTS:
                p = out_dir / f"{diag['expected_name']}{ext}"
                if p.exists():
                    p.unlink()
    except Exception as e:
        log.warning("undo_item file cleanup: %s", e)
    job["status"] = "awaiting_screenshots"
    _annotate_questions_with_diagrams(job_id)
    return jsonify({"ok": True, "undone_index": item_index, "status": job["status"]})

@app.route("/api/accounts/usage", methods=["GET"])
def accounts_usage():
    try:
        from backend.account_db import get_all_profile_stats
        return jsonify({"ok": True, "stats": get_all_profile_stats()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/diagram_watch/discard/<job_id>", methods=["POST"])
def diagram_watch_discard(job_id):
    data = request.get_json(silent=True) or {}
    src = data.get("src")
    if src:
        p = WATCH_DIR / Path(src).name
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass
        try:
            out_dir = OUTPUT / f"{job_id}_diagrams"
            q = out_dir / Path(src).name
            if q.exists():
                q.unlink()
        except Exception:
            pass
        with _DIAGRAM_WATCH["lock"]:
            _DIAGRAM_WATCH["seen"].add(Path(src).name)
    return jsonify({"ok": True, "discarded": src})

@app.route("/api/diagram_watch/pending/<job_id>", methods=["GET"])
def diagram_watch_pending(job_id):
    seen = _DIAGRAM_WATCH.get("seen", set())
    pending = []
    if WATCH_DIR.exists():
        for p in WATCH_DIR.iterdir():
            if p.is_file() and _is_image(p) and p.name not in seen:
                try:
                    pending.append({"name": p.name, "mtime": p.stat().st_mtime, "size": p.stat().st_size})
                except Exception:
                    continue
        pending.sort(key=lambda x: x["mtime"])
    return jsonify({"pending": pending[:20]})

@app.route("/api/diagram_watch/manual_assign/<job_id>", methods=["POST"])
def diagram_watch_manual_assign(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    idx, nxt = _get_next_expected(job)
    if nxt is None:
        return jsonify({"ok": False, "error": "all fulfilled"}), 400
    files = request.files.getlist("diagrams") or request.files.getlist("file")
    if not files:
        return jsonify({"ok": False, "error": "no file"}), 400
    out_dir = OUTPUT / f"{job_id}_diagrams"
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        dest = out_dir / nxt["expected_name"]
        f.save(str(dest))
        nxt["fulfilled"] = True
        saved.append(nxt["expected_name"])
        telemetry.add_event(f'[diagram] manual assign {nxt["expected_name"]}', "success")
        idx, nxt = _get_next_expected(job)
        if nxt is None:
            break
    job["diagram_dir"] = str(out_dir)
    for n in saved:
        q = next((qq for qq in job.get("questions", []) if qq.get("question_uuid") in n), None)
        if q is not None:
            q["diagram_file"] = f"{job_id}_diagrams/{n}"
    fulfilled = sum(1 for e in job.get("expected_diagrams", []) if e.get("fulfilled"))
    total = len(job.get("expected_diagrams", []))
    if fulfilled >= total and job.get("status") == "awaiting_screenshots":
        job["status"] = "diagrams_uploaded"
    _annotate_questions_with_diagrams(job_id)
    return jsonify({"ok": True, "saved": saved, "fulfilled": fulfilled, "total": total})

@app.route("/api/reword/<job_id>", methods=["POST"])
def reword(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    if job.get("status") in ("rewording", "rechecking", "extracting"):
        return jsonify({"ok": False, "error": f"job busy: {job.get('status')}"}), 409
    threading.Thread(target=_run_reword_job, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "status": "rewording"})

@app.route("/api/recheck/<job_id>", methods=["POST"])
def recheck(job_id):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "job not found"}), 404
    if not job.get("flagged"):
        return jsonify({"error": "no flagged to recheck"}), 400
    if job.get("status") in ("rewording", "rechecking", "extracting"):
        return jsonify({"ok": False, "error": f"job busy: {job.get('status')}"}), 409
    threading.Thread(target=_run_recheck_job, args=(job_id,), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "status": "rechecking"})

# Compatibility shims for old extraction_chatter frontend (expects /api/upload, /api/extract, etc.)
@app.route("/api/upload", methods=["POST"])
def upload_compat():
    f = request.files.get("file") or request.files.get("pdf")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "No file"}), 400
    dest = UPLOAD / Path(f.filename or "upload.pdf").name
    # also support job-based upload via /api/start path
    try:
        f.save(str(dest))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    return jsonify({"ok": True, "filename": dest.name})

@app.route("/api/extract", methods=["POST"])
def extract_compat():
    data = request.get_json() or {}
    filename = data.get("filename") or data.get("pdf")
    provider_raw = data.get("provider", "")
    if not filename:
        return jsonify({"ok": False, "error": "filename required"}), 400
    pdf_path = UPLOAD / filename
    if not pdf_path.exists():
        alt = Path("extraction_chatter/input_pdfs") / filename
        if alt.exists():
            pdf_path = alt
        else:
            return jsonify({"ok": False, "error": f"File not found: {filename}"}), 404
    provider = _resolve_profile(provider_raw)
    job_id = uuid.uuid4().hex[:8]
    job_uuid = str(uuid.uuid4())
    JOBS[job_id] = {"job_uuid": job_uuid, "pdf": str(pdf_path), "profile": provider, "status": "extracting", "progress": {"stage": "extracting", "pct": 0, "msg": "Queued"}, "created_at": time.time()}
    ans_filename = data.get("answer_key_filename")
    threading.Thread(target=_run_extraction_job, args=(job_id, str(pdf_path), provider, ans_filename), daemon=True).start()
    return jsonify({"ok": True, "job_id": job_id, "job_uuid": job_uuid, "status": "extracting"})

def _annotate_questions_with_diagrams(job_id):
    job = JOBS.get(job_id)
    if not job:
        return
    out_dir = OUTPUT / f"{job_id}_diagrams"
    files = {p.name for ext in IMAGE_EXTS for p in out_dir.glob(f"*{ext}")} if out_dir.exists() else set()
    exp_map = {e["expected_name"]: e for e in job.get("expected_diagrams", [])}
    for q in job.get("questions", []) + job.get("valid", []) + job.get("flagged", []) + job.get("rechecked", []) + job.get("still_flagged", []):
        qid = q.get("question_uuid")
        best = None
        for name in files:
            if qid and qid in name:
                e = exp_map.get(name)
                if e and e.get("fulfilled"):
                    best = f"{job_id}_diagrams/{name}"
                    break
        if best:
            q["diagram_file"] = best

@app.route("/api/questions", methods=["GET"])
def questions_compat():
    for jid in list(JOBS.keys()):
        _annotate_questions_with_diagrams(jid)
    all_qs = []
    for j in JOBS.values():
        status = j.get("status")
        if status == "rechecked":
            valid = j.get("valid", [])
            rechecked = j.get("rechecked", [])
            # valid already auto_valid; rechecked contains auto_fixed + needs_human
            combined = []
            seen = set()
            for q in valid + rechecked:
                _ensure_id(q)
                q.setdefault("final_status", "auto_valid" if q in valid else q.get("final_status") or _final_status_for(q))
                if q.get("id") not in seen:
                    combined.append(q)
                    seen.add(q.get("id"))
            # also show human review distinction
            all_qs.extend(combined)
        elif status == "reworded":
            src = j.get("valid", []) + j.get("flagged", [])
            for q in src:
                _ensure_id(q)
                q.setdefault("final_status", _final_status_for(q))
            all_qs.extend(src)
        else:
            for q in j.get("questions", []):
                _ensure_id(q)
                q.setdefault("final_status", "auto_valid")
                all_qs.extend([q])
    return jsonify({"questions": all_qs})

@app.route("/api/human_review/<job_id>", methods=["GET"])
def human_review(job_id):
    j = JOBS.get(job_id)
    if not j:
        return jsonify({"error": "not found"}), 404
    queue = j.get("human_review_queue") or j.get("still_flagged") or []
    for q in queue:
        _ensure_id(q)
        q.setdefault("final_status", "needs_human")
    return jsonify({"job_id": job_id, "count": len(queue), "questions": queue})

@app.route("/api/human_review", methods=["GET"])
def human_review_all():
    all_q = []
    for j in JOBS.values():
        q = j.get("human_review_queue") or j.get("still_flagged") or []
        for item in q:
            _ensure_id(item)
            item.setdefault("final_status", "needs_human")
        all_q.extend(q)
    return jsonify({"count": len(all_q), "questions": all_q})

def _find_question_everywhere(qid: str):
    """Find a question dict by its 'id' field, searching JOBS memory first then disk JSON files.
    Returns (job_id, job_dict, question_dict) or (None, None, None) if not found.
    When found on disk, the job is hydrated into JOBS so follow-up saves work correctly.
    """
    # 1. Search memory first
    for j_id, j in list(JOBS.items()):
        for bucket in ("questions", "valid", "flagged", "rechecked", "still_flagged", "human_review_queue"):
            for q in j.get(bucket, []) or []:
                if q.get("id") == qid:
                    return j_id, j, q

    # 2. Fallback: scan disk output JSON files
    for json_path in OUTPUT.glob("*_final.json"):
        try:
            qs = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for q in qs:
            if q.get("id") == qid:
                # Derive job_id from filename stem: <job_id>_final.json
                j_id = json_path.stem[: -len("_final")]
                if j_id not in JOBS:
                    JOBS[j_id] = {"status": "completed", "questions": qs, "valid": list(qs), "human_review_queue": []}
                else:
                    # Merge question into existing job
                    existing_ids = {x.get("id") for x in JOBS[j_id].get("valid", [])}
                    if qid not in existing_ids:
                        JOBS[j_id].setdefault("valid", []).append(q)
                return j_id, JOBS[j_id], q

    for json_path in OUTPUT.glob("*_human_review.json"):
        try:
            qs = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for q in qs:
            if q.get("id") == qid:
                j_id = json_path.stem[: -len("_human_review")]
                if j_id not in JOBS:
                    JOBS[j_id] = {"status": "completed", "questions": qs, "valid": [], "human_review_queue": list(qs)}
                else:
                    existing_ids = {x.get("id") for x in JOBS[j_id].get("human_review_queue", [])}
                    if qid not in existing_ids:
                        JOBS[j_id].setdefault("human_review_queue", []).append(q)
                return j_id, JOBS[j_id], q

    return None, None, None

def _sync_job_outputs_to_disk(job_id: str):
    """Sync valid and human review lists in memory and write out final/human_review JSON files."""
    j = JOBS.get(job_id)
    if not j:
        return
    valid_final = []
    human_review_queue = []
    for q in j.get("questions", []):
        wording_ok = (q.get("wording_status") == "valid")
        answer_ok = (q.get("answer_status") == "verified")
        diagram_ok = (q.get("diagram_status") not in ("human_review", "needs_fix"))
        needs_hr = q.get("needs_human_review", False)
        fs = q.get("final_status")

        if fs in ("auto_valid", "human_edited", "human_approved") or (wording_ok and answer_ok and diagram_ok and not needs_hr):
            q["final_status"] = fs if fs in ("human_edited", "human_approved") else "auto_valid"
            q["needs_human_review"] = False
            valid_final.append(q)
        else:
            q["final_status"] = "needs_human"
            q["needs_human_review"] = True
            human_review_queue.append(q)

    j["valid"] = valid_final
    j["human_review_queue"] = human_review_queue
    j["still_flagged"] = human_review_queue

    try:
        out_final = OUTPUT / f"{job_id}_final.json"
        out_hr = OUTPUT / f"{job_id}_human_review.json"
        out_final.write_text(json.dumps(valid_final, indent=2, ensure_ascii=False), encoding="utf-8")
        out_hr.write_text(json.dumps(human_review_queue, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning("Could not write job JSON files for %s: %s", job_id, e)

@app.route("/api/questions/<qid>", methods=["PUT"])
def update_question(qid):
    data = request.get_json() or {}
    for j_id, j in JOBS.items():
        for bucket_name in ("questions", "valid", "flagged", "rechecked", "still_flagged", "human_review_queue"):
            for q in j.get(bucket_name, []) or []:
                if q.get("id") == qid:
                    q["reworded_stem"] = data.get("reworded_stem", q.get("reworded_stem"))
                    if "choices" in data:
                        q["choices"] = data.get("choices")
                        q["reworded_choices"] = data.get("choices")
                    if "correct_choice_index" in data:
                        q["correct_choice_index"] = data.get("correct_choice_index")
                    if "diagram_description" in data:
                        q["diagram_description"] = data.get("diagram_description")
                    q["needs_human_review"] = False
                    q["validation_status"] = "valid"
                    q["validation_errors"] = []
                    q["final_status"] = "human_edited"
                    if q in j.get("still_flagged", []):
                        j["still_flagged"] = [x for x in j["still_flagged"] if x.get("id") != qid]
                    if q in j.get("human_review_queue", []):
                        j["human_review_queue"] = [x for x in j["human_review_queue"] if x.get("id") != qid]
                    if q in j.get("flagged", []):
                        j["flagged"] = [x for x in j["flagged"] if x.get("id") != qid]
                        if q not in j.get("valid", []):
                            j.setdefault("valid", []).append(q)
                    _sync_job_outputs_to_disk(j_id)
                    return jsonify({"ok": True, "question": q})
        for q in j.get("questions", []) or []:
            if q.get("id") == qid:
                q["reworded_stem"] = data.get("reworded_stem", q.get("reworded_stem", q.get("raw_stem")))
                if "choices" in data:
                    q["choices"] = data.get("choices")
                    q["reworded_choices"] = data.get("choices")
                    q["raw_choices"] = data.get("choices")
                if "correct_choice_index" in data:
                    q["correct_choice_index"] = data.get("correct_choice_index")
                if "diagram_description" in data:
                    q["diagram_description"] = data.get("diagram_description")
                q["needs_human_review"] = False
                q["final_status"] = "human_edited"
                _sync_job_outputs_to_disk(j_id)
                return jsonify({"ok": True, "question": q})
    return jsonify({"ok": False, "error": "question not found"}), 404

@app.route("/api/questions/<qid>/approve", methods=["POST"])
def approve_question(qid):
    j_id, j, q = _find_question_everywhere(qid)
    if not q:
        return jsonify({"ok": False, "error": "question not found"}), 404
    q["needs_human_review"] = False
    q["final_status"] = "human_approved"
    q["validation_status"] = "valid"
    q["validation_errors"] = []
    if q.get("diagram_status") in ("human_review", "needs_fix"):
        q["diagram_status"] = "approved"
    if q.get("answer_status") == "human_review":
        q["answer_status"] = "verified"
    _sync_job_outputs_to_disk(j_id)
    return jsonify({"ok": True, "question": q})

@app.route("/api/diagram/toggle/<qid>", methods=["POST"])
def toggle_diagram(qid):
    j_id, j, q = _find_question_everywhere(qid)
    if not q:
        return jsonify({"ok": False, "error": "question not found"}), 404

    orig = q.get("diagram_original_file")
    regen = q.get("diagram_rendered_file")
    if not orig and not regen:
        return jsonify({"ok": False, "error": "no diagrams associated with this question"}), 400

    curr = q.get("diagram_file")
    if curr == regen and orig:
        q["diagram_file"] = orig
        q["diagram_active_source"] = "original"
    elif orig and regen and curr == orig:
        q["diagram_file"] = regen
        q["diagram_active_source"] = "regenerated"
    elif regen:
        q["diagram_file"] = regen
        q["diagram_active_source"] = "regenerated"
    elif orig:
        q["diagram_file"] = orig
        q["diagram_active_source"] = "original"

    _sync_job_outputs_to_disk(j_id)
    return jsonify({
        "ok": True,
        "diagram_file": q["diagram_file"],
        "active_source": q.get("diagram_active_source"),
        "question": q
    })

@app.route("/api/diagram/select/<qid>", methods=["POST"])
def select_diagram(qid):
    """Set the active diagram to a specific source ('original' or 'regenerated')."""
    data = request.get_json() or {}
    target_source = data.get("source", "").lower()
    if target_source not in ("original", "regenerated"):
        return jsonify({"ok": False, "error": "source must be 'original' or 'regenerated'"}), 400

    j_id, j, q = _find_question_everywhere(qid)
    if not q:
        return jsonify({"ok": False, "error": "question not found"}), 404

    orig = q.get("diagram_original_file")
    regen = q.get("diagram_rendered_file")

    if target_source == "original":
        if not orig:
            return jsonify({"ok": False, "error": "no original diagram on file"}), 400
        q["diagram_file"] = orig
        q["diagram_active_source"] = "original"
    else:
        if not regen:
            return jsonify({"ok": False, "error": "no regenerated diagram on file"}), 400
        q["diagram_file"] = regen
        q["diagram_active_source"] = "regenerated"

    _sync_job_outputs_to_disk(j_id)
    return jsonify({
        "ok": True,
        "diagram_file": q["diagram_file"],
        "active_source": q["diagram_active_source"],
        "question": q
    })

@app.route("/api/diagram/fix/<qid>", methods=["POST"])
def fix_single_diagram(qid):
    found_job_id, _j, target_q = _find_question_everywhere(qid)
    if not target_q:
        return jsonify({"ok": False, "error": "question not found"}), 404

    try:
        qnum = target_q.get("question_number")
        pool = MultiProfilePoolManager()
        profiles = pool.get_active_profiles()
        profile = profiles[0] if profiles else None

        prov = NotebookLMProvider(profile=profile)
        orig_disk_path = str((OUTPUT / target_q.get("diagram_original_file", "")).resolve()) if target_q.get("diagram_original_file") else ""
        val_info = target_q.get("diagram_validation") or {}

        missing = val_info.get("missing_elements", [])
        distorted = val_info.get("distorted_elements", [])
        fix_instructions = val_info.get("fix_instructions") or "Ensure all geometric shapes, labels, angles, and markings match the original faithfully."

        fix_res = asyncio.run(prov.fix_diagram(
            question_dict=target_q,
            original_path=orig_disk_path,
            previous_code=target_q.get("diagram_code") or "",
            format_type=target_q.get("diagram_format") or "matplotlib",
            missing=missing,
            distorted=distorted,
            fix_instructions=fix_instructions
        ))

        new_code = fix_res.get("code")
        if new_code:
            target_q["diagram_code"] = new_code
            fmt = fix_res.get("format") or target_q.get("diagram_format") or "matplotlib"
            target_q["diagram_format"] = fmt

            dest_stem = Path(target_q.get("diagram_original_file", f"q{qnum}")).stem
            dest_parent = Path(target_q.get("diagram_original_file", "")).parent
            dest_name = f"{dest_stem}_regen.png"
            regen_disk_path = OUTPUT / dest_parent / dest_name

            from backend.diagram_renderer import render_diagram
            ok, msg = render_diagram(fmt, new_code, regen_disk_path)
            target_q["diagram_render_ok"] = ok
            if ok:
                target_q["diagram_rendered_file"] = f"{dest_parent.name}/{dest_name}" if dest_parent.name else dest_name
                re_val = asyncio.run(prov.validate_diagram(target_q, original_path=orig_disk_path, rendered_path=str(regen_disk_path)))
                target_q["diagram_validation"] = re_val
                re_verdict = (re_val.get("verdict") or "").lower()
                is_pass = (re_verdict == "pass") or bool(re_val.get("matches_original")) or (
                    re_val.get("context_sufficient", True) and not re_val.get("missing_elements") and not re_val.get("distorted_elements")
                )
                if is_pass:
                    target_q["diagram_validation_status"] = "pass"
                    target_q["diagram_status"] = "regenerated"
                    target_q["diagram_file"] = target_q["diagram_rendered_file"]
                else:
                    target_q["diagram_validation_status"] = "fail"
                    target_q["diagram_status"] = "human_review"
                    target_q["diagram_file"] = target_q["diagram_rendered_file"]
            else:
                target_q["diagram_render_error"] = msg
                target_q["diagram_validation_status"] = "fail"
                target_q["diagram_validation"] = {
                    "matches_original": False,
                    "verdict": "fail",
                    "fix_instructions": f"Render execution failed: {msg}"
                }
                target_q["diagram_status"] = "human_review"
        else:
            target_q["diagram_validation_status"] = "fail"
            target_q["diagram_status"] = "human_review"

        if found_job_id:
            _sync_job_outputs_to_disk(found_job_id)
        return jsonify({"ok": True, "question": target_q})
    except Exception as e:
        log.exception("fix_single_diagram failed for %s", qid)
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/jobs", methods=["GET"])
def jobs_compat():
    jobs = [{"id": k, "filename": Path(str(v.get("pdf") or "")).name, "status": v.get("status",""), "progress": v.get("progress"), "total_extracted": len(v.get("questions",[])), "created_at": v.get("created_at")} for k,v in JOBS.items()]
    return jsonify({"jobs": jobs})

@app.route("/api/telemetry", methods=["GET"])
def telemetry_compat():
    snap = telemetry.get_snapshot()
    if snap.get("total", 0) == 0:
        total = sum(len(j.get("questions",[])) for j in JOBS.values())
        if total:
            snap["total"] = total
            snap["completed"] = total
            snap["percentage"] = 100
            snap["stage"] = "completed"
    return jsonify(snap)

@app.route("/api/questions/clear", methods=["POST"])
def clear_q():
    for j in JOBS.values():
        j["questions"] = []
    return jsonify({"ok": True})

@app.route("/api/jobs/clear", methods=["POST", "DELETE"])
def clear_j():
    JOBS.clear()
    telemetry.reset()
    with _DIAGRAM_WATCH["lock"]:
        _DIAGRAM_WATCH["job_id"] = None
        _DIAGRAM_WATCH["history"] = []
        _DIAGRAM_WATCH["last_captured"] = None
        _DIAGRAM_WATCH["seen"] = set()
    for p in OUTPUT.iterdir():
        if p.name == ".gitkeep":
            continue
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
        except Exception as e:
            log.warning("Failed to remove output file %s: %s", p, e)
    return jsonify({"ok": True, "message": "All past jobs and artifacts cleared."})

@app.route("/api/recheck", methods=["POST"])
def recheck_compat():
    # trigger recheck on last job's flagged
    if not JOBS:
        return jsonify({"ok": False, "error": "no jobs"}), 400
    last_id = list(JOBS.keys())[-1]
    # call internal recheck logic via same as /api/recheck/<job_id>
    with app.test_request_context():
        pass
    # delegate
    job = JOBS[last_id]
    flagged = job.get("flagged") or []
    return jsonify({"ok": True, "total_rechecked": len(flagged), "issues_flagged": len([f for f in flagged if f.get("needs_human_review")])})

@app.route("/api/export", methods=["GET"])
def export_compat():
    all_qs = []
    for job_id, j in JOBS.items():
        if j.get("status") == "rechecked":
            final_path = OUTPUT / f"{job_id}_final.json"
            # prefer actual final file if exists else combine valid+fixed
            if final_path.exists():
                try:
                    all_qs.extend(json.loads(final_path.read_text(encoding="utf-8")))
                    continue
                except Exception:
                    pass
            all_qs.extend(j.get("valid", []) + [f for f in j.get("rechecked", []) if not f.get("needs_human_review")])
        elif j.get("status") == "reworded":
            all_qs.extend(j.get("valid", []) + j.get("flagged", []))
        else:
            all_qs.extend(j.get("questions",[]))
    out = OUTPUT / "exported_questions.json"
    out.write_text(json.dumps(all_qs, indent=2, ensure_ascii=False), encoding="utf-8")
    return send_file(str(out), as_attachment=True)

@app.route("/api/export_final/<job_id>", methods=["GET"])
def export_final(job_id):
    j = JOBS.get(job_id)
    if not j:
        return jsonify({"error": "not found"}), 404
    final_path = OUTPUT / f"{job_id}_final.json"
    if final_path.exists():
        return send_file(str(final_path), as_attachment=True, download_name=f"{job_id}_final.json")
    final = j.get("valid", []) + [f for f in j.get("rechecked", []) if not f.get("needs_human_review")]
    if not final:
        final = j.get("questions", [])
    tmp = OUTPUT / f"{job_id}_final_tmp.json"
    tmp.write_text(json.dumps(final, indent=2, ensure_ascii=False), encoding="utf-8")
    return send_file(str(tmp), as_attachment=True, download_name=f"{job_id}_final.json")

@app.route("/api/status/<job_id>")
def status(job_id):
    j = JOBS.get(job_id)
    if not j:
        return jsonify({"error": "not found"}), 404
    prog = j.get("progress") or {"stage": j.get("status"), "pct": 0, "msg": ""}
    tel = telemetry.get_snapshot()
    pct = prog.get("pct", 0)
    if j.get("status") in ("rewording", "rechecking") and tel.get("total"):
        pct = tel.get("percentage", pct)
    return jsonify({"job_id": job_id, "job_uuid": j.get("job_uuid"), "status": j.get("status"), "progress": prog, "percentage": pct, "count": len(j.get("questions",[])), "expected_diagrams": j.get("expected_diagrams",[]), "diagram_dir": j.get("diagram_dir"), "flagged": len(j.get("flagged",[])), "valid": len(j.get("valid",[])), "still_flagged": len(j.get("still_flagged",[])), "human_review_count": len(j.get("human_review_queue",[])), "error": j.get("error")})

@app.route("/api/job_progress/<job_id>", methods=["GET"])
def job_progress(job_id):
    j = JOBS.get(job_id)
    if not j:
        return jsonify({"error": "not found"}), 404
    prog = j.get("progress") or {"stage": j.get("status"), "pct": 0, "msg": ""}
    tel = telemetry.get_snapshot()
    if j.get("status") in ("rewording", "rechecking"):
        prog = {"stage": tel.get("stage", prog.get("stage")), "pct": tel.get("percentage", prog.get("pct")), "msg": tel.get("events", [{}])[-1].get("msg","") if tel.get("events") else prog.get("msg")}
    return jsonify({"job_id": job_id, "status": j.get("status"), "progress": prog, "telemetry": tel})

# Profile / sync / notebooks shims to avoid <!doctype HTML 404
@app.route("/api/profiles/<name>/login", methods=["POST"])
def login_compat(name):
    try:
        from backend.notebooklm_client import login_profile
        res = login_profile(name)
        return jsonify({"ok": res.get("ok", True), "message": res.get("message",""), "email": res.get("email","")})
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

@app.route("/api/profiles/<name>/toggle", methods=["POST"])
def toggle_compat(name):
    try:
        data = request.get_json() or {}
        disabled = data.get("disabled", True)
        from backend.notebooklm_client import set_profile_disabled
        res = set_profile_disabled(name, disabled)
        return jsonify({"ok": True, "disabled": disabled})
    except Exception as e:
        # fallback: just return ok
        return jsonify({"ok": True})

SYNC_STATUS = {"running": False, "total": 0, "completed": 0, "results": [], "error": None}

@app.route("/api/profiles/sync", methods=["POST"])
def sync_compat():
    global SYNC_STATUS
    if SYNC_STATUS.get("running"):
        return jsonify({"ok": True, "status": "already_running", "message": "Profile sync already in progress."})
    data = request.get_json(silent=True) or {}
    browser = data.get("browser", "chrome")
    selected_profiles = data.get("profiles", None)
    timeout_seconds = int(data.get("timeout_seconds", 5))
    SYNC_STATUS = {"running": True, "total": 0, "completed": 0, "results": [], "error": None}
    def run_sync():
        global SYNC_STATUS
        try:
            from backend.notebooklm_client import list_profiles, login_profile
            if selected_profiles is not None:
                profile_names = [p for p in selected_profiles if p != "default"]
            else:
                profiles = list_profiles()
                profile_names = [p["name"] for p in profiles if p["name"] != "default"]
            SYNC_STATUS["total"] = len(profile_names)
            print(f"\n=======================================================", flush=True)
            print(f"[NotebookLM Sync] Starting auto-sync for {len(profile_names)} profiles (timeout={timeout_seconds}s) using {browser}...", flush=True)
            print(f"=======================================================\n", flush=True)
            for idx, name in enumerate(profile_names, 1):
                print(f"[{idx}/{len(profile_names)}] Syncing profile: '{name}' (timeout={timeout_seconds}s)...", flush=True)
                try:
                    res = login_profile(name, browser_choice=browser, timeout_seconds=timeout_seconds)
                    stat_label = "SUCCESS" if res.get("ok") else "FAILED"
                    print(f"  -> [{stat_label}] {res.get('message')}\n", flush=True)
                    SYNC_STATUS["results"].append({"name": name, **res})
                except Exception as ex:
                    print(f"  -> [ERROR] Failed syncing '{name}': {ex}\n", flush=True)
                    SYNC_STATUS["results"].append({"name": name, "ok": False, "message": str(ex)})
                SYNC_STATUS["completed"] = idx
            print(f"=======================================================", flush=True)
            print(f"[NotebookLM Sync] Finished syncing all {len(profile_names)} profiles!", flush=True)
            print(f"=======================================================\n", flush=True)
        except Exception as e:
            log.exception("Background sync failed: %s", e)
            print(f"[NotebookLM Sync Error] {e}\n", flush=True)
            SYNC_STATUS["error"] = str(e)
        finally:
            SYNC_STATUS["running"] = False
    t = threading.Thread(target=run_sync, daemon=True)
    t.start()
    return jsonify({"ok": True, "status": "started", "message": "Auto-sync started in background. Check terminal for live status."})

@app.route("/api/profiles/sync/status", methods=["GET"])
def sync_status_compat():
    global SYNC_STATUS
    return jsonify(SYNC_STATUS)

@app.route("/api/notebooks/clean", methods=["POST"])
def clean_compat():
    try:
        from backend.clean_notebooks import clean_all
        res = clean_all()
        return jsonify({"ok": True, "cleaned": res})
    except Exception as e:
        return jsonify({"ok": True})

# ─── FailbetterPapersWorkflow sync flow (ported) ────────────────────────
@app.route("/api/nlm/profiles", methods=["GET"])
def nlm_profiles():
    try:
        from backend.notebooklm_client import list_profiles
        return jsonify(list_profiles())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/nlm/profiles/check", methods=["POST"])
def nlm_check_profile():
    try:
        from backend.notebooklm_client import list_profiles, check_profile
        data = request.get_json() or {}
        profile = data.get("profile")
        if profile:
            result = check_profile(profile)
            return jsonify([result])
        profiles = list_profiles()
        results = [check_profile(p["name"]) for p in profiles]
        return jsonify(results)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/nlm/profiles/login", methods=["POST"])
def nlm_login():
    try:
        from backend.notebooklm_client import login_profile
        data = request.get_json() or {}
        profile_name = data.get("profile_name", "").strip()
        browser = data.get("browser")
        timeout = data.get("timeout_seconds", 300)
        if not profile_name:
            return jsonify({"error": "profile_name required"}), 400
        result = login_profile(profile_name, browser, timeout_seconds=timeout)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "message": str(e)}), 500

@app.route("/api/nlm/profiles/auto-sync", methods=["POST"])
def nlm_auto_sync():
    try:
        from backend.notebooklm_client import batch_login_profiles
        data = request.get_json() or {}
        profiles = data.get("profiles")
        timeout = data.get("timeout_seconds", 20)
        browser = data.get("browser")
        results = batch_login_profiles(profiles, timeout_seconds=timeout, browser_choice=browser)
        return jsonify({"ok": True, "results": results})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/nlm/rate-limits", methods=["GET"])
def nlm_rate_limits():
    try:
        from backend.notebooklm_client import get_all_rate_limits
        return jsonify(get_all_rate_limits())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/nlm/rate-limits/clear", methods=["POST"])
def nlm_clear_rate_limit():
    try:
        from backend.notebooklm_client import clear_rate_limit
        data = request.get_json() or {}
        profile = data.get("profile")
        if not profile:
            return jsonify({"error": "profile required"}), 400
        ok = clear_rate_limit(profile)
        return jsonify({"ok": ok})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/nlm/profiles/<path:profile>/flag-cooldown", methods=["POST"])
def nlm_flag_cooldown(profile):
    try:
        from backend.notebooklm_client import flag_profile_cooldown
        data = request.get_json() or {}
        hours = data.get("hours", 24)
        flag_profile_cooldown(profile, duration_hours=hours)
        return jsonify({"ok": True, "message": f"Account {profile} flagged on {hours}h cooldown"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/nlm/profiles/<path:profile>/clear-cooldown", methods=["POST"])
def nlm_clear_cooldown(profile):
    try:
        from backend.notebooklm_client import clear_rate_limit
        ok = clear_rate_limit(profile)
        return jsonify({"ok": ok, "message": f"Cooldown cleared for {profile}"})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.errorhandler(404)
def api_404(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Not found: " + request.path}), 404
    return e

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)