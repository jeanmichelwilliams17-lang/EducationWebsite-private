"""
Simple NotebookLM account login & auth check script.

Usage:
    python simple_login.py              # Login to default profile (auto-detects browser)
    python simple_login.py work         # Login to 'work' profile
    python simple_login.py --list       # List all profiles (static check)
    python simple_login.py --check      # Check ALL profiles (live token fetch)
    python simple_login.py --check work # Check one profile (live token fetch)
    python simple_login.py --fix-metadata      # Fix account metadata for all profiles
    python simple_login.py --fix-metadata work  # Fix metadata for one profile
    python simple_login.py --browser chrome  # Force Chrome
    python simple_login.py --browser msedge  # Force Edge
"""

import asyncio
import json
import shutil
import sys
from pathlib import Path

from notebooklm.paths import (
    get_browser_profile_dir,
    get_storage_path,
    list_profiles,
    resolve_profile,
)
from notebooklm.auth import read_account_metadata


# ---------------------------------------------------------------------------
# Auth check
# ---------------------------------------------------------------------------


def _check_profile_live(name: str) -> dict:
    """Attempt to fetch CSRF tokens for a profile. Returns status dict."""
    storage = get_storage_path(profile=name)
    metadata = read_account_metadata(storage) if storage.exists() else {}
    email = metadata.get("email", "")
    base = {"name": name, "email": email, "has_storage": storage.exists()}

    if not storage.exists():
        return {**base, "ok": False, "reason": "No login session found"}

    try:
        from notebooklm.auth import fetch_tokens_with_domains

        csrf, session_id = asyncio.run(fetch_tokens_with_domains(profile=name))
        if csrf and session_id:
            return {**base, "ok": True, "reason": "Valid"}
        return {**base, "ok": False, "reason": "Token fetch returned empty"}
    except Exception as e:
        return {**base, "ok": False, "reason": str(e)[:80]}


def check_profiles(names: list[str] | None = None):
    """Check auth status for profiles (live token validation)."""
    targets = names if names else list_profiles()
    if not targets:
        print("No profiles found. Run: python simple_login.py <profile_name>")
        return

    print(f"{'Profile':<18} {'Email':<32} {'Status':<10} {'Details'}")
    print("-" * 90)

    all_ok = True
    for name in targets:
        r = _check_profile_live(name)
        status = "OK" if r["ok"] else "FAIL"
        if not r["ok"]:
            all_ok = False
        print(f"{r['name']:<18} {r['email']:<32} {status:<10} {r['reason']}")

    print()
    if all_ok:
        print("All profiles authenticated.")
    else:
        failed = [n for n in targets if not _check_profile_live(n)["ok"]]
        print(f"Re-login failed profiles: python simple_login.py {' '.join(failed)}")


# ---------------------------------------------------------------------------
# Profile list
# ---------------------------------------------------------------------------


def list_all_profiles():
    """Print all existing profiles with static auth status."""
    profiles = list_profiles()
    active = resolve_profile()
    if not profiles:
        print("No profiles found. Create one by logging in:")
        print("  python simple_login.py <profile_name>")
        return
    print(f"{'Profile':<20} {'Status':<15} {'Email':<35} {'Active'}")
    print("-" * 85)
    for name in profiles:
        storage = get_storage_path(profile=name)
        metadata = read_account_metadata(storage) if storage.exists() else {}
        authenticated = "Logged in" if storage.exists() else "NOT logged in"
        email = metadata.get("email", "")
        is_active = " <--" if name == active else ""
        print(f"{name:<20} {authenticated:<15} {email:<35}{is_active}")


# ---------------------------------------------------------------------------
# Browser detection
# ---------------------------------------------------------------------------

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


def _find_browser(user_choice: str | None = None) -> tuple[str, str | None]:
    """Find a usable browser. Returns (channel, executable_path_or_None)."""
    import os

    def _find_exe(name: str) -> str | None:
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


# ---------------------------------------------------------------------------
# Cookie extraction fallback (reads browser cookie DB directly)
# ---------------------------------------------------------------------------


