import logging
log = logging.getLogger("capture.session")
log.warning("capture_session is deprecated — diagram watch now lives in app.py as job-scoped queue")
def start_session(*a, **kw): return {"ok": False, "error": "deprecated: use /api/diagram_watch/start/<job_id>"}
def stop_session(*a, **kw): return {"ok": False, "error": "deprecated"}
def get_status(*a, **kw): return {"active": False}
def init_watcher(*a, **kw): return None
