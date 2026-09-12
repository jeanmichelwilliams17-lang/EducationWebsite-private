"""
backend/batch_scheduler.py
Multi-account worker pool and batch queue scheduler.
Manages concurrent execution across all authenticated Google accounts with SQLite-backed load balancing,
cooldown tracking, and accurate real-time telemetry.
"""

import asyncio
import datetime
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from backend import notebooklm_client
from backend import account_db

log = logging.getLogger("nlm.batch")

# ─── Live Telemetry State ─────────────────────────────────────────────

class LiveTelemetry:
    """Thread-safe store for real-time extraction telemetry."""

    def __init__(self):
        self._lock = threading.Lock()
        self.active_job_id: Optional[str] = None
        self.filename: str = ""
        self.stage: str = "idle"  # idle, extracting, awaiting_screenshots, rewording, solving, validating, fixing, rechecking, completed, failed
        self.stage_label: str = ""
        self.total_questions: int = 0
        self.completed_questions: int = 0
        self.in_flight_questions: int = 0
        self.failed_questions: int = 0
        self.overall_pct: int = 0
        self.profile_states: Dict[str, Dict[str, Any]] = {}
        self.events: List[Dict[str, Any]] = []
        self.start_time: Optional[float] = None

    def reset(self):
        with self._lock:
            self.active_job_id = None
            self.filename = ""
            self.stage = "idle"
            self.stage_label = ""
            self.total_questions = 0
            self.completed_questions = 0
            self.in_flight_questions = 0
            self.failed_questions = 0
            self.overall_pct = 0
            self.profile_states = {}
            self.events = []
            self.start_time = None

    def start_job(self, job_id: str, filename: str, total_questions: int):
        with self._lock:
            self.active_job_id = job_id
            self.filename = filename
            self.stage = "extracting"
            self.stage_label = "Extracting from PDF"
            self.total_questions = total_questions
            self.completed_questions = 0
            self.in_flight_questions = 0
            self.failed_questions = 0
            self.overall_pct = 5 if total_questions == 0 else 0
            self.start_time = time.time()
            self.events = [{
                "time": datetime.datetime.now().strftime("%H:%M:%S"),
                "msg": f"Started processing {filename}" + (f" ({total_questions} questions)" if total_questions else ""),
                "type": "info"
            }]

    def set_stage(self, stage: str, msg: str = "", total_for_stage: Optional[int] = None, pct_override: Optional[int] = None):
        with self._lock:
            self.stage = stage
            if msg:
                self.stage_label = msg
                self.add_event(msg, "info")
            if total_for_stage is not None:
                self.total_questions = total_for_stage
                self.completed_questions = 0
                self.in_flight_questions = 0
            if pct_override is not None:
                self.overall_pct = max(0, min(100, int(pct_override)))

    def update_profile(self, profile: str, status: str, task_desc: str = ""):
        with self._lock:
            self.profile_states[profile] = {
                "status": status,  # "idle", "busy", "cooldown", "disabled"
                "current_task": task_desc,
                "updated_at": time.time(),
            }

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
        try:
            account_db.record_batch_start(profile, count)
        except Exception as e:
            log.warning("Failed to record batch start in DB: %s", e)

    def record_batch_complete(self, q_nums: List[int], profile: str, notes: str = "", success: bool = True):
        with self._lock:
            count = len(q_nums)
            self.completed_questions = min(self.total_questions, self.completed_questions + count)
            self.in_flight_questions = max(0, self.in_flight_questions - count)
            self.profile_states[profile] = {
                "status": "idle",
                "current_task": "",
                "updated_at": time.time(),
            }
            range_str = f"Q{q_nums[0]}–Q{q_nums[-1]}" if len(q_nums) > 1 else f"Q{q_nums[0]}"
            extra = f" ({notes})" if notes else ""
            self.add_event(f"[{profile}] ✅ Completed Batch {range_str}{extra}", "success" if success else "error")
        try:
            account_db.record_batch_complete(profile, success)
        except Exception as e:
            log.warning("Failed to record batch complete in DB: %s", e)

    def finish_job(self, status: str = "completed", msg: str = ""):
        with self._lock:
            self.stage = status
            self.overall_pct = 100 if status == "completed" else self.overall_pct
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
            
            # Clamp percentage strictly between 0 and 100
            if self.stage == "completed":
                pct = 100
            elif self.total_questions > 0:
                calc = int((self.completed_questions / self.total_questions) * 100)
                pct = min(100, max(0, calc))
            else:
                pct = min(100, max(0, self.overall_pct))

            # Sync with actual profile list and account_db usage
            all_profiles = notebooklm_client.list_profiles()
            db_stats = {s["profile_name"]: s for s in account_db.get_all_profile_stats()}
            account_telemetry = []
            
            for p in all_profiles:
                p_name = p["name"]
                auth = p.get("authenticated", False)
                dis = p.get("disabled", False)
                rl = p.get("rate_limited")
                
                live_info = self.profile_states.get(p_name, {})
                live_status = live_info.get("status", "idle")
                task = live_info.get("current_task", "")
                stats = db_stats.get(p_name, {})

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
                    "email": p.get("email", "") or stats.get("email", ""),
                    "state": state,
                    "current_task": task,
                    "rate_limited": rl,
                    "total_batches": stats.get("total_batches", 0),
                    "total_questions": stats.get("total_questions", 0),
                })

            return {
                "active_job_id": self.active_job_id,
                "filename": self.filename,
                "stage": self.stage,
                "stage_label": self.stage_label,
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
    Uses SQLite-backed load balancing to prioritize least-used accounts across the pool.
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
            return ["default"]
        
        # Sort profiles by usage count in SQLite DB (least used first)
        ordered = account_db.get_profiles_ordered_by_usage(available)
        return ordered

    async def map_parallel(
        self,
        items: List[Any],
        process_func: Callable[[Any, str], Any],
        max_concurrency: Optional[int] = None,
    ) -> List[Any]:
        """
        Processes items concurrently across all available profiles, rotating via SQLite DB.
        process_func signature: async def process_func(item, profile_name) -> result
        """
        if not items:
            return []
        available_profiles = await self.get_available_profiles()
        concurrency = min(len(available_profiles), len(items))
        if max_concurrency is not None:
            concurrency = min(concurrency, max_concurrency)
        log.info("MultiProfilePool: %d profiles available for %d items (concurrency=%d, order=%s)",
                 len(available_profiles), len(items), concurrency, available_profiles[:5])

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
    """Split items into batches of batch_size, splitting remainder evenly instead of ballooning last batch."""
    if not items:
        return []
    batch_size = max(min_batch, batch_size)
    if len(items) <= batch_size:
        return [items]
    chunks: List[List[Any]] = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    if len(chunks) > 1 and len(chunks[-1]) < min_batch:
        remainder = chunks.pop()
        last = chunks.pop()
        merged = last + remainder
        mid = len(merged) // 2
        chunks.extend([merged[:mid], merged[mid:]])
    return chunks


def get_account_pool_status() -> Dict:
    """Return full status of all accounts (logged in, rate-limited, available)."""
    return telemetry.get_snapshot()