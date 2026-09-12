#!/usr/bin/env python3
import asyncio
import json
import uuid
import zipfile
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_file

from backend.diagram_finder import find_diagrams

app = Flask(__name__)
UPLOAD_DIR = Path("input_pdfs")
OUTPUT_DIR = Path("output")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/profiles")
def profiles():
    try:
        from backend.notebooklm_client import list_profiles
        return jsonify(list_profiles())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/process", methods=["POST"])
def process():
    if "pdf" not in request.files:
        return jsonify({"error": "No PDF uploaded"}), 400
    pdf = request.files["pdf"]
    if not pdf.filename:
        return jsonify({"error": "No file selected"}), 400
    profile = request.form.get("profile", "Slave 9")
    job_id = uuid.uuid4().hex[:8]
    pdf_path = UPLOAD_DIR / f"{job_id}_{Path(pdf.filename).name}"
    pdf.save(str(pdf_path))
    out_dir = OUTPUT_DIR / f"{job_id}_diagrams"
    try:
        manifest = asyncio.run(find_diagrams(pdf_path, out_dir, profile=profile))
        # zip
        zip_path = OUTPUT_DIR / f"{job_id}_diagrams.zip"
        with zipfile.ZipFile(str(zip_path), "w") as z:
            for m in manifest:
                p = Path(m["path"])
                if p.exists():
                    z.write(str(p), arcname=p.name)
            # also add manifest
            man = out_dir.parent / "diagram_manifest.json"
            if man.exists():
                z.write(str(man), arcname="diagram_manifest.json")
        return jsonify({"success": True, "job_id": job_id, "count": len(manifest), "manifest": manifest, "download_url": f"/download/{job_id}"})
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route("/download/<job_id>")
def download(job_id):
    zp = OUTPUT_DIR / f"{job_id}_diagrams.zip"
    if not zp.exists():
        return "Not found", 404
    return send_file(str(zp), as_attachment=True)

if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5001, use_reloader=False)