def _extract_cookies_from_browser_db(
    browser_profile: Path, browser_name: str
) -> list[dict] | None:
    """Try to read Google cookies directly from the browser's SQLite cookie DB."""
    import os
    import tempfile

    if browser_name == "chrome":
        cookie_db = browser_profile / "Default" / "Network" / "Cookies"
    elif browser_name == "msedge":
        cookie_db = browser_profile / "Default" / "Network" / "Cookies"
    else:
        return None

    if not cookie_db.exists():
        return None

    try:
        import rookiepy

        # rookiepy can extract from a specific profile path
        cookies = rookiepy.chrome(
            domains=["google.com", "notebooklm.google.com", "accounts.google.com"],
            browser_profile=str(browser_profile),
        )
        # Convert rookiepy format to Playwright storage_state format
        pw_cookies = []
        for c in cookies:
            pw_cookies.append(
                {
                    "name": c.get("name", ""),
                    "value": c.get("value", ""),
                    "domain": c.get("domain", ""),
                    "path": c.get("path", "/"),
                    "expires": c.get("expirationDate", -1),
                    "httpOnly": c.get("httpOnly", False),
                    "secure": c.get("secure", False),
                    "sameSite": c.get("sameSite", "Lax"),
                }
            )
        return pw_cookies
    except ImportError:
        pass
    except Exception:
        pass

    # Fallback: try sqlite3 directly (unencrypted cookies only)
    try:
        import sqlite3
        import shutil as _shutil

        tmp = Path(tempfile.mktemp(suffix=".db"))
        _shutil.copy2(str(cookie_db), str(tmp))
        conn = sqlite3.connect(str(tmp))
        cursor = conn.execute(
            "SELECT name, value, host_key, path, expires_utc, httponly, is_secure, samesite "
            "FROM cookies WHERE host_key LIKE '%google%'"
        )
        pw_cookies = []
        for row in cursor:
            pw_cookies.append(
                {
                    "name": row[0],
                    "value": row[1],
                    "domain": row[2],
                    "path": row[3],
                    "expires": row[4] if row[4] else -1,
                    "httpOnly": bool(row[5]),
                    "secure": bool(row[6]),
                    "sameSite": {0: "None", 1: "Lax", 2: "Strict"}.get(row[7], "Lax"),
                }
            )
        conn.close()
        tmp.unlink(missing_ok=True)
        return pw_cookies if pw_cookies else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


