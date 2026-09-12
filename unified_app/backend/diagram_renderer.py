"""
backend/diagram_renderer.py
Renders code-based diagrams into PNG images using:
- Matplotlib (subprocess python)
- Mermaid (Playwright headless Chromium rendering)
- SVG (Playwright headless Chromium rendering)
"""

import os
import re
import sys
import base64
import logging
import tempfile
import subprocess
from pathlib import Path
from typing import Tuple

log = logging.getLogger("extraction.diagram_renderer")

DISALLOWED_MATPLOTLIB_MODULES = [
    "os", "subprocess", "shutil", "sys", "socket", "urllib", "requests", "http",
    "webbrowser", "ctypes", "multiprocessing", "pty", "commands", "builtin"
]

def clean_code_snippet(raw_code: str, format_type: str = "") -> str:
    """Extract code from markdown code fences or raw text."""
    if not raw_code:
        return ""
    code = raw_code.strip()
    
    # Check for markdown code blocks
    fence_pattern = r"```(?:[a-zA-Z0-9_\-\+]*)\n(.*?)```"
    matches = re.findall(fence_pattern, code, re.DOTALL)
    if matches:
        # Choose the longest matching code block
        code = max(matches, key=len).strip()
    else:
        # Remove any starting or ending fence marks if present
        if code.startswith("```"):
            lines = code.split("\n")
            if lines:
                lines = lines[1:]
            code = "\n".join(lines)
        if code.endswith("```"):
            code = code[:-3].strip()

    # For SVG, extract <svg ... </svg> if embedded in other text
    if format_type.lower() == "svg" or "<svg" in code.lower():
        svg_match = re.search(r"(<svg[\s\S]*?<\/svg>)", code, re.IGNORECASE)
        if svg_match:
            code = svg_match.group(1).strip()

    return code.strip()


def render_matplotlib(code: str, output_path: Path, timeout: int = 30) -> Tuple[bool, str]:
    """Execute Python Matplotlib code in a subprocess and save as PNG."""
    cleaned = clean_code_snippet(code, "matplotlib")
    if not cleaned:
        return False, "Empty matplotlib code"

    # Pre-execution syntax check: catch invalid Python before spending a
    # subprocess + timeout window on code that can never run (e.g. a stray
    # comma leaving a function call argument empty). Cheap (no subprocess)
    # and lets the caller feed a precise, same-pass error back to the model
    # instead of only discovering it after render + validate have both run.
    try:
        compile(cleaned, "<diagram_code>", "exec")
    except SyntaxError as se:
        return False, f"Syntax error at line {se.lineno}: {se.msg} — {(se.text or '').strip()}"

    # Security check: disallow system modules
    for mod in DISALLOWED_MATPLOTLIB_MODULES:
        pattern = rf"\b(?:import\s+{mod}|from\s+{mod}\s+import|__import__\s*\(\s*['\"]{mod}['\"]\))\b"
        if re.search(pattern, cleaned):
            return False, f"Security violation: unauthorized module import '{mod}'"

    # Ensure output parent dir exists
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out_posix = output_path.as_posix()

    # Wrap code to ensure proper backend and save
    wrapper = f"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

# Apply clean visual styling
plt.rcParams['figure.dpi'] = 200
plt.rcParams['savefig.dpi'] = 200
plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Arial', 'Helvetica']

# --- User Code Start ---
{cleaned}
# --- User Code End ---

# Ensure figure is saved
if plt.get_fignums():
    fig = plt.gcf()
    fig.tight_layout()
    fig.savefig(r'{out_posix}', bbox_inches='tight', dpi=200, facecolor='white', edgecolor='none')
    plt.close('all')
else:
    raise RuntimeError("No matplotlib figures were created by the code")
"""

    with tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w", encoding="utf-8") as tf:
        tf.write(wrapper)
        temp_script = tf.name

    try:
        proc = subprocess.run(
            [sys.executable, temp_script],
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if proc.returncode != 0:
            err_msg = proc.stderr.strip() or proc.stdout.strip()
            log.warning("Matplotlib render failed with return code %d: %s", proc.returncode, err_msg[:300])
            return False, f"Execution failed: {err_msg[:300]}"

        if output_path.exists() and output_path.stat().st_size > 0:
            log.info("Matplotlib rendered successfully: %s (%d bytes)", output_path.name, output_path.stat().st_size)
            return True, "Success"
        return False, "Render finished without generating output file"
    except subprocess.TimeoutExpired:
        return False, f"Matplotlib rendering timed out after {timeout}s"
    except Exception as e:
        return False, f"Matplotlib execution error: {str(e)}"
    finally:
        try:
            os.unlink(temp_script)
        except Exception:
            pass


from concurrent.futures import ThreadPoolExecutor

def _run_in_isolated_thread(func, timeout: int = 30):
    """Executes a function in a fresh worker thread without an asyncio event loop."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func)
        return future.result(timeout=timeout)


