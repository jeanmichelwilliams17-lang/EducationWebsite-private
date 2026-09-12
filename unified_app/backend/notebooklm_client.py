"""
backend/notebooklm_client.py
Multi-account NotebookLM client: login, profile management, rate limiting,
and submission with auto-fallback across accounts.
"""

import asyncio
import csv
import io
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional, Tuple

import httpx

log = logging.getLogger("nlm.client")

# ─── Rate limit tracking ──────────────────────────────────────────────

_RATE_LIMITS_PATH = Path(__file__).parent.parent / "data" / "rate_limits.json"
_DEFAULT_COOLDOWN = 300
_SHORT_COOLDOWN = 120


def _load_rate_limits() -> dict:
    if _RATE_LIMITS_PATH.exists():
        try:
            return json.loads(_RATE_LIMITS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_rate_limits(data: dict) -> None:
    _RATE_LIMITS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _RATE_LIMITS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def mark_rate_limited(profile: str, reason: str = "", cooldown_seconds: Optional[int] = None) -> None:
    from datetime import datetime, timezone

    limits = _load_rate_limits()
    r_lower = reason.lower()
    cooldown = cooldown_seconds or (_SHORT_COOLDOWN if ("wait a few seconds" in r_lower or "try again" in r_lower) else _DEFAULT_COOLDOWN)
    limits[profile] = {
        "limited_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "reason": reason,
        "cooldown": cooldown,
    }
    _save_rate_limits(limits)
    log.warning("Profile %s marked as rate-limited (%ds cooldown): %s", profile, cooldown, reason)


def is_rate_limited(profile: str) -> Optional[dict]:
    from datetime import datetime, timezone

    limits = _load_rate_limits()
    entry = limits.get(profile)
    if not entry:
        return None
    limited_at = datetime.fromisoformat(entry["limited_at"].replace("Z", "+00:00"))
    cooldown = entry.get("cooldown", _DEFAULT_COOLDOWN)
    elapsed = (datetime.now(timezone.utc) - limited_at).total_seconds()
    if elapsed >= cooldown:
        del limits[profile]
        _save_rate_limits(limits)
        return None
    remaining = cooldown - elapsed
    hours, mins = int(remaining // 3600), int((remaining % 3600) // 60)
    human = f"{mins}m {remaining % 60}s" if hours == 0 else f"{hours}h {mins}m"
    return {
        "profile": profile,
        "limited_at": entry["limited_at"],
        "reason": entry.get("reason", ""),
        "remaining_seconds": int(remaining),
        "remaining_human": human,
    }


def clear_rate_limit(profile: str) -> bool:
    limits = _load_rate_limits()
    if profile in limits:
        del limits[profile]
        _save_rate_limits(limits)
        return True
    return False


def flag_profile_cooldown(profile: str, duration_hours: int = 24) -> None:
    mark_rate_limited(profile, f"Manually flagged {duration_hours}h cooldown", cooldown_seconds=duration_hours * 3600)


def get_all_rate_limits() -> list:
    limits = _load_rate_limits()
    results = []
    for profile in list(limits.keys()):
        info = is_rate_limited(profile)
        if info:
            results.append(info)
    return results


# ─── Disabled profile management ─────────────────────────────────────

_DISABLED_PROFILES_PATH = Path(__file__).parent.parent / "data" / "disabled_profiles.json"


def _load_disabled() -> set:
    if _DISABLED_PROFILES_PATH.exists():
        try:
            return set(json.loads(_DISABLED_PROFILES_PATH.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def _save_disabled(disabled: set) -> None:
    _DISABLED_PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DISABLED_PROFILES_PATH.write_text(
        json.dumps(sorted(disabled), indent=2, ensure_ascii=False), encoding="utf-8"
    )


def is_profile_disabled(name: str) -> bool:
    return name in _load_disabled()


def set_profile_disabled(name: str, disabled: bool) -> None:
    d = _load_disabled()
    if disabled:
        d.add(name)
    else:
        d.discard(name)
    _save_disabled(d)
    log.info("Profile '%s' %s.", name, "disabled" if disabled else "enabled")


# ─── Profile management ───────────────────────────────────────────────

def list_profiles() -> list:
    """List all saved notebooklm-py profiles with auth status, email, and rate-limit info."""
    try:
        from notebooklm.paths import list_profiles as _list, get_storage_path
        from notebooklm.auth import read_account_metadata
    except ImportError:
        return []

    disabled_set = _load_disabled()
    profiles = []
    for name in _list():
        storage = get_storage_path(profile=name)
        metadata = read_account_metadata(storage) if storage.exists() else {}
        email = metadata.get("email", "")
        rl = is_rate_limited(name)
        # Only mark as authenticated if session file exists AND email was recorded
        authenticated = storage.exists() and bool(email)
        profiles.append({
            "name": name,
            "authenticated": authenticated,
            "has_storage": storage.exists(),
            "email": email,
            "rate_limited": rl,
            "disabled": name in disabled_set,
        })
    return profiles


def check_profile(profile_name: str) -> dict:
    """Live-check a profile by attempting to fetch CSRF tokens."""
    try:
        from notebooklm.paths import get_storage_path
        from notebooklm.auth import read_account_metadata, fetch_tokens_with_domains
    except ImportError:
        return {"name": profile_name, "ok": False, "reason": "notebooklm-py not installed"}

    storage = get_storage_path(profile=profile_name)
    metadata = read_account_metadata(storage) if storage.exists() else {}
    base = {
        "name": profile_name,
        "email": metadata.get("email", ""),
        "ok": False,
        "reason": "",
    }

    if not storage.exists():
        base["reason"] = "No login session"
        return base

    try:
        import concurrent.futures

        def _fetch():
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(fetch_tokens_with_domains(profile=profile_name))
            finally:
                loop.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(_fetch)
            csrf, session_id = future.result(timeout=15)

        if csrf and session_id:
            base["ok"] = True
            base["reason"] = "Valid"
        else:
            base["reason"] = "Token fetch returned empty"
    except Exception as e:
        base["reason"] = str(e)[:120]
    return base


# ─── Browser detection ────────────────────────────────────────────────

COMMON_PATHS = {
    "chrome": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ],
    "msedge": [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
}


def _find_browser(user_choice: Optional[str] = None) -> tuple:
    import os, shutil

    def _find_exe(name: str):
        path = shutil.which(name)
        if path:
            return path
        for candidate in COMMON_PATHS.get(name, []):
            if os.path.isfile(candidate):
                return candidate
        return None

    if user_choice:
        if user_choice in ("chrome", "msedge"):
            return (user_choice, _find_exe(user_choice))
        return (user_choice, None)

    for name in ("chrome", "msedge"):
        exe = _find_exe(name)
        if exe:
            return (name, exe)
    return ("chromium", None)


# ─── Cookie extraction fallback ───────────────────────────────────────

def _extract_cookies_from_browser_db(browser_profile: Path, browser_name: str) -> list | None:
    import tempfile, sqlite3, shutil

    cookie_db = browser_profile / "Default" / "Network" / "Cookies"
    if not cookie_db.exists():
        return None
    try:
        tmp = Path(tempfile.mktemp(suffix=".db"))
        shutil.copy2(str(cookie_db), str(tmp))
        conn = sqlite3.connect(str(tmp))
        cursor = conn.execute(
            "SELECT name, value, host_key, path, expires_utc, httponly, is_secure, samesite FROM cookies WHERE host_key LIKE '%google%'"
        )
        pw_cookies = []
        for row in cursor:
            pw_cookies.append({
                "name": row[0], "value": row[1], "domain": row[2],
                "path": row[3], "expires": row[4] if row[4] else -1,
                "httpOnly": bool(row[5]), "secure": bool(row[6]),
                "sameSite": {0: "None", 1: "Lax", 2: "Strict"}.get(row[7], "Lax"),
            })
        conn.close()
        tmp.unlink(missing_ok=True)
        return pw_cookies if pw_cookies else None
    except Exception:
        return None


# ─── Login ─────────────────────────────────────────────────────────────

def login_profile(profile_name: str, browser_choice: Optional[str] = None, timeout_seconds: int = 300) -> dict:
    """
    Launch Chrome browser context and capture session cookies for a profile.
    Saves cookies and account metadata for future API use.
    Returns {ok: bool, message: str, email: str}.
    """
    try:
        from notebooklm.paths import get_storage_path, get_browser_profile_dir
        from notebooklm.cli.services.playwright_login import (
            filter_storage_state_cookies_by_domain_policy,
        )
        from notebooklm.auth import enumerate_accounts, write_account_metadata
    except ImportError:
        return {"ok": False, "message": "notebooklm-py or playwright not installed."}

    storage_path = get_storage_path(profile=profile_name)
    browser_profile = get_browser_profile_dir(profile=profile_name)
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    browser_profile.mkdir(parents=True, exist_ok=True)

    channel, exec_path = _find_browser(browser_choice or "chrome")

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            launch_args = {
                "headless": False,
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
            }
            if exec_path:
                launch_args["executable_path"] = exec_path

            context = p.chromium.launch_persistent_context(
                str(browser_profile),
                channel=channel if not exec_path else None,
                **launch_args,
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto("https://notebooklm.google.com/", wait_until="commit", timeout=10000)
            except Exception:
                pass

            log.info("Chrome browser opened for profile '%s'. Waiting for login/dashboard...", profile_name)

            deadline = time.time() + timeout_seconds
            logged_in = False
            while time.time() < deadline:
                try:
                    if page.is_closed() or not context.pages:
                        break
                    current = page.url
                    if "notebooklm.google.com" in current and "accounts.google.com" not in current:
                        logged_in = True
                        time.sleep(1.0)  # Allow Google session cookies to finish setting
                        break
                except Exception as ex:
                    if "closed" in str(ex).lower():
                        break
                time.sleep(0.5)

            storage_state = None
            try:
                storage_state = context.storage_state()
            except Exception as e:
                log.warning("Could not capture live storage state: %s", e)

            try:
                context.close()
            except Exception:
                pass

            cookies_list = []
            if storage_state and isinstance(storage_state, dict):
                cookies_list = storage_state.get("cookies", [])

            if not cookies_list:
                db_cookies = _extract_cookies_from_browser_db(browser_profile, channel)
                if db_cookies:
                    cookies_list = db_cookies

            if not cookies_list:
                return {
                    "ok": False,
                    "message": f"Could not capture Google session cookies for '{profile_name}'. Make sure NotebookLM dashboard is open.",
                }

            filtered = filter_storage_state_cookies_by_domain_policy({"cookies": cookies_list, "origins": []})
            storage_path.write_text(json.dumps(filtered, indent=2, ensure_ascii=False), encoding="utf-8")

        # Extract account metadata
        stored = json.loads(storage_path.read_text(encoding="utf-8"))
        cookie_jar = httpx.Cookies()
        for c in stored.get("cookies", []):
            cookie_jar.set(c["name"], c["value"], domain=c.get("domain", ""))

        accounts = []
        try:
            accounts = asyncio.run(enumerate_accounts(cookie_jar))
        except Exception:
            pass

        email = ""
        if accounts:
            acc = accounts[0]
            write_account_metadata(storage_path, authuser=acc.authuser, email=acc.email)
            email = acc.email or ""

        log.info("Profile '%s' logged in successfully as %s", profile_name, email)
        return {"ok": True, "message": f"Logged in as {email}" if email else "Session cookies saved", "email": email}


    except Exception as e:
        log.exception("Login failed for profile '%s'", profile_name)
        return {"ok": False, "message": str(e)}


def batch_login_profiles(profile_names: Optional[list] = None, timeout_seconds: int = 20, browser_choice: Optional[str] = None) -> list:
    """
    Automates login/cookie refresh across multiple profiles sequentially.
    Ideal for pre-logged-in browser profiles (e.g. Slave1, Slave2, slave3, etc.).
    """
    try:
        if not profile_names:
            profiles = list_profiles()
            profile_names = [p["name"] for p in profiles if p["name"] != "default"]
    except Exception as e:
        log.exception("Failed to list profiles for auto-sync")
        return [{"name": "unknown", "ok": False, "message": f"Failed to list profiles: {e}"}]

    if not profile_names:
        return []

    results = []
    for name in profile_names:
        try:
            log.info("Auto-syncing login for profile '%s'...", name)
            res = login_profile(name, browser_choice=browser_choice, timeout_seconds=timeout_seconds)
            results.append({"name": name, **res})
        except Exception as e:
            log.exception("Auto-sync crashed for profile '%s'", name)
            results.append({"name": name, "ok": False, "message": str(e)[:300]})
    return results


# ─── Profile selection with fallback ──────────────────────────────────

def find_backup_profile(primary: str = "", exclude: Optional[set] = None) -> Optional[str]:
    import random
    disabled = _load_disabled()
    exclude = exclude or set()
    try:
        from notebooklm.paths import list_profiles
        candidates = [n for n in list_profiles() if n != primary and n not in disabled and n not in exclude and is_rate_limited(n) is None]
        random.shuffle(candidates)
        for name in candidates:
            chk = check_profile(name)
            if chk.get("ok"):
                return name
    except Exception:
        pass
    return None


_find_backup_profile = find_backup_profile



def get_working_profile(requested: str = "") -> str:
    """
    Return a working, authenticated, non-rate-limited profile.
    If requested is valid and authenticated, returns requested.
    Otherwise finds the first valid authenticated profile available.
    """
    if requested and is_rate_limited(requested) is None:
        chk = check_profile(requested)
        if chk.get("ok"):
            return requested

    backup = _find_backup_profile(requested)
    if backup:
        log.info("Profile '%s' is not authenticated or is rate-limited. Switched to '%s'.", requested or "default", backup)
        return backup

    try:
        from notebooklm.paths import list_profiles as _list
        for name in _list():
            chk = check_profile(name)
            if chk.get("ok"):
                log.info("Using authenticated profile '%s'.", name)
                clear_rate_limit(name)
                return name
    except Exception:
        pass

    return requested or "default"


# ─── Auth expired detection ───────────────────────────────────────────

_AUTH_EXPIRED_PHRASES = [
    "authentication expired", "authentication invalid", "redirected to",
    "accounts.google.com", "re-authenticate", "re authenticate",
    "session expired", "cookie", "unauthenticated",
    "status code 16", "rpc_code=16",
]


class AuthExpiredError(Exception):
    """Raised when NotebookLM authentication has expired and re-login is needed."""
    pass


def _is_auth_expired(error_msg: str) -> bool:
    lower = error_msg.lower()
    return any(p in lower for p in _AUTH_EXPIRED_PHRASES)


# ─── Core submission ──────────────────────────────────────────────────

_RATED_LIMIT_PHRASES = [
    "rate limited", "rate limit", "too many requests",
    "quota exceeded", "wait a few seconds",
]


def _is_rate_limit(error_msg: str) -> bool:
    return any(p in error_msg.lower() for p in _RATED_LIMIT_PHRASES)


async def _submit_async(
    profile: str,
    source_path,
    prompt: str,
    variables: dict,
    output_label: str,
    job_id: str = "",
) -> Tuple[bool, str, str]:
    """
    Core async submission to NotebookLM.
    Returns (success, response_text, error_message).
    """
    from notebooklm import NotebookLMClient
    from notebooklm.rpc import RPCMethod
    from notebooklm.paths import get_storage_path, get_browser_profile_dir
    from notebooklm.cli.services.playwright_login import (
        filter_storage_state_cookies_by_domain_policy,
    )

    resolved_prompt = prompt
    label_lower = (output_label or "").lower()
    if any(k in label_lower for k in ("extract", "pdf", "whole")):
        task_chat_timeout = 600  # 10 minutes for full PDF extractions
        task_upload_timeout = 180 # 3 minutes for PDF file ingestion
    elif any(k in label_lower for k in ("solve", "reword", "val", "refine", "answer")):
        task_chat_timeout = 360  # 6 minutes for 10-question LaTeX derivation batches
        task_upload_timeout = 90
    else:
        task_chat_timeout = 300  # 5 minutes generous default
        task_upload_timeout = 60

    async with NotebookLMClient.from_storage(profile=profile, chat_timeout=task_chat_timeout) as client:
        nb = await asyncio.wait_for(client.notebooks.create(f"Chatter — {output_label}"), timeout=45)
        notebook_id = nb.id
        answer_text = ""
        try:
            if source_path:
                source_paths = [source_path] if isinstance(source_path, str) else list(source_path)
                source_paths = [p for p in source_paths if p and Path(p).exists()]
                for idx, up_path in enumerate(source_paths):
                    for attempt in range(3):
                        try:
                            await asyncio.wait_for(client.sources.add_file(notebook_id, up_path, wait=True), timeout=task_upload_timeout)
                            break
                        except Exception as src_err:
                            if attempt < 2:
                                await asyncio.sleep(2)
                            else:
                                raise
                await asyncio.sleep(1)
            response = await asyncio.wait_for(client.chat.ask(notebook_id, resolved_prompt), timeout=task_chat_timeout)
            answer_text = getattr(response, "answer", None) or getattr(response, "raw_response", "") or str(response)
            if not answer_text or len(answer_text.strip()) < 500 or ("saved" in answer_text.lower() and "document" in answer_text.lower()):
                try:
                    from backend.ai_provider import extract_answer_from_raw_stream as _extract_raw
                    fallback = _extract_raw(getattr(response, "raw_response", "") or "")
                    if fallback and len(fallback) > len(answer_text):
                        answer_text = fallback
                except Exception:
                    pass
                if "~~~" not in answer_text:
                    try:
                        notes = await client.notes.list(notebook_id)
                        best = ""
                        for n in notes:
                            txt = getattr(n, "content", None) or getattr(n, "text", None) or getattr(n, "full_text", None) or str(n)
                            if "~~~" in txt and len(txt) > len(best):
                                best = txt
                        if best:
                            answer_text = best
                    except Exception:
                        pass
            return True, answer_text, ""
        except Exception as e:
            err_str = str(e)
            if _is_rate_limit(err_str):
                mark_rate_limited(profile, err_str)
            if _is_auth_expired(err_str):
                raise AuthExpiredError(err_str) from e
            raise
        finally:
            try:
                await client.rpc_call(RPCMethod.DELETE_NOTEBOOK, [notebook_id])
            except Exception:
                try:
                    await client.notebooks.delete(notebook_id)
                except Exception:
                    pass


def submit(
    profile: str,
    source_path,
    prompt: str,
    variables: Optional[dict] = None,
    output_label: str = "extract",
    job_id: str = "",
    _retried_auth: bool = False,
) -> Tuple[bool, str, str]:
    """
    Synchronous wrapper around the async submission.
    Handles rate-limit detection, profile switching, and auto-fallback on auth errors.
    Returns (success, response_text, error_message).
    """
    profile = get_working_profile(profile)

    try:
        success, text, err = asyncio.run(
            _submit_async(profile, source_path, prompt, variables or {}, output_label, job_id)
        )
        return success, text, err
    except AuthExpiredError as e:
        err_str = str(e)
        backup = _find_backup_profile(profile, exclude={profile})
        if backup and not _retried_auth:
            log.warning("Auth expired for '%s' (%s) -> falling back to '%s'...", profile, err_str[:80], backup)
            try:
                from backend.batch_scheduler import telemetry as _tel
                _tel.add_event(f"[{profile}] AuthExpired -> [{backup}]", "error")
            except Exception:
                pass
            return submit(backup, source_path, prompt, variables, output_label, job_id, _retried_auth=True)
        log.error("Auth expired for '%s' and no valid backup available.", profile)
        return False, "", f"Authentication expired for profile '{profile}'. Please re-login."
    except Exception as e:
        err_str = str(e)
        if _is_rate_limit(err_str):
            mark_rate_limited(profile, err_str)
            backup = _find_backup_profile(profile, exclude={profile})
            if backup:
                log.warning("Rate limit hit for '%s' -> backing up to '%s'", profile, backup)
                try:
                    success, text, err = asyncio.run(
                        _submit_async(backup, source_path, prompt, variables or {}, output_label, job_id)
                    )
                    return success, text, err
                except Exception as e2:
                    return False, "", str(e2)
        return False, "", err_str