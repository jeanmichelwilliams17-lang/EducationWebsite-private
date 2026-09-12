"""
Clean all notebooks from all NotebookLM profiles except the protected profile 'Slave'.
"""
import asyncio
import io
import sys
from notebooklm import NotebookLMClient
from backend.notebooklm_client import list_profiles

# Ensure UTF-8 output on Windows console
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

PROTECTED_PROFILES = set()  # Set to empty to clean all disposable notebooks across all accounts


async def delete_single(client, nb_id: str, title: str):
    try:
        await client.notebooks.delete(nb_id)
        print(f"  [DELETED] {title} ({nb_id})", flush=True)
        return True
    except Exception as e:
        print(f"  [FAIL] {title} ({nb_id}): {e}", flush=True)
        return False


async def clean_profile(name: str, delete_all: bool = True):
    if name.strip().lower() in PROTECTED_PROFILES:
        print(f"[*] SKIPPING protected profile: '{name}'", flush=True)
        return {"profile": name, "deleted": 0, "status": "skipped_protected"}

    print(f"\n==================================================", flush=True)
    print(f"[*] Connecting to profile: '{name}'...", flush=True)

    try:
        async with NotebookLMClient.from_storage(profile=name) as client:
            notebooks = await client.notebooks.list()
            print(f"[*] Found {len(notebooks)} notebook(s) in profile '{name}'", flush=True)
            if not notebooks:
                print(f"[OK] Profile '{name}' is already clean (0 notebooks).", flush=True)
                return {"profile": name, "deleted": 0, "status": "already_clean"}

            tasks = []
            for nb in notebooks:
                nb_id = getattr(nb, "id", None) or getattr(nb, "notebook_id", str(nb))
                title = getattr(nb, "title", None) or getattr(nb, "name", "Untitled")
                # Delete all or only extraction disposable notebooks
                if delete_all or "extraction" in title.lower() or "untitled" in title.lower():
                    tasks.append(delete_single(client, nb_id, title))

            if tasks:
                results = await asyncio.gather(*tasks)
                success_count = sum(1 for r in results if r)
                print(f"[OK] Finished cleaning '{name}': {success_count}/{len(tasks)} deleted successfully.", flush=True)
                return {"profile": name, "deleted": success_count, "status": "cleaned"}
            return {"profile": name, "deleted": 0, "status": "no_matching"}
    except Exception as exc:
        print(f"[!] Profile '{name}' error: {exc}", flush=True)
        return {"profile": name, "deleted": 0, "error": str(exc)}


async def main():
    profiles = list_profiles()
    print(f"Total profiles to check: {len(profiles)}", flush=True)
    for p in profiles:
        await clean_profile(p["name"])
    print("\n==================================================", flush=True)
    print("All old notebooks deleted across all accounts!", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