def render_mermaid(code: str, output_path: Path, timeout: int = 30) -> Tuple[bool, str]:
    """Render Mermaid code to PNG using Playwright headless Chromium."""
    cleaned = clean_code_snippet(code, "mermaid")
    if not cleaned:
        return False, "Empty mermaid code"

    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _pw_render():
        from playwright.sync_api import sync_playwright

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <style>
        body {{
            margin: 20px;
            background: white;
            display: inline-block;
            font-family: sans-serif;
        }}
        .mermaid {{
            background: white;
        }}
    </style>
</head>
<body>
    <div class="mermaid" id="container">
{cleaned}
    </div>
    <script>
        mermaid.initialize({{
            startOnLoad: true,
            theme: 'default',
            securityLevel: 'loose'
        }});
    </script>
</body>
</html>"""

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(device_scale_factor=2)
            page.set_content(html)
            page.wait_for_selector("#container svg", timeout=int(timeout * 1000))
            svg_elem = page.locator("#container svg")
            svg_elem.screenshot(path=str(output_path))
            browser.close()

    # First try Playwright in an isolated thread
    try:
        _run_in_isolated_thread(_pw_render, timeout=timeout)
        if output_path.exists() and output_path.stat().st_size > 0:
            log.info("Mermaid rendered via Playwright: %s (%d bytes)", output_path.name, output_path.stat().st_size)
            return True, "Success"
    except Exception as pw_err:
        log.warning("Playwright Mermaid render failed (%s), attempting mermaid.ink API fallback...", pw_err)

    # Fallback to mermaid.ink API
    try:
        import urllib.request
        cleaned_b64 = base64.b64encode(cleaned.encode("utf-8")).decode("ascii")
        url = f"https://mermaid.ink/img/{cleaned_b64}?bgColor=white"
        req = urllib.request.Request(url, headers={"User-Agent": "EducationWebsite/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if data and len(data) > 200:
                output_path.write_bytes(data)
                log.info("Mermaid rendered via mermaid.ink fallback: %s (%d bytes)", output_path.name, len(data))
                return True, "Success (via mermaid.ink)"
    except Exception as ink_err:
        log.warning("Mermaid.ink fallback also failed: %s", ink_err)

    return False, "Failed to render Mermaid diagram"


def render_svg(code: str, output_path: Path, timeout: int = 30) -> Tuple[bool, str]:
    """Render SVG XML code to PNG using Playwright headless Chromium."""
    cleaned = clean_code_snippet(code, "svg")
    if not cleaned:
        return False, "Empty SVG code"

    if not ("<svg" in cleaned.lower() and "</svg>" in cleaned.lower()):
        return False, "Invalid SVG code (missing <svg> or </svg> tags)"

    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _pw_svg_render():
        from playwright.sync_api import sync_playwright

        html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{
            margin: 20px;
            background: white;
            display: inline-block;
        }}
    </style>
</head>
<body>
    <div id="svg-container">
        {cleaned}
    </div>
</body>
</html>"""

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(device_scale_factor=2)
            page.set_content(html)
            page.wait_for_selector("#svg-container svg", timeout=int(timeout * 1000))
            svg_elem = page.locator("#svg-container svg")
            svg_elem.screenshot(path=str(output_path))
            browser.close()

    try:
        _run_in_isolated_thread(_pw_svg_render, timeout=timeout)
        if output_path.exists() and output_path.stat().st_size > 0:
            log.info("SVG rendered via Playwright: %s (%d bytes)", output_path.name, output_path.stat().st_size)
            return True, "Success"
        return False, "SVG screenshot file was empty"
    except Exception as e:
        log.warning("SVG Playwright render error: %s", e)
        return False, f"SVG render error: {str(e)}"


def render_diagram(format_type: str, code: str, output_path: Path, timeout: int = 30) -> Tuple[bool, str]:
    """
    Unified entry point for rendering code into a PNG diagram.
    format_type: 'matplotlib' | 'mermaid' | 'svg'
    """
    fmt = (format_type or "").strip().lower()
    
    # Auto-detect if format is unspecified or misclassified
    if not fmt:
        if "<svg" in code.lower():
            fmt = "svg"
        elif any(code.strip().startswith(kw) for kw in ["graph", "flowchart", "sequenceDiagram", "classDiagram", "stateDiagram", "gitGraph", "pie"]):
            fmt = "mermaid"
        else:
            fmt = "matplotlib"

    if fmt in ("matplotlib", "python", "py", "plt"):
        return render_matplotlib(code, output_path, timeout=timeout)
    elif fmt in ("mermaid", "mmd"):
        return render_mermaid(code, output_path, timeout=timeout)
    elif fmt in ("svg", "xml"):
        return render_svg(code, output_path, timeout=timeout)
    else:
        # If unknown, try matplotlib first, then svg if it has tags
        if "<svg" in code.lower():
            return render_svg(code, output_path, timeout=timeout)
        return render_matplotlib(code, output_path, timeout=timeout)