def do_login(profile_name: str | None, browser_choice: str | None = None):
    """Open browser, wait for user to log in, then save cookies."""
    from notebooklm.cli.services.playwright_login import (
        filter_storage_state_cookies_by_domain_policy,
    )

    profile = resolve_profile(profile_name)
    storage_path = get_storage_path(profile=profile)
    browser_profile = get_browser_profile_dir(profile=profile)

    storage_path.parent.mkdir(parents=True, exist_ok=True)
    browser_profile.mkdir(parents=True, exist_ok=True)

    channel, exec_path = _find_browser(browser_choice)

    print(f"Profile:   {profile}")
    print(f"Browser:   {channel}")
    print(f"Storage:   {storage_path}")
    print()

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

        # Don't wait for page load — just open the browser
        try:
            page.goto("https://accounts.google.com/", wait_until="commit", timeout=5000)
        except Exception:
            pass

        print("=" * 50)
        print("A browser window has opened.")
        print()
        print("1. Go to: https://notebooklm.google.com")
        print("2. Log in to your Google account")
        print("3. Make sure you see the NotebookLM dashboard")
        print("4. Come back here and press Enter")
        print()
        print("DO NOT close the browser window.")
        print("=" * 50)
        print()
        input("Press Enter when NotebookLM is loaded...")

        # Try to capture cookies from the live browser
        storage_state = None
        try:
            storage_state = context.storage_state()
        except Exception as e:
            print(f"Could not capture from live browser: {e}")

        if storage_state:
            filtered = filter_storage_state_cookies_by_domain_policy(
                dict(storage_state)
            )
            storage_path.parent.mkdir(parents=True, exist_ok=True)
            storage_path.write_text(
                json.dumps(filtered, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            cookie_count = len(filtered.get("cookies", []))
            print(f"Saved {cookie_count} cookies to {storage_path}")
        else:
            print("Trying to extract cookies from browser database...")
            cookies = _extract_cookies_from_browser_db(browser_profile, channel)
            if cookies:
                filtered = filter_storage_state_cookies_by_domain_policy(
                    {"cookies": cookies, "origins": []}
                )
                storage_path.parent.mkdir(parents=True, exist_ok=True)
                storage_path.write_text(
                    json.dumps(filtered, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                cookie_count = len(filtered.get("cookies", []))
                print(f"Extracted {cookie_count} cookies from browser database")
            else:
                print()
                print("ERROR: Could not capture cookies.")
                print("Make sure the browser is still open and you are logged in.")
                try:
                    context.close()
                except Exception:
                    pass
                return

        try:
            context.close()
        except Exception:
            pass

    # Extract account info from cookies and save metadata
    import httpx
    from notebooklm.auth import enumerate_accounts, write_account_metadata

    stored = json.loads(storage_path.read_text(encoding="utf-8"))
    cookie_jar = httpx.Cookies()
    for c in stored.get("cookies", []):
        cookie_jar.set(c["name"], c["value"], domain=c.get("domain", ""))

    accounts = asyncio.run(enumerate_accounts(cookie_jar))
    if accounts:
        acc = accounts[0]
        write_account_metadata(storage_path, authuser=acc.authuser, email=acc.email)
        print(f"Account:   {acc.email or '(unknown)'} (authuser={acc.authuser})")
        if len(accounts) > 1:
            print(f"Note: {len(accounts)} accounts found in this cookie jar.")
            print(f"      Using the first one. Others may cause routing conflicts.")
    else:
        print("Account:   (could not detect account from cookies)")

    print(f"Done! Try: python simple_login.py --check {profile}")


def fix_metadata(names: list[str] | None = None):
    """Detect and save account metadata for existing profiles."""
    import httpx
    from notebooklm.auth import enumerate_accounts, write_account_metadata

    targets = names if names else list_profiles()
    if not targets:
        print("No profiles found.")
        return

    for name in targets:
        p = get_storage_path(profile=name)
        if not p.exists():
            print(f"{name}: no storage file, skipping")
            continue

        stored = json.loads(p.read_text(encoding="utf-8"))
        jar = httpx.Cookies()
        for c in stored.get("cookies", []):
            jar.set(c["name"], c["value"], domain=c.get("domain", ""))

        try:
            accounts = asyncio.run(enumerate_accounts(jar))
            if accounts:
                acc = accounts[0]
                write_account_metadata(p, authuser=acc.authuser, email=acc.email)
                print(f"{name}: {acc.email or '(unknown)'} (authuser={acc.authuser})")
                if len(accounts) > 1:
                    print(f"  WARNING: {len(accounts)} accounts in this profile!")
                    for a in accounts:
                        print(f"    authuser={a.authuser}: {a.email}")
                    print(f"  Using first one. Re-login with only ONE account to fix.")
            else:
                print(f"{name}: no valid accounts found (cookies may be expired)")
        except Exception as e:
            print(f"{name}: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    args = sys.argv[1:]
    browser = None

    if "--browser" in args:
        idx = args.index("--browser")
        if idx + 1 < len(args):
            browser = args[idx + 1]
            args = args[:idx] + args[idx + 2 :]
        else:
            print("--browser requires a value: chrome, msedge, or chromium")
            sys.exit(1)

    if not args:
        do_login(None, browser)
    elif args[0] in ("--list", "-l", "list"):
        list_all_profiles()
    elif args[0] in ("--check", "-c"):
        targets = args[1:] if len(args) > 1 else None
        check_profiles(targets)
    elif args[0] == "--fix-metadata":
        targets = args[1:] if len(args) > 1 else None
        fix_metadata(targets)
    elif args[0] in ("--help", "-h"):
        print(__doc__)
    else:
        do_login(args[0], browser)


if __name__ == "__main__":
    main()
