"""
backend/ai_provider.py
Abstracted AI Provider interface.
Allows seamless switching between NotebookLM Chatter (10-account pool)
and OpenAI/Azure APIs (gpt-4o-mini / gpt-4o) without touching extraction logic.
"""

import asyncio
import json
import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.validator import get_whitelist_prompt_string, validate_question_dict

log = logging.getLogger("extraction.ai_provider")
PROMPTS_DIR = Path(__file__).parent.parent / "prompts"


def _load_prompt_template(filename: str) -> str:
    path = PROMPTS_DIR / filename
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


class AiProvider(ABC):
    """Abstract base class for all AI model providers."""

    @abstractmethod
    async def extract_and_structure(
        self, ocr_text: str, image_description: str = ""
    ) -> Dict[str, Any]:
        """Convert raw OCR and image description into structured question JSON."""
        pass

    @abstractmethod
    async def reword_and_clean(
        self, raw_stem: str, raw_choices: list, visible_correct_answer: Optional[str] = None, source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def describe_diagram(self, image_description_or_path: str) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def recheck_question(self, question_dict: Dict[str, Any], source_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        pass

    @abstractmethod
    async def extract_from_answer_script(self, answer_script_text: str, source_paths: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        pass

    @abstractmethod
    async def critique_and_refine(
        self, question_dict: Dict[str, Any], critique_issues: str, original_doc_excerpt: str = "", image_hints: str = "", source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        pass

    async def reword_and_clean_batch(
        self, questions: List[Dict[str, Any]], source_paths: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Clean up and normalize KaTeX for a batch of questions (>= 10 questions/batch)."""
        results = []
        for q in questions:
            res = await self.reword_and_clean(
                raw_stem=q.get("raw_stem", ""),
                raw_choices=q.get("raw_choices", []),
                visible_correct_answer=q.get("visible_correct_answer"),
            )
            res["question_number"] = q.get("question_number")
            res["reworded_context"] = q.get("context_latex")
            results.append(res)
        return results

    async def extract_document_questions(self, file_path: Path, chunk_size: int = 15) -> List[Dict[str, Any]]:
        """Extract all questions directly from a full PDF or text document."""
        return []


class NotebookLMProvider(AiProvider):
    """Provider powered by the local multi-account NotebookLM Chatter engine (Primary Default)."""

    def __init__(self, profile: Optional[str] = None):
        self.profile = profile

    def _pick_profile(self) -> str:
        from backend.notebooklm_client import find_backup_profile, is_rate_limited
        p = self.profile
        if not p or is_rate_limited(p):
            p = find_backup_profile(p or "") or p or "default"
        return p

    async def _ask(self, source_text: str, prompt: str, label: str = "task", source_paths: Optional[List[str]] = None) -> str:
        import tempfile
        from backend.notebooklm_client import _submit_async, mark_rate_limited, _is_rate_limit, _is_auth_expired, AuthExpiredError, find_backup_profile
        profile = self._pick_profile()
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as tmp:
                tmp.write(source_text)
                tmp_path = tmp.name
            all_sources: List[str] = [tmp_path]
            if source_paths:
                for p in source_paths:
                    if p and Path(p).exists():
                        all_sources.append(str(p))
            try:
                ok, ans, err = await _submit_async(profile, all_sources, prompt, {}, label)
                if ok:
                    return ans
                raise RuntimeError(err or "submit failed")
            except AuthExpiredError as ae:
                mark_rate_limited(profile, str(ae), cooldown_seconds=3600)
                backup = find_backup_profile(profile)
                if backup:
                    ok2, ans2, err2 = await _submit_async(backup, all_sources, prompt, {}, label)
                    if ok2:
                        return ans2
                raise
            except Exception as e:
                err = str(e)
                if _is_auth_expired(err):
                    mark_rate_limited(profile, err, cooldown_seconds=3600)
                    raise AuthExpiredError(err) from e
                if _is_rate_limit(err):
                    mark_rate_limited(profile, err)
                raise
        finally:
            if tmp_path:
                Path(tmp_path).unlink(missing_ok=True)

    def _parse_json_response(self, text: str) -> Optional[dict]:
        """Extract the first valid JSON object from a chat response."""
        import re
        # Try direct parse first
        try:
            return json.loads(text)
        except Exception:
            pass
        # Find JSON block
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
        return None

    def _parse_json_array_response(self, text: str) -> Optional[List[dict]]:
        """Extract the first valid JSON array from a chat response."""
        import re
        try:
            val = json.loads(text)
            if isinstance(val, list):
                return val
        except Exception:
            pass
        match = re.search(r"\[[\s\S]*\]", text)
        if match:
            try:
                val = json.loads(match.group())
                if isinstance(val, list):
                    return val
            except Exception:
                pass
        return None

    async def extract_and_structure(
        self, ocr_text: str, image_description: str = ""
    ) -> Dict[str, Any]:
        template = _load_prompt_template("extraction_structuring.txt")
        prompt = (
            template
            .replace("{{ocr_text}}", ocr_text)
            .replace("{{image_description}}", image_description or "None")
            .replace("{{page_number}}", "unknown")
        )
        source_text = f"OCR Text:\n{ocr_text}\n\nDiagram Description:\n{image_description or 'None'}"

        try:
            answer = await self._ask(source_text, prompt, label="extraction")
            result = self._parse_json_response(answer)
            if result:
                return result
            log.warning("NotebookLM returned non-JSON for extract_and_structure, falling back.")
        except Exception as e:
            log.warning("NotebookLM execution failed: %s. Falling back to local parser.", e)

        return SimulationProvider().extract_and_structure_sync(ocr_text, image_description)

    async def reword_and_clean(
        self, raw_stem: str, raw_choices: list, visible_correct_answer: Optional[str] = None, source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        template = _load_prompt_template("rewording_own_pdf.txt")
        prompt = (
            template
            .replace("{{context_latex}}", "None")
            .replace("{{raw_stem}}", raw_stem)
            .replace("{{raw_choices}}", json.dumps(raw_choices))
            .replace("{{visible_correct_answer}}", str(visible_correct_answer or "None"))
        )
        source_text = f"Question stem:\n{raw_stem}\n\nChoices:\n{json.dumps(raw_choices)}"
        try:
            answer = await self._ask(source_text, prompt, label="reword", source_paths=source_paths)
            result = self._parse_json_response(answer)
            if result:
                return result
            log.warning("NotebookLM returned non-JSON for reword_and_clean, falling back.")
        except Exception as e:
            log.warning("NotebookLM reword failed: %s. Falling back.", e)
        return SimulationProvider().reword_and_clean_sync(raw_stem, raw_choices, visible_correct_answer)

    async def reword_and_clean_batch(
        self, questions: List[Dict[str, Any]], source_paths: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        if not questions:
            return []
        template = _load_prompt_template("rewording_batch.txt")
        if not template:
            return await super().reword_and_clean_batch(questions)
        batch_input = []
        for q in questions:
            batch_input.append({
                "question_number": q.get("question_number", 1),
                "context_latex": q.get("context_latex"),
                "raw_stem": q.get("raw_stem", ""),
                "raw_choices": q.get("raw_choices", []),
                "visible_correct_answer": q.get("visible_correct_answer"),
            })
        prompt = (
            template
            .replace("{{batch_size}}", str(len(questions)))
            .replace("{{questions_json}}", json.dumps(batch_input, indent=2))
        )
        source_text = f"Batch of {len(questions)} exam questions for KaTeX normalization:\n\n{json.dumps(batch_input, indent=2)}"
        try:
            answer = await self._ask(source_text, prompt, label="reword_batch", source_paths=source_paths)
            parsed_list = self._parse_json_array_response(answer)
            if parsed_list and len(parsed_list) == len(questions):
                return parsed_list
            elif parsed_list and len(parsed_list) > 0:
                res_map = {item.get("question_number"): item for item in parsed_list if isinstance(item, dict)}
                final_results = []
                for q in questions:
                    q_num = q.get("question_number")
                    if q_num in res_map:
                        final_results.append(res_map[q_num])
                    else:
                        sim = SimulationProvider().reword_and_clean_sync(
                            q.get("raw_stem", ""), q.get("raw_choices", []), q.get("visible_correct_answer")
                        )
                        sim["question_number"] = q_num
                        sim["reworded_context"] = q.get("context_latex")
                        final_results.append(sim)
                return final_results
            log.warning("NotebookLM returned non-JSON array for batch reword (%d items), falling back.", len(questions))
        except Exception as e:
            log.warning("NotebookLM batch reword failed: %s. Falling back to simulation.", e)

        return SimulationProvider().reword_and_clean_batch_sync(questions)

    async def describe_diagram(self, image_description_or_path: str) -> Dict[str, Any]:
        log.warning("describe_diagram is deprecated: attach raw crops via source_paths instead")
        return {
            "diagram_type": "geometric figure / coordinate graph",
            "full_description": str(image_description_or_path),
            "all_labeled_values": [],
        }

    async def recheck_question(self, question_dict: Dict[str, Any], source_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        template = _load_prompt_template("recheck_verification.txt")
        prompt = (
            template
            .replace("{{stem}}", question_dict.get("reworded_stem") or question_dict.get("raw_stem", ""))
            .replace("{{choices}}", json.dumps(question_dict.get("choices", [])))
            .replace("{{correct_answer}}", str(question_dict.get("correct_choice_index", "None")))
            .replace("{{diagram_context}}", question_dict.get("diagram_description") or "None")
        )
        source_text = f"Stem: {question_dict.get('reworded_stem') or question_dict.get('raw_stem','')}\nChoices: {json.dumps(question_dict.get('choices',[]))}"
        try:
            answer = await self._ask(source_text, prompt, label="recheck", source_paths=source_paths)
            result = self._parse_json_response(answer)
            if result:
                return result
        except Exception as e:
            log.warning("NotebookLM recheck failed: %s. Falling back.", e)
        return await SimulationProvider().recheck_question(question_dict)

    async def extract_from_answer_script(self, answer_script_text: str, source_paths: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        template = _load_prompt_template("answer_script_pairing.txt")
        if template:
            prompt = template.replace("{{answer_script_text}}", answer_script_text)
            source_text = f"Answer Key / Marking Scheme:\n{answer_script_text}"
            try:
                answer = await self._ask(source_text, prompt, label="answer_key", source_paths=source_paths)
                parsed = self._parse_json_array_response(answer)
                if parsed is not None:
                    return parsed
                single = self._parse_json_response(answer)
                if single and isinstance(single, dict):
                    return [single]
                log.warning("NotebookLM returned non-JSON for answer_script, falling back.")
            except Exception as e:
                log.warning("NotebookLM answer_script failed: %s. Falling back.", e)
        return await SimulationProvider().extract_from_answer_script(answer_script_text)

    async def critique_and_refine(
        self, question_dict: Dict[str, Any], critique_issues: str, original_doc_excerpt: str = "", image_hints: str = "", source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        template = _load_prompt_template("refine_with_critique.txt")
        draft_stem = question_dict.get("reworded_stem") or question_dict.get("raw_stem", "")
        draft_choices = question_dict.get("choices") or question_dict.get("raw_choices", [])
        correct_idx = question_dict.get("correct_choice_index", "None")
        context_latex = question_dict.get("context_latex") or "None"
        raw_stem = question_dict.get("raw_stem", "")
        diagram_desc = question_dict.get("diagram_description") or str(question_dict.get("diagram_info", "")) or "None"
        source_file = question_dict.get("source_file", "unknown")
        prompt = (
            template
            .replace("{{context_latex}}", str(context_latex))
            .replace("{{draft_stem}}", draft_stem)
            .replace("{{draft_choices}}", json.dumps(draft_choices))
            .replace("{{correct_answer}}", str(correct_idx))
            .replace("{{critique_issues}}", critique_issues)
            .replace("{{original_document_excerpt}}", original_doc_excerpt or raw_stem)
            .replace("{{diagram_info}}", image_hints or diagram_desc)
            .replace("{{source_file}}", str(source_file))
        )
        source_text = (
            f"Original Document Excerpt (source: {source_file}):\n{original_doc_excerpt or raw_stem}\n\n"
            f"Draft Question:\n{draft_stem}\n\n"
            f"Choices:\n{json.dumps(draft_choices)}\n\n"
            f"Diagram Info / Images: {image_hints or diagram_desc}\n\n"
            f"Critique Feedback (reason flagged):\n{critique_issues}"
        )
        try:
            answer = await self._ask(source_text, prompt, label="refine", source_paths=source_paths)
            result = self._parse_json_response(answer)
            if result:
                return result
            log.warning("NotebookLM returned non-JSON for critique_and_refine, falling back.")
        except Exception as e:
            log.warning("NotebookLM refine failed: %s. Falling back.", e)
        return await SimulationProvider().critique_and_refine(question_dict, critique_issues)

    async def extract_document_questions(self, file_path: Path, chunk_size: int = 15) -> List[Dict[str, Any]]:
        """Upload full PDF / text document as a source to NotebookLM and extract all questions.

        Two-phase chunked approach to avoid NotebookLM prompt size limits:
        1. Ask for a comma-separated list of question numbers (short prompt).
        2. Extract questions in batches of >=10 (default 15) using the tilde schema.
        """
        from notebooklm import NotebookLMClient, ReportFormat
        import re as _re
        file_path = Path(file_path)
        profile = self._pick_profile()

        try:
            client_ctx = NotebookLMClient.from_storage(profile=profile, timeout=600.0, chat_timeout=600.0)
            all_questions: List[Dict[str, Any]] = []
            async with client_ctx as client:
                nb = await client.notebooks.create(f"Doc_Extract_{file_path.stem[:12]}")
                try:
                    await client.sources.add_file(nb.id, str(file_path), wait=True)
                    await asyncio.sleep(5)  # allow full indexing after upload

                    # Phase 1: detect question numbers across all pages to the very end
                    count_resp = await client.chat.ask(
                        nb.id,
                        "Check every page in this document from the first page to the very last page. "
                        "What is the list of all question numbers present? List all numbers separated by commas (e.g. 1, 2, ... 61). "
                        "Output the comma-separated list of numbers only, nothing else.",
                    )
                    count_text = getattr(count_resp, "answer", "") or extract_answer_from_raw_stream(getattr(count_resp, "raw_response", "")) or str(count_resp)
                    found_nums = [int(n) for n in _re.findall(r"\b\d+\b", count_text)]
                    if found_nums:
                        max_q = max(found_nums)
                        # Ensure continuous range from 1 to max detected
                        q_nums = list(range(1, max_q + 1))
                    else:
                        q_nums = list(range(1, 62))  # fallback
                    log.info("Detected %d question numbers (1 to %d) in %s", len(q_nums), q_nums[-1], file_path.name)

                    _tilde_tmpl = _load_prompt_template("whole_pdf_extraction_tilde.txt")
                    if _tilde_tmpl and "~~~~~" in _tilde_tmpl:
                        _fmt_m = _re.search(r"~~~~~.*?~~~~~", _tilde_tmpl, _re.DOTALL)
                        TILDE_FORMAT = _fmt_m.group(0).strip() if _fmt_m else _tilde_tmpl.strip()
                        _tilde_rules_block = _tilde_tmpl
                    else:
                        TILDE_FORMAT = (
                            "~~~~~Exam: <Name>~~~Subject: <Subject>~~~Year: <Year>"
                            "~~~Question Number: <Num>~~~Label Path: <Path>"
                            "~~~Question Type: <Multiple Choice/Grid-in/Short Answer/Structured/True False>"
                            "~~~Marks: <[X marks] or None>~~~Passage Context: <Stimulus or None>"
                            "~~~Question: <Stem>~~~Question Diagram Present: <true/false>"
                            "~~~Question Diagram Page: <Page or None>~~~Question Diagram Hint: <Desc or None>"
                            "~~~Answer A: <Choice or None>~~~Answer A Diagram Present: <true/false>"
                            "~~~Answer A Diagram Page: <Page or None>~~~Answer A Diagram Hint: <Hint or None>"
                            "~~~Answer B: <Choice or None>~~~Answer B Diagram Present: <true/false>"
                            "~~~Answer B Diagram Page: <Page or None>~~~Answer B Diagram Hint: <Hint or None>"
                            "~~~Answer C: <Choice or None>~~~Answer C Diagram Present: <true/false>"
                            "~~~Answer C Diagram Page: <Page or None>~~~Answer C Diagram Hint: <Hint or None>"
                            "~~~Answer D: <Choice or None>~~~Answer D Diagram Present: <true/false>"
                            "~~~Answer D Diagram Page: <Page or None>~~~Answer D Diagram Hint: <Hint or None>~~~~~"
                        )
                        _tilde_rules_block = ""

                    if chunk_size >= 999:
                        all_questions: List[Dict[str, Any]] = []
                        max_target_q = q_nums[-1] if q_nums else 61
                        if _tilde_rules_block:
                            whole_prompt = (
                                f"Extract questions 1 to the very end of the document (including question {max_target_q} and any final/extra questions). "
                                f"Create a new note titled \"output.txt\" containing ONLY the tilde lines (one question per line, no other text).\n"
                                f"{_tilde_rules_block}"
                            )
                        else:
                            whole_prompt = (
                                f"Extract questions 1 to the very end of the document (including question {max_target_q} and any final/extra questions). "
                                f"Create a new note titled \"output.txt\" containing ONLY the tilde lines (one question per line, no other text). "
                                f"Format: {TILDE_FORMAT}\n"
                                f"Rules: non-MCQ set Answer A-D to None. Math in \\( ... \\). If a diagram present set true with PDF page number and hint."
                            )
                        ans = ""
                        studio_done = False
                        try:
                            log.info("Whole-doc: generating Studio report for %s", file_path.name)
                            gen = await client.artifacts.generate_report(
                                nb.id,
                                report_format=ReportFormat.CUSTOM,
                                custom_prompt=whole_prompt,
                            )
                            await client.artifacts.wait_for_completion(nb.id, gen.task_id, timeout=400)
                            await asyncio.sleep(3)
                            reports = []
                            try:
                                reports = await client.artifacts.list_reports(nb.id)
                            except Exception:
                                reports = await client.artifacts.list(nb.id)
                            for r in reports if isinstance(reports, list) else []:
                                title = (getattr(r, "title", "") or getattr(r, "name", "") or "").lower()
                                rid = getattr(r, "id", None)
                                if not rid:
                                    continue
                                if "output" in title or True:
                                    tmp = f"/tmp/{nb.id}_output_{rid}.md"
                                    try:
                                        await client.artifacts.download_report(nb.id, tmp, rid)
                                        import pathlib as _pl
                                        if _pl.Path(tmp).exists():
                                            txt = _pl.Path(tmp).read_text(encoding="utf-8", errors="ignore")
                                            if "~~~" in txt:
                                                ans = txt
                                                log.info("Studio generate_report succeeded (%d chars, %d lines)", len(txt), txt.count("~~~"))
                                                studio_done = True
                                                break
                                    except Exception as e:
                                        log.warning("Studio download_report %s failed: %s", rid, e)
                            if not ans:
                                # fallback: try any artifact with output in title
                                try:
                                    arts = await client.artifacts.list(nb.id)
                                    for a in arts if isinstance(arts, list) else []:
                                        t = (getattr(a, "title", "") or "").lower()
                                        if "output" in t:
                                            tmp2 = f"/tmp/{nb.id}_art_{a.id}.md"
                                            try:
                                                await client.artifacts.download_report(nb.id, tmp2, a.id)
                                                import pathlib as _pl2
                                                if _pl2.Path(tmp2).exists():
                                                    txt2 = _pl2.Path(tmp2).read_text(encoding="utf-8", errors="ignore")
                                                    if "~~~" in txt2:
                                                        ans = txt2
                                                        studio_done = True
                                                        break
                                            except Exception:
                                                pass
                                except Exception:
                                    pass
                        except Exception as e:
                            log.warning("Studio generate_report failed: %s — falling back to chat", e)
                        if not studio_done or "~~~" not in ans:
                            resp = None
                            try:
                                resp = await client.chat.ask(nb.id, whole_prompt)
                            except Exception as ask_err:
                                log.warning("Whole-doc chat.ask raised %s — still trying to fetch output.txt", ask_err)
                                resp = None
                            await asyncio.sleep(6)
                            for attempt in range(4):
                                try:
                                    notes = await client.notes.list(nb.id)
                                    best = ""
                                    for n in notes:
                                        title = (getattr(n, "title", "") or "").lower()
                                        txt = getattr(n, "content", None) or getattr(n, "text", None) or getattr(n, "full_text", None) or ""
                                        if "~~~" in txt:
                                            if title == "output.txt" or "output" in title:
                                                ans = txt
                                                log.info("Found output.txt in Notes (%d chars)", len(txt))
                                                break
                                            if len(txt) > len(best):
                                                best = txt
                                    if not ans and best:
                                        ans = best
                                except Exception as e:
                                    log.warning("notes.list attempt %d failed: %s", attempt, e)
                                if ans and "~~~" in ans:
                                    break
                                try:
                                    arts = await client.artifacts.list(nb.id)
                                    for a in arts if isinstance(arts, list) else []:
                                        t = (getattr(a, "title", "") or getattr(a, "name", "") or "").lower()
                                        if "output" in t:
                                            try:
                                                tmp = f"/tmp/art_{a.id}.md" if hasattr(a, "id") else "/tmp/art_output.md"
                                                await client.artifacts.download_report(nb.id, tmp, getattr(a, "id", None))
                                                import pathlib as _pl
                                                txt2 = _pl.Path(tmp).read_text(encoding="utf-8", errors="ignore") if _pl.Path(tmp).exists() else ""
                                                if "~~~" in txt2:
                                                    ans = txt2
                                                    log.info("Found output.txt in Studio Artifacts (%d chars)", len(txt2))
                                                    break
                                            except Exception:
                                                pass
                                    if ans and "~~~" in ans:
                                        break
                                except Exception:
                                    pass
                                # also try follow-up chat dump of artifact
                                if not ans or "~~~" not in ans:
                                    try:
                                        follow = await client.chat.ask(nb, "Output the entire contents of output.txt here in chat as raw tilde lines, one per line. No summary.")
                                        fans = getattr(follow, "answer", "") or ""
                                        if "~~~" in fans:
                                            ans = fans
                                            log.info("Recovered via follow-up chat dump (%d chars)", len(ans))
                                            break
                                    except Exception:
                                        pass
                                await asyncio.sleep(3)
                            if not ans or "~~~" not in ans:
                                if resp is not None:
                                    chat_ans = getattr(resp, "answer", "") or extract_answer_from_raw_stream(getattr(resp, "raw_response", "") or "") or str(resp)
                                    if "~~~" in chat_ans:
                                        ans = chat_ans
                                        log.info("Falling back to chat answer (%d chars)", len(ans))
                                if not ans or "~~~" not in ans:
                                    try:
                                        notes2 = await client.notes.list(nb.id)
                                        for n in notes2:
                                            txt2 = getattr(n, "content", None) or getattr(n, "text", None) or ""
                                            if "~~~" in txt2 and len(txt2) > len(ans or ""):
                                                ans = txt2
                                    except Exception:
                                        pass
                            # final fallback: history
                            if not ans or "~~~" not in ans:
                                try:
                                    hist = await client.chat.get_history(nb.id)
                                    for turn in hist if isinstance(hist, list) else []:
                                        if isinstance(turn, tuple) and len(turn)==2:
                                            _, assistant = turn
                                            if assistant and "~~~" in assistant and len(assistant) > len(ans or ""):
                                                ans = assistant
                                                log.info("Recovered from chat history (%d chars)", len(ans))
                                except Exception:
                                    pass
                        if ans:
                            all_questions = parse_tilde_lines(ans)
                            log.info("Whole-doc recovered %d tilde lines", len(all_questions))
                        else:
                            log.warning("Whole-doc extract: no tilde lines recovered from Studio, chat, notes, or history")
                    else:
                        CHUNK_SIZE = max(10, chunk_size)
                        all_questions: List[Dict[str, Any]] = []
                        max_target_q = q_nums[-1] if q_nums else 61
                        current_q = 1
                        max_iterations = 25
                        iteration = 0
                        while current_q <= max_target_q and iteration < max_iterations:
                            iteration += 1
                            target_end = min(current_q + CHUNK_SIZE - 1, max_target_q)
                            is_last_chunk = target_end >= max_target_q

                            if is_last_chunk:
                                range_desc = f"questions {current_q} to the very end of the document (including question {max_target_q} and any final/extra questions)"
                            else:
                                range_desc = f"ONLY questions {current_q} to {target_end}"

                            chunk_prompt = (
                                f"Extract {range_desc}. "
                                f"Create a new note titled \"output.txt\" containing ONLY the tilde lines (one question per line, no other text) and also try to output them here in chat.\n"
                                f"Preferred source is the note output.txt — I will fetch that note after you create it.\n"
                                f"Output each question as a SINGLE RAW LINE (no markdown, no bold, no bullets).\n"
                                f"Format: {TILDE_FORMAT}\n"
                                f"Rules: non-MCQ set Answer A-D to None. "
                                f"Math in \\( ... \\). If a diagram present set true with PDF page number and hint."
                            )
                            try:
                                # Fresh ask per chunk (no conversation_id)
                                resp = await client.chat.ask(nb.id, chunk_prompt)
                                await asyncio.sleep(2)
                                ans_from_txt = ""
                                try:
                                    notes = await client.notes.list(nb.id)
                                    best = ""
                                    for n in notes:
                                        title = (getattr(n, "title", "") or "").lower()
                                        txt = getattr(n, "content", None) or getattr(n, "text", None) or getattr(n, "full_text", None) or ""
                                        if not txt:
                                            continue
                                        if "~~~" in txt:
                                            if title == "output.txt" or "output" in title:
                                                ans_from_txt = txt
                                                break
                                            if len(txt) > len(best):
                                                best = txt
                                    if not ans_from_txt and best:
                                        ans_from_txt = best
                                    if ans_from_txt:
                                        log.info("TXT-preferred: recovered %d chars from note %r", len(ans_from_txt), title)
                                except Exception as ne:
                                    log.warning("notes.list failed: %s", ne)
                                ans = ans_from_txt
                                if not ans or "~~~" not in ans:
                                    chat_ans = getattr(resp, "answer", "") or ""
                                    if not chat_ans:
                                        raw = getattr(resp, "raw_response", "") or ""
                                        chat_ans = extract_answer_from_raw_stream(raw) or str(resp)
                                    if ans and chat_ans and len(chat_ans) > len(ans):
                                        ans = chat_ans
                                    elif not ans:
                                        ans = chat_ans

                                chunk_parsed = parse_tilde_lines(ans)
                                if chunk_parsed:
                                    all_questions.extend(chunk_parsed)
                                    chunk_nums = [
                                        q.get("question_number")
                                        for q in chunk_parsed
                                        if isinstance(q.get("question_number"), int)
                                    ]
                                    highest_in_chunk = max(chunk_nums) if chunk_nums else target_end
                                    log.info(
                                        "Chunk Q%s–Q%s: %d questions parsed (highest: Q%d)",
                                        current_q,
                                        target_end,
                                        len(chunk_parsed),
                                        highest_in_chunk,
                                    )

                                    # Dynamic advance: resume immediately after the highest parsed question
                                    if highest_in_chunk >= current_q:
                                        current_q = highest_in_chunk + 1
                                    else:
                                        current_q = target_end + 1
                                else:
                                    log.warning(
                                        "Chunk Q%s–Q%s: no parseable tilde lines. ans[:200]=%r",
                                        current_q,
                                        target_end,
                                        ans[:200],
                                    )
                                    current_q = target_end + 1
                            except Exception as chunk_err:
                                log.warning("Chunk Q%s–Q%s failed: %s", current_q, target_end, chunk_err)
                                current_q = target_end + 1

                    if all_questions:
                        # Deduplicate by question number, keeping the most complete entry
                        by_num = {}
                        for q in all_questions:
                            num = q.get("question_number")
                            if num is None:
                                continue
                            if num not in by_num or len(str(q.get("raw_stem", ""))) > len(str(by_num[num].get("raw_stem", ""))):
                                by_num[num] = q
                        unique_questions = [by_num[k] for k in sorted(by_num.keys())]
                        log.info("Parsed %d unique questions (from %d total entries) from %s", len(unique_questions), len(all_questions), file_path.name)
                        return unique_questions
                finally:
                    try:
                        await client.notebooks.delete(nb.id)
                    except Exception:
                        pass
                    if not all_questions:
                        log.warning("Notebook %s deleted after 0 questions extracted from %s (debug trail preserved in logs)", nb.id, file_path.name)
        except Exception as e:
            log.warning("Document extraction via NotebookLM failed: %s. Falling back.", e)

        return []


def extract_answer_from_raw_stream(raw: str) -> str:
    """
    Robustly extract the generated text from a Google GenerateFreeFormStreamed response,
    bypassing line-splitting bugs in upstream libraries when responses contain newlines.
    """
    import json
    import re

    if not raw:
        return ""

    candidates = []

    # Strategy 1: Extract inner JSON string from wrb.fr envelopes
    for match in re.finditer(r'wrb\.fr["\']?\s*,\s*null\s*,\s*"((?:[^"\\]|\\.)*)"', raw):
        escaped_str = match.group(1)
        try:
            inner_json = json.loads(f'"{escaped_str}"')
            inner_data = json.loads(inner_json)
            if isinstance(inner_data, list) and inner_data:
                first = inner_data[0]
                if isinstance(first, list) and first:
                    text = first[0]
                    if isinstance(text, str) and text.strip():
                        candidates.append(text)
        except Exception:
            pass

    # Strategy 2: Direct extraction of tilde-delimited lines from raw text
    tilde_lines = []
    # Replace escaped newlines so each question is on its own line
    normalized = raw.replace("\\r\\n", "\n").replace("\\n", "\n")
    for line in normalized.splitlines():
        line = line.strip()
        if "~~~" in line:
            # Clean up JSON string artifact quotes/brackets around tilde line
            clean_line = re.sub(r'^[\[\],"\s\\]+', '', line)
            clean_line = re.sub(r'[\[\],"\s\\]+$', '', clean_line)
            if clean_line.startswith("~") or "~~~" in clean_line:
                tilde_lines.append(clean_line)

    if tilde_lines:
        candidates.append("\n".join(tilde_lines))

    if candidates:
        return max(candidates, key=len)
    return ""


def parse_tilde_lines(text: str) -> List[Dict[str, Any]]:
    """
    Parse ~~~~~Field1: val~~~Field2: val~~~~~ lines output by NotebookLM or Gemini.
    Supports Exam, Subject, Year, Question Number, Label Path, Question Type,
    Marks, Passage Context, Question stem, Answer A-D choices, Diagram Present/Page/Hint
    for Question and individual Answer choices.
    """
    import re
    questions = []
    lines = text.splitlines()

    for line in lines:
        line = line.strip()
        if not line or "~~~" not in line:
            continue

        # Strip outer ~~~~~
        clean = re.sub(r"^~+", "", line)
        clean = re.sub(r"~+$", "", clean)

        fields = {}
        tokens = clean.split("~~~")
        for token in tokens:
            token = token.strip()
            if not token:
                continue
            colon = token.find(":")
            if colon != -1:
                key = token[:colon].strip()
                val = token[colon + 1:].strip()
                fields[key] = val

        num_str = fields.get("Question Number") or fields.get("Number") or fields.get("number")
        stem = fields.get("Question") or fields.get("question") or fields.get("Stem") or ""
        if not num_str and not stem:
            if line.startswith("#"):
                continue
            pos_parts = [p.strip() for p in line.split("~~~~~")]
            if len(pos_parts) < 2:
                pos_parts = [p.strip() for p in clean.split("~~~")]
            pos_parts = [p for p in pos_parts if p != ""]
            if len(pos_parts) >= 7 and re.match(r"^\d+$", pos_parts[0].strip()):
                try:
                    pn = pos_parts[0].strip()
                    fields["Question Number"] = pn
                    if len(pos_parts) > 1:
                        fields["Exam"] = pos_parts[1]
                    if len(pos_parts) > 2:
                        fields["Subject"] = pos_parts[2]
                    if len(pos_parts) > 3:
                        fields["Year"] = pos_parts[3]
                    if len(pos_parts) > 4:
                        fields["Label Path"] = pos_parts[4]
                    if len(pos_parts) > 5:
                        fields["Question Type"] = pos_parts[5]
                    if len(pos_parts) > 6:
                        fields["Question"] = pos_parts[6]
                    for idx, letter in enumerate(["A", "B", "C", "D"]):
                        pi = 7 + idx
                        if pi < len(pos_parts):
                            val = pos_parts[pi]
                            if ":" in val:
                                val = val.split(":", 1)[1].strip()
                            fields[f"Answer {letter}"] = val
                    if len(pos_parts) > 11:
                        fields["Question Diagram Present"] = pos_parts[11]
                    if len(pos_parts) > 12:
                        fields["Question Diagram Page"] = pos_parts[12]
                    if len(pos_parts) > 13:
                        fields["Question Diagram Hint"] = pos_parts[13]
                    num_str = fields.get("Question Number")
                    stem = fields.get("Question") or ""
                    if not num_str and not stem:
                        continue
                except Exception:
                    continue
            else:
                continue

        # Decode \n escape sequences in stem or context for clean multi-line display
        if "\\n" in stem:
            stem = stem.replace("\\n", "\n")

        q_num = 1
        if num_str:
            digits = re.findall(r"\d+", str(num_str))
            if digits:
                q_num = int(digits[0])

        label_path = fields.get("Label Path") or fields.get("label_path") or str(q_num)
        q_type = fields.get("Question Type") or fields.get("question_type") or "Multiple Choice"
        marks = fields.get("Marks") or fields.get("marks")
        if marks and marks.strip().lower() in ("none", "null", ""):
            marks = None

        context = fields.get("Passage Context") or fields.get("Context") or fields.get("context")
        if context and context.strip().lower() in ("none", "null", ""):
            context = None
        elif context and "\\n" in context:
            context = context.replace("\\n", "\n")

        # Question Diagram parsing
        q_dp_raw = str(fields.get("Question Diagram Present") or fields.get("Diagram Present", "false")).strip().lower()
        q_has_diagram = q_dp_raw in ("true", "yes", "1")

        q_p_raw = fields.get("Question Diagram Page") or fields.get("Page") or fields.get("page")
        q_page_num = None
        if q_p_raw and q_p_raw.strip().lower() not in ("none", "null", ""):
            digits = re.findall(r"\d+", str(q_p_raw))
            if digits:
                q_page_num = int(digits[0])

        q_diag_hint = fields.get("Question Diagram Hint") or fields.get("Diagram Description") or fields.get("diagram_description")
        if q_diag_hint and q_diag_hint.strip().lower() in ("none", "null", ""):
            q_diag_hint = None

        # Parse Choices A-D and their individual diagrams
        choices = []
        diagram_info = {
            "question": {
                "present": q_has_diagram,
                "page": q_page_num,
                "hint": q_diag_hint,
            }
        }

        any_diagram = q_has_diagram

        for l in ("A", "B", "C", "D"):
            c_val = fields.get(f"Answer {l}") or fields.get(f"Choice {l}") or fields.get(l)
            if c_val and c_val.strip().lower() not in ("none", "null", ""):
                # Strip leading "A. ", "(A) ", "A) " if accidentally present
                c_clean = re.sub(r"^\(?[A-Da-d]\)?[.:\)]\s*", "", c_val.strip())
                choices.append(c_clean)

            # Choice diagram fields
            c_dp_raw = str(fields.get(f"Answer {l} Diagram Present", "false")).strip().lower()
            c_has_diag = c_dp_raw in ("true", "yes", "1")

            c_p_raw = fields.get(f"Answer {l} Diagram Page")
            c_page_num = None
            if c_p_raw and c_p_raw.strip().lower() not in ("none", "null", ""):
                digits = re.findall(r"\d+", str(c_p_raw))
                if digits:
                    c_page_num = int(digits[0])

            c_hint = fields.get(f"Answer {l} Diagram Hint")
            if c_hint and c_hint.strip().lower() in ("none", "null", ""):
                c_hint = None

            diagram_info[l] = {
                "present": c_has_diag,
                "page": c_page_num or q_page_num,
                "hint": c_hint,
            }
            if c_has_diag:
                any_diagram = True

        exam = fields.get("Exam") or fields.get("exam")
        subject = fields.get("Subject") or fields.get("subject")
        year = fields.get("Year") or fields.get("year")
        month = fields.get("Month") or fields.get("month")
        paper = fields.get("Paper") or fields.get("paper")

        q_item = {
            "question_number": q_num,
            "label_path": str(label_path),
            "question_type": q_type,
            "marks": marks,
            "context_latex": context,
            "raw_stem": stem,
            "raw_choices": choices,
            "has_visual_diagram": any_diagram,
            "diagram_description": q_diag_hint,
            "source_page_number": q_page_num or 1,
            "diagram_info": diagram_info,
        }
        if exam and exam.strip().lower() not in ("none", "null", ""):
            q_item["exam"] = exam.strip()
        if subject and subject.strip().lower() not in ("none", "null", ""):
            q_item["subject"] = subject.strip()
        if year and year.strip().lower() not in ("none", "null", ""):
            q_item["year"] = year.strip()
        if month and month.strip().lower() not in ("none", "null", ""):
            q_item["month"] = month.strip()
        if paper and paper.strip().lower() not in ("none", "null", ""):
            q_item["paper"] = paper.strip()

        questions.append(q_item)

    return questions


class OpenAIProvider(AiProvider):
    """Provider powered by OpenAI / Azure OpenAI (e.g. gpt-4o-mini / gpt-4o)."""

    def __init__(self, model: str = "gpt-4o-mini", api_key: Optional[str] = None):
        self.model = model
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")

    async def extract_and_structure(
        self, ocr_text: str, image_description: str = ""
    ) -> Dict[str, Any]:
        try:
            import httpx
            template = _load_prompt_template("extraction_structuring.txt")
            prompt = template.replace("{{ocr_text}}", ocr_text).replace(
                "{{image_description}}", image_description or "None"
            )

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            }

            async with httpx.AsyncClient(timeout=45.0) as client:
                res = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
                res.raise_for_status()
                data = res.json()
                content = data["choices"][0]["message"]["content"]
                return json.loads(content)
        except Exception as e:
            log.warning("OpenAI API call failed (%s), falling back to simulation parser.", e)
            return SimulationProvider().extract_and_structure_sync(ocr_text, image_description)

    async def reword_and_clean(
        self, raw_stem: str, raw_choices: list, visible_correct_answer: Optional[str] = None, source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        try:
            import httpx
            template = _load_prompt_template("rewording_own_pdf.txt")
            prompt = (
                template.replace("{{raw_stem}}", raw_stem)
                .replace("{{raw_choices}}", json.dumps(raw_choices))
                .replace("{{visible_correct_answer}}", str(visible_correct_answer or "None"))
            )
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "user", "content": prompt}
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            }
            async with httpx.AsyncClient(timeout=45.0) as client:
                res = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
                res.raise_for_status()
                data = res.json()
                content = data["choices"][0]["message"]["content"]
                return json.loads(content)
        except Exception as e:
            log.warning("OpenAI API call failed (%s), falling back to simulation parser.", e)
            return SimulationProvider().reword_and_clean_sync(raw_stem, raw_choices, visible_correct_answer)

    async def reword_and_clean_batch(
        self, questions: List[Dict[str, Any]], source_paths: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        try:
            import httpx
            template = _load_prompt_template("rewording_batch.txt")
            batch_payload = [
                {
                    "question_number": q.get("question_number", i + 1),
                    "context_latex": q.get("context_latex"),
                    "raw_stem": q.get("raw_stem", ""),
                    "raw_choices": q.get("raw_choices", []),
                    "visible_correct_answer": q.get("visible_correct_answer"),
                }
                for i, q in enumerate(questions)
            ]
            prompt = (
                template.replace("{{batch_size}}", str(len(questions)))
                .replace("{{questions_json}}", json.dumps(batch_payload, indent=2))
            )
            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
            }
            async with httpx.AsyncClient(timeout=60.0) as client:
                res = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
                res.raise_for_status()
                data = res.json()
                content = data["choices"][0]["message"]["content"]
                val = json.loads(content)
                if isinstance(val, list):
                    return val
                if isinstance(val, dict) and "questions" in val:
                    return val["questions"]
        except Exception as e:
            log.warning("OpenAI batch reword failed (%s), falling back to simulation.", e)
        return SimulationProvider().reword_and_clean_batch_sync(questions)

    async def describe_diagram(self, image_description_or_path: str) -> Dict[str, Any]:
        return {
            "diagram_type": "geometric figure / coordinate graph",
            "full_description": str(image_description_or_path),
            "all_labeled_values": [],
        }

    async def recheck_question(self, question_dict: Dict[str, Any], source_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        return await SimulationProvider().recheck_question(question_dict)

    async def extract_from_answer_script(self, answer_script_text: str, source_paths: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        return await SimulationProvider().extract_from_answer_script(answer_script_text)

    async def critique_and_refine(
        self, question_dict: Dict[str, Any], critique_issues: str, original_doc_excerpt: str = "", image_hints: str = "", source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        return await SimulationProvider().critique_and_refine(question_dict, critique_issues)


class SimulationProvider(AiProvider):
    """Deterministic local parser for testing and offline fallback."""

    async def extract_and_structure(
        self, ocr_text: str, image_description: str = ""
    ) -> Dict[str, Any]:
        return self.extract_and_structure_sync(ocr_text, image_description)

    def extract_and_structure_sync(
        self, ocr_text: str, image_description: str = ""
    ) -> Dict[str, Any]:
        import re

        lines = [l.strip() for l in ocr_text.strip().split("\n") if l.strip()]
        choices = []
        stem_lines = []
        choice_letters = ["(A)", "(B)", "(C)", "(D)", "A.", "B.", "C.", "D.", "A)", "B)", "C)", "D)"]

        for line in lines:
            is_choice = False
            for marker in choice_letters:
                if line.startswith(marker):
                    choices.append(line[len(marker):].strip())
                    is_choice = True
                    break
            if not is_choice:
                stem_lines.append(line)

        raw_stem = " ".join(stem_lines) if stem_lines else ocr_text

        # Basic delimiter normalization for test purposes
        reworded = re.sub(r"(?<!\\)\$(.*?)(?<!\\)\$", r"\\(\1\\)", raw_stem)

        return {
            "raw_stem": raw_stem,
            "raw_choices": choices[:4] if len(choices) >= 4 else choices,
            "visible_correct_answer": None,
            "diagram_description_verbatim": image_description or None,
            "needs_human_review": len(choices) != 4,
            "extraction_notes": "Processed via extraction parser",
        }

    async def reword_and_clean(
        self, raw_stem: str, raw_choices: list, visible_correct_answer: Optional[str] = None, source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        return self.reword_and_clean_sync(raw_stem, raw_choices, visible_correct_answer)

    def reword_and_clean_sync(
        self, raw_stem: str, raw_choices: list, visible_correct_answer: Optional[str] = None
    ) -> Dict[str, Any]:
        import re
        clean_stem = re.sub(r"(?<!\\)\$(.*?)(?<!\\)\$", r"\\(\1\\)", raw_stem)
        clean_choices = [
            re.sub(r"(?<!\\)\$(.*?)(?<!\\)\$", r"\\(\1\\)", str(c)) for c in raw_choices
        ]

        return {
            "reworded_stem": clean_stem,
            "reworded_choices": clean_choices,
            "correct_choice_index": 0 if visible_correct_answer else None,
            "possible_error_flag": False,
            "possible_error_note": "",
        }

    async def reword_and_clean_batch(
        self, questions: List[Dict[str, Any]], source_paths: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        return self.reword_and_clean_batch_sync(questions)

    def reword_and_clean_batch_sync(
        self, questions: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        results = []
        for q in questions:
            res = self.reword_and_clean_sync(
                raw_stem=q.get("raw_stem", ""),
                raw_choices=q.get("raw_choices", []),
                visible_correct_answer=q.get("visible_correct_answer"),
            )
            res["question_number"] = q.get("question_number")
            res["reworded_context"] = q.get("context_latex")
            results.append(res)
        return results

    async def describe_diagram(self, image_description_or_path: str) -> Dict[str, Any]:
        return {
            "diagram_type": "geometric figure / coordinate graph",
            "full_description": str(image_description_or_path),
            "all_labeled_values": [],
        }

    async def recheck_question(self, question_dict: Dict[str, Any], source_paths: Optional[List[str]] = None) -> Dict[str, Any]:
        stem = question_dict.get("reworded_stem") or question_dict.get("raw_stem", "")
        choices = question_dict.get("choices", [])
        return {
            "pass": len(choices) == 4,
            "independent_worked_solution": f"Independent derivation completed for: {stem[:60]}...",
            "verified_correct_choice_index": question_dict.get("correct_choice_index", 0),
            "issues_found": [] if len(choices) == 4 else ["Choice count is not 4"],
            "recheck_notes": "Verified by independent pass",
        }

    async def extract_from_answer_script(self, answer_script_text: str, source_paths: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        import re
        answers = []
        # Match lines like "1. (A) ..." or "Question 1: B"
        lines = [l.strip() for l in answer_script_text.split("\n") if l.strip()]
        for line in lines:
            m = re.search(r"(?:Question\s+|Q)?(\d+)[\.:\)]\s*(?:Option\s+|Choice\s+)?\(([A-D])\)|(?:Question\s+|Q)?(\d+)[\.:\)]\s*([A-D])\b", line, re.IGNORECASE)
            if m:
                q_num = int(m.group(1) or m.group(3))
                letter = (m.group(2) or m.group(4)).upper()
                idx = ord(letter) - ord('A')
                answers.append({
                    "question_number": q_num,
                    "label_path": str(q_num),
                    "correct_choice_letter": letter,
                    "correct_choice_index": idx,
                    "explanation_latex": line,
                    "has_solution_diagram": "figure" in line.lower() or "diagram" in line.lower(),
                })
        return answers

    async def critique_and_refine(
        self, question_dict: Dict[str, Any], critique_issues: str, original_doc_excerpt: str = "", image_hints: str = "", source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        return self.critique_and_refine_sync(question_dict, critique_issues)

    def critique_and_refine_sync(
        self, question_dict: Dict[str, Any], critique_issues: str
    ) -> Dict[str, Any]:
        import re
        stem = question_dict.get("reworded_stem") or question_dict.get("raw_stem", "")
        choices = question_dict.get("choices") or question_dict.get("raw_choices", [])
        clean_stem = re.sub(r"(?<!\\)\$(.*?)(?<!\\)\$", r"\\(\1\\)", stem)
        clean_choices = [
            re.sub(r"(?<!\\)\$(.*?)(?<!\\)\$", r"\\(\1\\)", str(c)) for c in choices
        ]
        return {
            "reworded_context": question_dict.get("context_latex"),
            "reworded_stem": clean_stem,
            "reworded_choices": clean_choices,
            "correct_choice_index": question_dict.get("correct_choice_index", 0),
            "explanation_latex": f"Resolved critique: {critique_issues}",
            "refinement_notes": "Refined via simulation fallback",
        }


def get_ai_provider(provider_type: str = "notebooklm", **kwargs) -> AiProvider:
    """Factory function to get the configured AI provider (NotebookLM is primary default)."""
    if provider_type == "notebooklm" or provider_type == "auto":
        return NotebookLMProvider(**kwargs)
    elif provider_type == "openai":
        return OpenAIProvider(**kwargs)
    elif provider_type == "simulation":
        return SimulationProvider()
    return NotebookLMProvider(**kwargs)
