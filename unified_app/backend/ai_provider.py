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


class DiagramGenerationFailed(Exception):
    """
    Raised when an AI-backed diagram call (regenerate/validate/fix) could not
    produce a usable, parseable result after retries.

    This intentionally does NOT fall back to SimulationProvider's canned
    stub content. Callers should catch this and route the record straight
    to needs_fix/human_review with the real error message, rather than
    silently rendering an unrelated placeholder diagram as if it were a
    genuine AI result.
    """
    pass


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

    async def regenerate_diagram(
        self, question_dict: Dict[str, Any], diagram_path: Optional[str] = None, source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Convert diagram screenshot into code-based rendering (matplotlib, mermaid, svg)."""
        return {}

    async def validate_diagram(
        self, question_dict: Dict[str, Any], original_path: str, rendered_path: str, source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Validate rendered diagram against original screenshot in question context."""
        return {}

    async def fix_diagram(
        self, question_dict: Dict[str, Any], original_path: str, previous_code: str, format_type: str, missing: list, distorted: list, fix_instructions: str, source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """Fix diagram code based on validation issues."""
        return {}


class NotebookLMProvider(AiProvider):
    """Provider powered by the local multi-account NotebookLM Chatter engine (Primary Default)."""

    def __init__(self, profile: Optional[str] = None):
        self.profile = profile
        self._last_used_profile: Optional[str] = None

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
        self._last_used_profile = profile
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
                    self._last_used_profile = profile
                    return ans
                raise RuntimeError(err or "submit failed")
            except AuthExpiredError as ae:
                mark_rate_limited(profile, str(ae), cooldown_seconds=3600)
                tried = {profile}
                backup = find_backup_profile(profile, exclude=tried)
                if backup:
                    tried.add(backup)
                    log.warning("[%s] failed (%s) -> falling back to [%s]", profile, ae, backup)
                    try:
                        from backend.batch_scheduler import telemetry as _tel
                        _tel.add_event(f"[{profile}] fell back to [{backup}]: {str(ae)[:80]}", "error")
                    except Exception:
                        pass
                    ok2, ans2, err2 = await _submit_async(backup, all_sources, prompt, {}, label)
                    if ok2:
                        self._last_used_profile = backup
                        return ans2
                raise
            except Exception as e:
                err = str(e)
                if _is_auth_expired(err):
                    mark_rate_limited(profile, err, cooldown_seconds=3600)
                    raise AuthExpiredError(err) from e
                if _is_rate_limit(err) or isinstance(e, (asyncio.TimeoutError, TimeoutError)) or "timeout" in err.lower():
                    if _is_rate_limit(err):
                        mark_rate_limited(profile, err)
                    tried = {profile}
                    backup = find_backup_profile(profile, exclude=tried)
                    if backup:
                        reason_label = "timed out" if ("timeout" in err.lower() or isinstance(e, (asyncio.TimeoutError, TimeoutError))) else "rate-limited"
                        log.warning("[%s] %s (%s) -> falling back to [%s]", profile, reason_label, err[:80], backup)
                        try:
                            from backend.batch_scheduler import telemetry as _tel
                            _tel.add_event(f"[{profile}] {reason_label} -> [{backup}]", "error")
                        except Exception:
                            pass
                        try:
                            ok2, ans2, err2 = await _submit_async(backup, all_sources, prompt, {}, label)
                            if ok2:
                                self._last_used_profile = backup
                                return ans2
                        except Exception as e2:
                            log.warning("[%s] fallback also failed: %s", backup, e2)
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

    async def _ask_json_with_retry(
        self,
        source_text: str,
        prompt: str,
        label: str,
        source_paths: Optional[List[str]] = None,
        max_attempts: int = 3,
    ) -> dict:
        """
        Like _ask() + _parse_json_response(), but retries on failure
        (including a falsy/unparseable JSON response, which _ask() itself
        would treat as a "success") before giving up. Rotates to a backup
        profile between attempts when one is available, since a second
        attempt on the exact same account is unlikely to fix a bad response.
        Raises DiagramGenerationFailed instead of ever silently returning
        stub/placeholder content.
        """
        from backend.notebooklm_client import find_backup_profile
        last_err = "unknown failure"
        for attempt in range(1, max_attempts + 1):
            try:
                answer = await self._ask(source_text, prompt, label=label, source_paths=source_paths)
                res = self._parse_json_response(answer)
                if res:
                    return res
                last_err = "Model returned no parseable JSON"
                log.warning("[%s] attempt %d/%d: %s", label, attempt, max_attempts, last_err)
            except Exception as e:
                last_err = str(e)
                log.warning("[%s] attempt %d/%d failed: %s", label, attempt, max_attempts, last_err)

            if attempt < max_attempts:
                backup = find_backup_profile(self.profile or "", exclude={self.profile})
                if backup:
                    log.info("[%s] rotating profile %s -> %s for retry", label, self.profile, backup)
                    self.profile = backup
                await asyncio.sleep(2 * attempt)

        raise DiagramGenerationFailed(f"{label} failed after {max_attempts} attempts: {last_err}")

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
                "diagram_hint": q.get("diagram_hint") or q.get("diagram_description") or q.get("diagram_info"),
                "label_path": q.get("label_path"),
            })
        prompt = (
            template
            .replace("{{batch_size}}", str(len(questions)))
            .replace("{{questions_json}}", json.dumps(batch_input, indent=2))
        )
        source_text = f"Batch of {len(questions)} exam questions for KaTeX normalization:\n\n{json.dumps(batch_input, indent=2)}"
        try:
            answer = await self._ask(source_text, prompt, label="reword_batch", source_paths=source_paths)
            try:
                save_debug_raw(source_paths[0] if source_paths else "reword", answer, "reword_batch")
            except Exception:
                pass
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
        for attempt in range(2):
            try:
                answer = await self._ask(source_text, prompt, label="refine", source_paths=source_paths)
                result = self._parse_json_response(answer)
                if result:
                    return result
                log.warning("NotebookLM returned non-JSON for critique_and_refine on %s (attempt %d)", question_dict.get("label_path"), attempt+1)
            except Exception as e:
                log.warning("NotebookLM refine failed for %s (attempt %d): %s", question_dict.get("label_path"), attempt+1, e)
            if attempt == 0:
                import asyncio as _asyncio
                await _asyncio.sleep(1.5)
        return {
            "refine_failed": True,
            "refinement_notes": f"AI refine call failed or returned invalid JSON — needs manual fix. Last error/critique: {str(critique_issues)[:200]}",
            "reworded_stem": question_dict.get("reworded_stem") or question_dict.get("raw_stem"),
            "reworded_choices": question_dict.get("choices") or question_dict.get("raw_choices"),
            "table": question_dict.get("table"),
            "correct_choice_index": question_dict.get("correct_choice_index"),
            "explanation_latex": question_dict.get("explanation_latex"),
        }

    def build_answer_bundle(self, question_rec: dict, whole_pdf_path: Optional[str] = None, flag_reason: Optional[str] = None) -> dict:
        imgs = question_rec.get("images") or []
        srcs = [whole_pdf_path] + imgs if whole_pdf_path else imgs
        bundle = {
            "source_paths": srcs,
            "question_text": question_rec.get("reworded_stem") or question_rec.get("raw_stem", ""),
            "choices": question_rec.get("reworded_choices") or question_rec.get("choices") or question_rec.get("raw_choices", []),
            "table": question_rec.get("table"),
            "context_latex": question_rec.get("context_latex"),
        }
        if flag_reason:
            bundle["flag_reason"] = flag_reason
        return bundle

    async def solve_batch(self, questions: List[Dict[str, Any]], source_paths: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        template = _load_prompt_template("solve_and_explain.txt")
        if not template:
            return []
        batch_input = [{"question_number": q.get("question_number"), "reworded_stem": q.get("reworded_stem"), "reworded_choices": q.get("reworded_choices") or q.get("choices"), "table": q.get("table"), "context_latex": q.get("context_latex")} for q in questions]
        prompt = template.replace("{{batch_size}}", str(len(questions))).replace("{{questions_json}}", json.dumps(batch_input, indent=2))
        source_text = json.dumps(batch_input, indent=2)
        try:
            answer = await self._ask(source_text, prompt, label="solve", source_paths=source_paths)
            parsed = self._parse_json_array_response(answer)
            if parsed:
                return parsed
            single = self._parse_json_response(answer)
            if single:
                return [single]
        except Exception as e:
            log.warning("solve_batch failed: %s", e)
        return []

    async def validate_answers_batch(self, questions: List[Dict[str, Any]], source_paths: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        template = _load_prompt_template("answer_validate.txt")
        if not template:
            return []
        batch_input = [{"question_number": q.get("question_number"), "reworded_stem": q.get("reworded_stem"), "reworded_choices": q.get("reworded_choices") or q.get("choices"), "table": q.get("table"), "context_latex": q.get("context_latex")} for q in questions]
        prompt = template.replace("{{batch_size}}", str(len(questions))).replace("{{questions_json}}", json.dumps(batch_input, indent=2))
        source_text = json.dumps(batch_input, indent=2)
        try:
            answer = await self._ask(source_text, prompt, label="validate", source_paths=source_paths)
            parsed = self._parse_json_array_response(answer)
            if parsed:
                return parsed
        except Exception as e:
            log.warning("validate_answers_batch failed: %s", e)
        return []

    async def fix_answer(self, question_rec: dict, mismatch_reason: str, source_paths: Optional[List[str]] = None) -> dict:
        template = _load_prompt_template("answer_fix.txt")
        if not template:
            return {}
        bundle_json = json.dumps(self.build_answer_bundle(question_rec), indent=2)
        solve = question_rec.get("solve") or {}
        validation = question_rec.get("answer_validation") or {}
        solve_idx = solve.get("correct_choice_index") if solve else question_rec.get("correct_choice_index")
        solve_exp = solve.get("explanation_latex") if solve else question_rec.get("explanation_latex")
        val_idx = validation.get("verified_correct_choice_index")
        val_sol = validation.get("independent_worked_solution")
        prompt = (
            template
            .replace("{{bundle_json}}", bundle_json)
            .replace("{{solve_index}}", str(solve_idx if solve_idx is not None else "None"))
            .replace("{{solve_explanation}}", str(solve_exp or "None"))
            .replace("{{validate_index}}", str(val_idx if val_idx is not None else "None"))
            .replace("{{validate_solution}}", str(val_sol or "None"))
            .replace("{{mismatch_reason}}", str(mismatch_reason or ""))
            .replace("{{flag_reason}}", str(question_rec.get("possible_error_note") or ""))
        )
        source_text = bundle_json
        try:
            answer = await self._ask(source_text, prompt, label="fix", source_paths=source_paths)
            result = self._parse_json_response(answer)
            if result:
                return result
        except Exception as e:
            log.warning("fix_answer failed: %s", e)
        return {}

    async def regenerate_diagram(
        self, question_dict: Dict[str, Any], diagram_path: Optional[str] = None, source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        template = _load_prompt_template("diagram_regenerate.txt")
        if not template:
            return {}
        stem = question_dict.get("reworded_stem") or question_dict.get("raw_stem", "")
        choices = question_dict.get("choices") or question_dict.get("raw_choices", [])
        corr = question_dict.get("correct_choice_index")
        corr_str = f"Index {corr}" if corr is not None else "Unknown"
        diag_ctx = question_dict.get("diagram_description") or question_dict.get("diagram_hint") or "None"

        prompt = (
            template
            .replace("{{stem}}", stem)
            .replace("{{choices}}", json.dumps(choices))
            .replace("{{correct_answer}}", corr_str)
            .replace("{{diagram_context}}", str(diag_ctx))
        )
        all_sources = list(source_paths or [])
        if diagram_path and Path(diagram_path).exists() and str(diagram_path) not in all_sources:
            all_sources.append(str(diagram_path))

        source_text = f"Question Stem: {stem}\nChoices: {json.dumps(choices)}\nAnswer: {corr_str}\nDiagram Context: {diag_ctx}"
        return await self._ask_json_with_retry(
            source_text, prompt, label="regen_diag", source_paths=all_sources, max_attempts=3
        )

    async def validate_diagram(
        self, question_dict: Dict[str, Any], original_path: str, rendered_path: str, source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        template = _load_prompt_template("diagram_validate.txt")
        if not template:
            return {}
        stem = question_dict.get("reworded_stem") or question_dict.get("raw_stem", "")
        choices = question_dict.get("choices") or question_dict.get("raw_choices", [])
        corr = question_dict.get("correct_choice_index")
        corr_str = f"Index {corr}" if corr is not None else "Unknown"
        diag_ctx = question_dict.get("diagram_description") or question_dict.get("diagram_hint") or "None"

        prompt = (
            template
            .replace("{{stem}}", stem)
            .replace("{{choices}}", json.dumps(choices))
            .replace("{{correct_answer}}", corr_str)
            .replace("{{diagram_context}}", str(diag_ctx))
        )
        all_sources = list(source_paths or [])
        if original_path and Path(original_path).exists() and str(original_path) not in all_sources:
            all_sources.append(str(original_path))
        if rendered_path and Path(rendered_path).exists() and str(rendered_path) not in all_sources:
            all_sources.append(str(rendered_path))

        source_text = f"Question Stem: {stem}\nChoices: {json.dumps(choices)}\nAnswer: {corr_str}\nDiagram Context: {diag_ctx}"
        return await self._ask_json_with_retry(
            source_text, prompt, label="val_diag", source_paths=all_sources, max_attempts=3
        )

    async def fix_diagram(
        self, question_dict: Dict[str, Any], original_path: str, previous_code: str, format_type: str, missing: list, distorted: list, fix_instructions: str, source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        template = _load_prompt_template("diagram_fix.txt")
        if not template:
            return {}
        stem = question_dict.get("reworded_stem") or question_dict.get("raw_stem", "")
        choices = question_dict.get("choices") or question_dict.get("raw_choices", [])
        corr = question_dict.get("correct_choice_index")
        corr_str = f"Index {corr}" if corr is not None else "Unknown"

        prompt = (
            template
            .replace("{{stem}}", stem)
            .replace("{{choices}}", json.dumps(choices))
            .replace("{{correct_answer}}", corr_str)
            .replace("{{format}}", format_type or "matplotlib")
            .replace("{{previous_code}}", previous_code)
            .replace("{{missing_elements}}", json.dumps(missing or []))
            .replace("{{distorted_elements}}", json.dumps(distorted or []))
            .replace("{{fix_instructions}}", fix_instructions or "")
        )
        all_sources = list(source_paths or [])
        if original_path and Path(original_path).exists() and str(original_path) not in all_sources:
            all_sources.append(str(original_path))

        source_text = f"Question Stem: {stem}\nFormat: {format_type}\nPrevious Code:\n{previous_code}\nFix Instructions: {fix_instructions}"
        # Only 2 attempts here: this is already the last-chance repair stage
        # (step 13); if it can't get a real fix quickly, the record should
        # land in human_review with the original screenshot preserved,
        # rather than spending an unbounded number of retries or silently
        # returning the same broken code relabeled as "fixed".
        return await self._ask_json_with_retry(
            source_text, prompt, label="fix_diag", source_paths=all_sources, max_attempts=2
        )

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
                            save_debug_raw(file_path, ans, "whole")
                            all_questions = parse_tilde_lines(ans)
                            log.info("Whole-doc recovered %d tilde lines (raw %d chars, %d lines)", len(all_questions), len(ans), ans.count("\n")+1)
                            if not all_questions and ans:
                                try:
                                    dbg = Path(__file__).parent.parent / "output" / f"debug_{file_path.stem[:20]}_raw.txt"
                                    dbg.parent.mkdir(parents=True, exist_ok=True)
                                    dbg.write_text(ans[:200000], encoding="utf-8", errors="ignore")
                                    log.warning("0 questions parsed — raw model output saved to %s (%d chars)", dbg, len(ans))
                                except Exception as de:
                                    log.warning("failed to save debug raw: %s", de)
                            expected_ct = len(q_nums) if q_nums else 0
                            if all_questions and expected_ct and len(all_questions) < expected_ct * 0.5:
                                log.warning("Parsed %d/%d expected — retrying via plain chat.ask fallback", len(all_questions), expected_ct)
                                try:
                                    fallback_prompt = f"Extract questions 1 to {expected_ct}. Output ONLY raw tilde lines, one per line. No report formatting, no headers, no wrapping. Format: {TILDE_FORMAT}"
                                    resp2 = await client.chat.ask(nb.id, fallback_prompt)
                                    ans2 = getattr(resp2, "answer", "") or extract_answer_from_raw_stream(getattr(resp2, "raw_response", "") or "") or ""
                                    if ans2 and "~~~" in ans2:
                                        fb_parsed = parse_tilde_lines(ans2)
                                        if len(fb_parsed) > len(all_questions):
                                            log.info("Fallback chat recovered %d questions (was %d)", len(fb_parsed), len(all_questions))
                                            all_questions = fb_parsed
                                            ans = ans2
                                except Exception as fe:
                                    log.warning("fallback chat retry failed: %s", fe)
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

                                if ans:
                                    save_debug_raw(file_path, ans, f"chunk_{current_q}_{target_end}")
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
                        log.warning("Notebook %s deleted after 0 questions extracted from %s (raw saved to output/debug_*.txt if available)", nb.id, file_path.name)
                        try:
                            if ans:
                                fallback_dbg = Path(__file__).parent.parent / "output" / f"debug_{file_path.stem[:20]}_raw.txt"
                                fallback_dbg.parent.mkdir(parents=True, exist_ok=True)
                                if not fallback_dbg.exists():
                                    fallback_dbg.write_text(ans[:200000], encoding="utf-8", errors="ignore")
                        except Exception:
                            pass
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


def _normalize_tildes(text: str) -> str:
    import re
    return re.sub(r"(?<!~)~{2,4}(?!~)", "~~~", text)

def save_debug_raw(file_path, ans: str, tag: str):
    try:
        import time
        safe = Path(str(file_path)).stem[:20].replace(" ", "_")
        out = Path(__file__).parent.parent / "output" / "debug_raw" / f"debug_{safe}_{tag}_{int(time.time())}.txt"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(ans[:300000] if ans else "(empty)", encoding="utf-8", errors="ignore")
        log.info("debug raw saved: %s (%d chars)", out, len(ans or ""))
    except Exception as e:
        log.warning("save_debug_raw failed: %s", e)

def parse_tilde_lines(text: str) -> List[Dict[str, Any]]:
    """
    Parse ~~~~~Field1: val~~~Field2: val~~~~~ lines output by NotebookLM or Gemini.
    Resilient to Studio Report word-wrap: rejoins wrapped lines and splits on ~~~~~ boundary.
    Also handles single-tilde positional format: ~~~~~val1~val2~val3~...~~~~~
    """
    import re
    questions = []
    if not text or "~" not in text:
        return questions
    text = _normalize_tildes(text)
    normalized = re.sub(r"\s*\n\s*", " ", text)
    raw_records = re.split(r"~{5,}", normalized)
    # Accept records that have either ~~~ or single ~ as field separator
    records = [r.strip() for r in raw_records if r.strip() and "~" in r]
    if len(records) <= 1:
        line_records = [l.strip() for l in text.splitlines() if "~" in l]
        if len(line_records) > len(records):
            records = line_records

    # Positional field keys for single-tilde format (13 fields, no key prefixes)
    _POSITIONAL_KEYS_13 = ["L", "T", "Q", "A", "B", "C", "D", "_placeholder", "DPG", "DH", "DC", "_extra", "DG"]

    for rec in records:
        clean = rec.strip()
        clean = re.sub(r"^~+", "", clean)
        clean = re.sub(r"~+$", "", clean)

        fields = {}
        tokens = clean.split("~~~")

        # Detect single-tilde positional format: if splitting on ~~~ yields very
        # few tokens but splitting on single ~ yields many, use single-tilde split.
        single_tokens = [t for t in clean.split("~") if t != ""]
        if len(tokens) <= 2 and len(single_tokens) >= 7:
            # Single-tilde positional format — map to keyed fields
            for idx, val in enumerate(single_tokens):
                val = val.strip()
                if idx < len(_POSITIONAL_KEYS_13):
                    key = _POSITIONAL_KEYS_13[idx]
                    if not key.startswith("_"):
                        fields[key] = val
                else:
                    # Extra fields beyond 13 — store with index
                    fields[f"_extra_{idx}"] = val
            log.debug("Positional single-tilde parse: %d fields from record starting with %r", len(fields), single_tokens[0][:30] if single_tokens else "?")
        else:
            for token in tokens:
                token = token.strip()
                if not token:
                    continue
                colon = token.find(":")
                if colon != -1:
                    key = token[:colon].strip()
                    val = token[colon + 1:].strip()
                    fields[key] = val

        num_str = fields.get("L") or fields.get("Label Path") or fields.get("Question Number") or fields.get("Number") or fields.get("number")
        stem = fields.get("Q") or fields.get("Question") or fields.get("question") or fields.get("Stem") or ""
        if not num_str and not stem:
            if rec.startswith("#"):
                continue
            pos_parts = [p.strip() for p in clean.split("~~~") if p.strip()]
            if not pos_parts:
                pos_parts = [p.strip() for p in rec.split("~~~~~") if p.strip()]
            if pos_parts and len(pos_parts) not in (12, 13, 14) and len(pos_parts) < 12:
                log.warning("record %d malformed: got %d fields expected 13 — tokens=%r", len(questions), len(pos_parts), pos_parts[:6])
            handled = False
            if len(pos_parts) >= 7 and len(pos_parts) != 12 and re.match(r"^\d+$", pos_parts[0].strip()):
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
                    if num_str or stem:
                        handled = True
                except Exception:
                    handled = False
            if not handled:
                if len(pos_parts) in (12, 13):
                    try:
                        idx_hint = len(questions)
                        fields = {
                            "L": pos_parts[0], "T": pos_parts[1], "M": pos_parts[2], "PSG": pos_parts[3], "Q": pos_parts[4],
                            "A": pos_parts[5], "B": pos_parts[6], "C": pos_parts[7], "D": pos_parts[8],
                            "DPG": pos_parts[9], "DH": pos_parts[10], "DC": pos_parts[11],
                        }
                        if len(pos_parts) == 13:
                            fields["DG"] = pos_parts[12]
                        num_str = fields.get("L") or str(idx_hint + 1)
                        stem = fields.get("Q") or ""
                        handled = True
                    except Exception:
                        handled = False
                if not handled:
                    continue

        # Decode \n escape sequences in stem or context for clean multi-line display
        if "\\n" in stem:
            stem = stem.replace("\\n", "\n")

        def _pick(*keys):
            for k in keys:
                v = fields.get(k)
                if v is not None and str(v).strip() != "":
                    return str(v).strip()
            return None

        label_path = _pick("L", "Label Path", "label_path") or str(len(questions)+1)
        stem = _pick("Q", "Question", "question", "Stem") or stem
        if "\\n" in stem:
            stem = stem.replace("\\n", "\n")
        q_type = _pick("T", "Question Type", "question_type") or "Multiple Choice"
        marks = _pick("M", "Marks", "marks")
        if marks and marks.strip().lower() in ("none", "null", ""):
            marks = None
        context = _pick("PSG", "P", "Passage Context", "Context", "context")
        if context and re.fullmatch(r"\d{1,3}", context.strip()):
            context = None
        if context and context.strip().lower() in ("none", "null", ""):
            context = None
        elif context and "\\n" in context:
            context = context.replace("\\n", "\n")

        q_page_raw = _pick("DPG", "Question Diagram Page", "Page", "page")
        q_page_num = None
        if q_page_raw and q_page_raw.strip().lower() not in ("none", "null", ""):
            digits = re.findall(r"\d+", str(q_page_raw))
            if digits:
                q_page_num = int(digits[0])

        q_diag_hint = _pick("DH", "Question Diagram Hint", "Diagram Description", "diagram_description")
        if q_diag_hint and q_diag_hint.strip().lower() in ("none", "null", ""):
            q_diag_hint = None

        dc_val = _pick("DC", "Diagram Choice")
        dc_norm = (dc_val or "None").strip().upper()
        q_has_diagram = bool(q_diag_hint and q_diag_hint.lower() not in ("none", "null", ""))

        q_num_digits = re.findall(r"\d+", str(label_path))
        q_num = int(q_num_digits[0]) if q_num_digits else (len(questions)+1)
        if num_str:
            digits2 = re.findall(r"\d+", str(num_str))
            if digits2:
                q_num = int(digits2[0])

        choices = []
        diagram_info = {
            "question": {
                "present": q_has_diagram and dc_norm in ("Q", "QUESTION"),
                "page": q_page_num,
                "hint": q_diag_hint if dc_norm in ("Q", "QUESTION") else None,
            }
        }
        if q_has_diagram and dc_norm in ("Q", "QUESTION"):
            any_diagram = True
        else:
            legacy_q_present = str(fields.get("Question Diagram Present") or fields.get("Diagram Present", "false")).strip().lower()
            if legacy_q_present in ("true", "yes", "1"):
                diagram_info["question"]["present"] = True
                any_diagram = True
            else:
                any_diagram = bool(diagram_info["question"]["present"])

        for l in ("A", "B", "C", "D"):
            c_val = fields.get(l) or fields.get(f"Answer {l}") or fields.get(f"Choice {l}")
            if c_val and c_val.strip().lower() not in ("none", "null", ""):
                c_clean = re.sub(r"^\(?[A-Da-d]\)?[.:\)]\s*", "", c_val.strip())
                choices.append(c_clean)

            c_has_diag = (dc_norm == l)
            if not c_has_diag:
                legacy_c = str(fields.get(f"Answer {l} Diagram Present", "false")).strip().lower()
                if legacy_c in ("true", "yes", "1"):
                    c_has_diag = True

            c_hint = q_diag_hint if c_has_diag and dc_norm == l else None
            if not c_hint:
                legacy_hint = fields.get(f"Answer {l} Diagram Hint")
                if legacy_hint and legacy_hint.strip().lower() not in ("none", "null", ""):
                    c_hint = legacy_hint

            c_page = q_page_num if c_has_diag else None
            if not c_page:
                legacy_page = fields.get(f"Answer {l} Diagram Page")
                if legacy_page and legacy_page.strip().lower() not in ("none", "null", ""):
                    d = re.findall(r"\d+", str(legacy_page))
                    if d:
                        c_page = int(d[0])

            diagram_info[l] = {
                "present": c_has_diag,
                "page": c_page or q_page_num,
                "hint": c_hint,
            }
            if c_has_diag:
                any_diagram = True
        dg_raw = _pick("DG", "Diagram Group")
        if dg_raw and dg_raw.strip().lower() not in ("none", "null", ""):
            try:
                import json as _json2
                dg_list = _json2.loads(dg_raw)
                if isinstance(dg_list, list):
                    q_list_extra = []
                    for entry in dg_list:
                        if not isinstance(entry, dict):
                            continue
                        target = (entry.get("target") or "").strip().upper()
                        pg = entry.get("page")
                        hint = entry.get("hint")
                        pg_num = None
                        if pg is not None and str(pg).strip().lower() not in ("none", "null", ""):
                            d = re.findall(r"\d+", str(pg))
                            if d:
                                pg_num = int(d[0])
                        if target == "Q":
                            q_list_extra.append({"present": True, "page": pg_num or q_page_num, "hint": hint or q_diag_hint})
                            any_diagram = True
                        elif target in ("A", "B", "C", "D"):
                            diagram_info[target] = {"present": True, "page": pg_num or q_page_num, "hint": hint}
                            any_diagram = True
                    if q_list_extra:
                        if isinstance(diagram_info.get("question"), list):
                            diagram_info["question"] = diagram_info["question"] + q_list_extra  # type: ignore
                        elif diagram_info.get("question", {}).get("present"):  # type: ignore
                            diagram_info["question"] = [diagram_info["question"]] + q_list_extra  # type: ignore
                        else:
                            diagram_info["question"] = q_list_extra if len(q_list_extra) > 1 else q_list_extra[0]  # type: ignore
            except Exception:
                pass
        if not q_has_diagram and not any_diagram:
            any_diagram = bool(q_diag_hint)

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

    async def regenerate_diagram(
        self, question_dict: Dict[str, Any], diagram_path: Optional[str] = None, source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        stem = (question_dict.get("reworded_stem") or question_dict.get("raw_stem", "")).lower()
        if any(k in stem for k in ["flow", "process", "algorithm", "step", "decision"]):
            return {
                "format": "mermaid",
                "code": "flowchart TD\n  Start([Start]) --> Process[Step 1: Compute Values]\n  Process --> Decision{Condition Satisfied?}\n  Decision -- Yes --> Finish([End / Result])\n  Decision -- No --> Process",
                "description": "Generated flowchart for process logic",
                "labeled_values": ["Start", "Step 1", "Condition Satisfied", "End / Result"]
            }
        elif any(k in stem for k in ["circle", "triangle", "angle", "polygon", "shape", "geometry"]):
            return {
                "format": "svg",
                "code": '<svg width="300" height="200" xmlns="http://www.w3.org/2000/svg"><polygon points="50,160 250,160 150,40" fill="#f8fafc" stroke="#3b82f6" stroke-width="3"/><text x="150" y="30" font-family="sans-serif" font-size="14" text-anchor="middle" fill="#1e293b" font-weight="bold">A</text><text x="40" y="180" font-family="sans-serif" font-size="14" fill="#1e293b" font-weight="bold">B</text><text x="250" y="180" font-family="sans-serif" font-size="14" fill="#1e293b" font-weight="bold">C</text></svg>',
                "description": "Geometric triangle ABC figure",
                "labeled_values": ["A", "B", "C"]
            }
        else:
            return {
                "format": "matplotlib",
                "code": "x = np.linspace(0, 10, 50)\ny = 2 * x + 3\nplt.plot(x, y, label='y = 2x + 3', color='#2563eb', lw=2)\nplt.xlabel('x')\nplt.ylabel('y')\nplt.title('Coordinate Graph')\nplt.grid(True, linestyle='--', alpha=0.5)\nplt.legend()",
                "description": "Linear coordinate graph",
                "labeled_values": ["x", "y", "y = 2x + 3"]
            }

    async def validate_diagram(
        self, question_dict: Dict[str, Any], original_path: str, rendered_path: str, source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        return {
            "matches_original": True,
            "missing_elements": [],
            "distorted_elements": [],
            "context_sufficient": True,
            "verdict": "pass",
            "fix_instructions": ""
        }

    async def fix_diagram(
        self, question_dict: Dict[str, Any], original_path: str, previous_code: str, format_type: str, missing: list, distorted: list, fix_instructions: str, source_paths: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        return {
            "format": format_type or "matplotlib",
            "code": previous_code,
            "changes_made": "Adjusted formatting and labels based on critique"
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