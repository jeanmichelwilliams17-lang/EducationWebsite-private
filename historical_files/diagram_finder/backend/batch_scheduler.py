"""
backend/batch_scheduler.py
Multi-account worker pool and batch queue scheduler.
Manages concurrent execution across 11 Google accounts, tracks per-account workload,
cooldown states, and live telemetry for real-time UI progress updates.
"""

import asyncio
import datetime
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from backend import notebooklm_client

log = logging.getLogger("nlm.batch")

# ─── Live Telemetry State ─────────────────────────────────────────────

class LiveTelemetry:
    """Thread-safe store for real-time extraction telemetry."""

    def __init__(self):
        self._lock = threading.Lock()
        self.active_job_id: Optional[str] = None
        self.filename: str = ""
        self.stage: str = "idle"  # idle, ocr_crop, awaiting_diagram_review, diagram_review, parallel_extraction, critique_refine, recheck, completed, failed
        self.total_questions: int = 0
        self.completed_questions: int = 0
        self.in_flight_questions: int = 0
        self.failed_questions: int = 0
        self.profile_states: Dict[str, Dict[str, Any]] = {}
        self.events: List[Dict[str, Any]] = []
        self.start_time: Optional[float] = None

    def start_job(self, job_id: str, filename: str, total_questions: int):
        with self._lock:
            self.active_job_id = job_id
            self.filename = filename
            self.stage = "ocr_crop"
            self.total_questions = total_questions
            self.completed_questions = 0
            self.in_flight_questions = 0
            self.failed_questions = 0
            self.start_time = time.time()
            self.events = [{
                "time": datetime.datetime.now().strftime("%H:%M:%S"),
                "msg": f"Started processing {filename} ({total_questions} questions)",
                "type": "info"
            }]

    def set_stage(self, stage: str, msg: str = ""):
        with self._lock:
            self.stage = stage
            if msg:
                self.add_event(msg, "info")

    def update_profile(self, profile: str, status: str, task_desc: str = ""):
        with self._lock:
            self.profile_states[profile] = {
                "status": status,  # "idle", "busy", "cooldown", "disabled"
                "current_task": task_desc,
                "updated_at": time.time(),
            }

    def record_question_start(self, q_num: int, profile: str):
        with self._lock:
            self.in_flight_questions += 1
            self.profile_states[profile] = {
                "status": "busy",
                "current_task": f"Question {q_num}",
                "updated_at": time.time(),
            }
            self.add_event(f"[{profile}] Started Question {q_num}", "progress")

    def record_batch_start(self, q_nums: List[int], profile: str):
        with self._lock:
            count = len(q_nums)
            self.in_flight_questions += count
            range_str = f"Q{q_nums[0]}–Q{q_nums[-1]}" if len(q_nums) > 1 else f"Q{q_nums[0]}"
            self.profile_states[profile] = {
                "status": "busy",
                "current_task": f"Batch {range_str} ({count} qs)",
                "updated_at": time.time(),
            }
            self.add_event(f"[{profile}] 🚀 Started Batch {range_str} ({count} questions)", "progress")

    def record_batch_complete(self, q_nums: List[int], profile: str, notes: str = ""):
        with self._lock:
            count = len(q_nums)
            self.completed_questions += count
            self.in_flight_questions = max(0, self.in_flight_questions - count)
            self.profile_states[profile] = {
                "status": "idle",
                "current_task": "",
                "updated_at": time.time(),
            }
            range_str = f"Q{q_nums[0]}–Q{q_nums[-1]}" if len(q_nums) > 1 else f"Q{q_nums[0]}"
            extra = f" ({notes})" if notes else ""
            self.add_event(f"[{profile}] ✅ Completed Batch {range_str}{extra}", "success")

    def record_question_complete(self, q_num: int, profile: str, notes: str = ""):
        with self._lock:
            self.completed_questions += 1
            self.in_flight_questions = max(0, self.in_flight_questions - 1)
            self.profile_states[profile] = {
                "status": "idle",
                "current_task": "",
                "updated_at": time.time(),
            }
            extra = f" ({notes})" if notes else ""
            self.add_event(f"[{profile}] ✅ Completed Question {q_num}{extra}", "success")

    def record_question_fail(self, q_num: int, profile: str, error: str):
        with self._lock:
            self.failed_questions += 1
            self.in_flight_questions = max(0, self.in_flight_questions - 1)
            self.profile_states[profile] = {
                "status": "idle",
                "current_task": "",
                "updated_at": time.time(),
            }
            self.add_event(f"[{profile}] ❌ Failed Question {q_num}: {error[:80]}", "error")

    def finish_job(self, status: str = "completed", msg: str = ""):
        with self._lock:
            self.stage = status
            for p in self.profile_states:
                self.profile_states[p]["status"] = "idle"
                self.profile_states[p]["current_task"] = ""
            if msg:
                self.add_event(msg, "success" if status == "completed" else "error")

    def add_event(self, msg: str, event_type: str = "info"):
        self.events.append({
            "time": datetime.datetime.now().strftime("%H:%M:%S"),
            "msg": msg,
            "type": event_type,
        })
        if len(self.events) > 50:
            self.events = self.events[-50:]

    def get_snapshot(self) -> Dict[str, Any]:
        with self._lock:
            elapsed = int(time.time() - self.start_time) if self.start_time else 0
            pct = 0
            if self.total_questions > 0:
                pct = int((self.completed_questions / self.total_questions) * 100)

            # Sync with actual profile list
            all_profiles = notebooklm_client.list_profiles()
            account_telemetry = []
            for p in all_profiles:
                p_name = p["name"]
                auth = p.get("authenticated", False)
                dis = p.get("disabled", False)
                rl = p.get("rate_limited")
                
                live_info = self.profile_states.get(p_name, {})
                live_status = live_info.get("status", "idle")
                task = live_info.get("current_task", "")

                if dis:
                    state = "disabled"
                elif not auth:
                    state = "logged_out"
                elif rl is not None:
                    state = "cooldown"
                elif live_status == "busy":
                    state = "busy"
                else:
                    state = "idle"

                account_telemetry.append({
                    "name": p_name,
                    "email": p.get("email", ""),
                    "state": state,
                    "current_task": task,
                    "rate_limited": rl,
                })

            return {
                "active_job_id": self.active_job_id,
                "filename": self.filename,
                "stage": self.stage,
                "total": self.total_questions,
                "completed": self.completed_questions,
                "in_flight": self.in_flight_questions,
                "failed": self.failed_questions,
                "percentage": pct,
                "elapsed_seconds": elapsed,
                "accounts": account_telemetry,
                "events": list(self.events),
            }


# Global telemetry instance
telemetry = LiveTelemetry()


# ─── Multi-Account Concurrency Manager ────────────────────────────────

class MultiProfilePoolManager:
    """
    Manages concurrent tasks across available authenticated NotebookLM accounts.
    Uses an asyncio.Queue to hand out available profiles and reclaims them upon task completion.
    """

    def __init__(self, preferred_profiles: Optional[List[str]] = None):
        self.preferred_profiles = preferred_profiles

    async def get_available_profiles(self) -> List[str]:
        profiles = notebooklm_client.list_profiles()
        available = []
        for p in profiles:
            if p.get("authenticated") and not p.get("disabled") and not p.get("rate_limited"):
                available.append(p["name"])
        
        # Fallback to default if no slave profiles are available
        if not available:
            available = ["default"]
        return available

    async def map_parallel(
        self,
        items: List[Any],
        process_func: Callable[[Any, str], Any],
        max_concurrency: Optional[int] = None,
    ) -> List[Any]:
        """
        Processes items concurrently across all available profiles.
        process_func signature: async def process_func(item, profile_name) -> result
        """
        if not items:
            return []
        available_profiles = await self.get_available_profiles()
        concurrency = min(len(available_profiles), len(items))
        if max_concurrency is not None:
            concurrency = min(concurrency, max_concurrency)
        log.info("MultiProfilePool: %d profiles available for %d items (concurrency=%d)", len(available_profiles), len(items), concurrency)

        queue: asyncio.Queue = asyncio.Queue()
        for p in available_profiles:
            await queue.put(p)

        results: List[Any] = [None] * len(items)
        errors: List[Optional[BaseException]] = [None] * len(items)
        sem = asyncio.Semaphore(concurrency if concurrency > 0 else 1)

        async def worker(index: int, item: Any):
            async with sem:
                profile = await queue.get()
                try:
                    res = await process_func(item, profile)
                    results[index] = res
                except BaseException as e:
                    log.exception("Worker %d failed on profile %s: %s", index, profile, e)
                    errors[index] = e
                    results[index] = e
                finally:
                    await queue.put(profile)

        tasks = [asyncio.create_task(worker(i, item)) for i, item in enumerate(items)]
        await asyncio.gather(*tasks)
        for idx, err in enumerate(errors):
            if err is not None:
                raise err
        return results


def chunk_into_batches(items: List[Any], batch_size: int, min_batch: int = 10) -> List[List[Any]]:
    """Split items into batches of batch_size, merging a trailing small remainder (< min_batch) into the previous batch."""
    if not items:
        return []
    batch_size = max(min_batch, batch_size)
    if len(items) <= batch_size:
        return [items]
    chunks: List[List[Any]] = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    if len(chunks) > 1 and len(chunks[-1]) < min_batch:
        chunks[-2].extend(chunks.pop())
    return chunks


def get_account_pool_status() -> Dict:
    """Return full status of all accounts (logged in, rate-limited, available)."""
    return telemetry.get_snapshot()