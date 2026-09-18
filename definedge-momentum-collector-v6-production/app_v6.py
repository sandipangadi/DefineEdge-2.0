import importlib.util
import io
import os
import secrets
import sys
import threading
import tempfile
import time
import zipfile
from hashlib import sha256
from pathlib import Path

from flask import jsonify, make_response, render_template, request, send_file
from jinja2 import FileSystemLoader

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
LEGACY_APP_PATH = REPO_ROOT / "definedge-momentum-batch-collector-v5" / "definedge-momentum-batch-collector-v5" / "app.py"
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))
from drive_bridge import drive_auth_mode, oauth_write_configured, publish_package, upload_market_data_package
from input_zip_validation import validate_and_sanitize_algostra_zip
from shoonya_probe import config_status as shoonya_config_status, run_probe as shoonya_run_probe
from session_enrichment import enrich_package as enrich_session_package
if not LEGACY_APP_PATH.exists():
    raise RuntimeError(f"Frozen V5 engine not found: {LEGACY_APP_PATH}")
spec = importlib.util.spec_from_file_location("definedge_v5_engine", LEGACY_APP_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load the frozen V5 reconstruction engine.")
legacy = importlib.util.module_from_spec(spec)
sys.modules["definedge_v5_engine"] = legacy
spec.loader.exec_module(legacy)
app = legacy.app
app.template_folder = str(BASE_DIR / "templates")
app.jinja_loader = FileSystemLoader(str(BASE_DIR / "templates"))
EVIDENCE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_EVIDENCE_FOLDER_ID", "").strip()
STATUS_FOLDER_ID = os.getenv("GOOGLE_DRIVE_STATUS_FOLDER_ID", "").strip()

def _drive_config_status():
    missing = []
    if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip() and not oauth_write_configured():
        missing.append("GOOGLE_SERVICE_ACCOUNT_JSON or Google OAuth credentials")
    if not EVIDENCE_FOLDER_ID:
        missing.append("GOOGLE_DRIVE_EVIDENCE_FOLDER_ID")
    return missing

def _template_kwargs(**kwargs):
    base = {"drive_missing": _drive_config_status(), "drive_auth_mode": drive_auth_mode(), "drive_write_ready": oauth_write_configured()}
    base.update(kwargs)
    return base

def _render_index(otp_sent=False, message=None, error=None, status_code=200):
    return render_template("index_v6.html", **_template_kwargs(otp_sent=otp_sent, message=message, error=error)), status_code

def home_v6():
    legacy.cleanup_jobs()
    return render_template("index_v6.html", **_template_kwargs(otp_sent=False))

def send_otp_route_v6():
    try:
        state_id = legacy.send_definedge_otp(request.form.get("collector_password", ""))
        response = make_response(render_template("index_v6.html", **_template_kwargs(otp_sent=True, message="OTP sent. Password accepted for this session.")))
        response.set_cookie("de_state", state_id, max_age=legacy.OTP_TTL_SECONDS, httponly=True, secure=True, samesite="Lax")
        return response
    except Exception as exc:
        return _render_index(False, error=str(exc), status_code=400)

def resend_otp_route_v6():
    try:
        legacy.cleanup_states()
        state_id = request.cookies.get("de_state", "")
        state = legacy.OTP_STATES.get(state_id)
        if not state or not state.get("password_verified"):
            raise RuntimeError("Collector session expired. Enter the collector password and send a new OTP.")
        response = legacy.HTTP.get(legacy.LOGIN_URL + legacy.API_TOKEN, headers={"api_secret": legacy.API_SECRET}, timeout=30)
        if not response.ok:
            raise RuntimeError(f"Definedge fresh OTP request failed ({response.status_code}): {response.text[:500]}")
        data = response.json()
        otp_token = data.get("otp_token")
        if not otp_token:
            raise RuntimeError("Definedge returned success but no otp_token for the fresh OTP.")
        state["otp_token"] = otp_token
        state["created"] = time.time()
        state["password_verified"] = True
        page = make_response(render_template("index_v6.html", **_template_kwargs(otp_sent=True, message="Fresh Definedge OTP sent. Use the newest OTP only.")))
        page.set_cookie("de_state", state_id, max_age=legacy.OTP_TTL_SECONDS, httponly=True, secure=True, samesite="Lax")
        return page
    except Exception as exc:
        return _render_index(False, error=str(exc), status_code=400)

def authenticate_v6(state_id, otp):
    legacy.cleanup_states()
    state = legacy.OTP_STATES.get(state_id)
    if not state or not state.get("password_verified"):
        raise RuntimeError("OTP session expired. Use Send Fresh OTP or start again with the collector password.")
    otp_code = str(otp or "").strip()
    otp_token = str(state.get("otp_token") or "").strip()
    if not otp_code:
        raise RuntimeError("Enter the Definedge OTP.")
    if not otp_token:
        raise RuntimeError("OTP token is missing. Click Send Fresh OTP.")
    ac = sha256(f"{otp_token}{otp_code}{legacy.API_SECRET}".encode("utf-8")).hexdigest()
    response = legacy.HTTP.post(legacy.TOKEN_URL, json={"otp_token": otp_token, "otp": otp_code, "ac": ac}, headers={"Content-Type": "application/json", "Accept": "application/json"}, timeout=30)
    if not response.ok:
        raise RuntimeError(f"Definedge OTP authentication failed ({response.status_code}): {response.text[:500]}. Use Send Fresh OTP and enter the newest OTP.")
    data = response.json()
    session_key = data.get("api_session_key") or legacy.extract_session_key(data)
    if not session_key:
        raise RuntimeError("Definedge authentication succeeded but no usable api_session_key was returned.")
    legacy.OTP_STATES.pop(state_id, None)
    return session_key

def job_worker_v6(job_id, session_key, input_bytes, input_filename, before_minutes, after_minutes, include_chain, source_meta=None):
    try:
        legacy.build_v5_package(job_id, session_key, input_bytes, input_filename, before_minutes, after_minutes, include_chain)
        with legacy.JOB_LOCK:
            pre_enrichment_job = dict(legacy.JOBS.get(job_id, {}))
        pre_enrichment_output = pre_enrichment_job.get("output_path")
        enrichment_summary = {}
        if pre_enrichment_output:
            legacy.set_job(
                job_id,
                status="running",
                progress=97,
                message="Core evidence complete. Adding 09:15 session history and OI attribution...",
            )
            try:
                enrichment_summary = enrich_session_package(
                    pre_enrichment_output,
                    session_key,
                    input_bytes,
                    legacy,
                    job_id,
                )
            except Exception as enrichment_exc:
                enrichment_summary = {
                    "session_enrichment_version": "6.4",
                    "session_enrichment_error": str(enrichment_exc),
                    "session_enrichment_complete": False,
                }
        with legacy.JOB_LOCK:
            job = dict(legacy.JOBS.get(job_id, {}))
        output_path = job.get("output_path")
        if not output_path:
            raise RuntimeError("Reconstruction completed but no evidence ZIP was produced.")
        source_path = Path(output_path)
        v6_name = source_path.name.replace("Momentum_Full_Analysis_V5", "Momentum_Full_Evidence_V6")
        target_path = source_path.with_name(v6_name)
        if target_path != source_path:
            source_path.replace(target_path)
            output_path = str(target_path)
            legacy.set_job(job_id, output_path=output_path, output_filename=target_path.name)
        legacy.set_job(job_id, status="running", progress=97, message="Evidence reconstructed. Publishing package to Trading Brain Drive...")
        try:
            uploaded, manifest = publish_package(output_path, EVIDENCE_FOLDER_ID, STATUS_FOLDER_ID or None, source_meta=source_meta or {}, job_summary=job.get("summary", {}))
        except Exception as publish_exc:
            with legacy.JOB_LOCK:
                latest_job = dict(legacy.JOBS.get(job_id, {}))
            summary = dict(latest_job.get("summary", {}))
            summary.update({"pipeline_version": "6.4", "reconstruction_engine": "V5 frozen + V6.4 session enrichment", **enrichment_summary, "ready_for_trading_brain": False, "local_evidence_ready": True, "drive_auth_mode": drive_auth_mode(), "drive_publish_error": str(publish_exc), "source_input": input_filename})
            legacy.set_job(job_id, status="error", progress=100, message=f"Evidence package is complete, but Drive publication failed: {publish_exc} Download Evidence ZIP below; Definedge collection does not need to be rerun.", summary=summary, output_path=output_path, output_filename=Path(output_path).name)
            return
        with legacy.JOB_LOCK:
            latest_job = dict(legacy.JOBS.get(job_id, {}))
        summary = dict(latest_job.get("summary", {}))
        summary.update({"drive_file_id": uploaded.get("id", ""), "drive_file_name": uploaded.get("name", ""), "drive_file_url": uploaded.get("webViewLink", ""), "pipeline_version": "6.4", "reconstruction_engine": "V5 frozen + V6.4 session enrichment", **enrichment_summary, "ready_for_trading_brain": True, "local_evidence_ready": True, "drive_auth_mode": drive_auth_mode(), "manifest_status": manifest.get("status", ""), "source_input": input_filename})
        legacy.set_job(job_id, status="done", progress=100, message="Completed and published to Trading Brain Drive.", summary=summary)
    except Exception as exc:
        legacy.set_job(job_id, status="error", progress=100, message=str(exc))

def collect_route_v6():
    try:
        missing = _drive_config_status()
        if missing:
            raise RuntimeError("V6 Drive publishing is not configured: " + ", ".join(missing))
        otp = request.form.get("otp", "").strip()
        state_id = request.cookies.get("de_state", "")
        if not otp:
            raise RuntimeError("Enter the Definedge OTP.")
        uploaded = request.files.get("algostra_zip")
        if not uploaded or not uploaded.filename:
            raise RuntimeError("Select today's AlgoStra ZIP. V6.4 will not silently use an older Drive ZIP.")
        if not uploaded.filename.lower().endswith(".zip"):
            raise RuntimeError("AlgoStra input must be one ZIP file.")
        input_bytes = uploaded.read()
        if not input_bytes:
            raise RuntimeError("Uploaded AlgoStra ZIP is empty.")

        test_zf, _ = legacy.safe_zip_input(input_bytes)
        try:
            input_bytes, input_audit = validate_and_sanitize_algostra_zip(test_zf)
        finally:
            test_zf.close()

        before_minutes = max(0, min(60, int(request.form.get("before_min", "3"))))
        after_minutes = max(0, min(180, int(request.form.get("after_min", "15"))))
        include_chain = request.form.get("include_chain") == "yes"
        session_key = authenticate_v6(state_id, otp)
        job_id = secrets.token_urlsafe(18)
        source_meta = {
            "name": uploaded.filename,
            "source": "manual_upload_v6.4_dynamic",
            "csv_count_original": input_audit["csv_count_original"],
            "csv_count_sanitized": input_audit["csv_count_sanitized"],
            "input_audit": input_audit,
        }
        with legacy.JOB_LOCK:
            legacy.JOBS[job_id] = {
                "created": time.time(),
                "status": "queued",
                "progress": 1,
                "message": (
                    f"Starting V6.3 from uploaded ZIP: {uploaded.filename} "
                    f"({input_audit['csv_count_sanitized']} unique AlgoStra CSVs verified; "
                    f"{input_audit['duplicate_csv_count_removed']} exact duplicate(s) removed)..."
                ),
                "output_path": "",
                "output_filename": "",
                "summary": {
                    "pipeline_version": "6.4",
                    "ready_for_trading_brain": False,
                    "drive_auth_mode": drive_auth_mode(),
                    "source_input": uploaded.filename,
                    "source_csv_count_original": input_audit["csv_count_original"],
                    "source_csv_count_sanitized": input_audit["csv_count_sanitized"],
                    "source_input_audit": input_audit,
                },
            }
        threading.Thread(target=job_worker_v6, kwargs={"job_id": job_id, "session_key": session_key, "input_bytes": input_bytes, "input_filename": uploaded.filename, "before_minutes": before_minutes, "after_minutes": after_minutes, "include_chain": include_chain, "source_meta": source_meta}, daemon=True).start()
        response = make_response(render_template("job_v6.html", job_id=job_id))
        response.delete_cookie("de_state")
        return response
    except Exception as exc:
        return _render_index(True, error=str(exc), status_code=400)

def status_route_v6(job_id):
    legacy.cleanup_jobs()
    with legacy.JOB_LOCK:
        job = legacy.JOBS.get(job_id)
        if not job:
            return jsonify({"status": "missing", "progress": 100, "message": "This job expired or the service restarted."}), 404
        snapshot = dict(job)
    output_path = snapshot.get("output_path")
    return jsonify({"status": snapshot.get("status", "missing"), "progress": snapshot.get("progress", 0), "message": snapshot.get("message", ""), "summary": dict(snapshot.get("summary", {})), "download_ready": bool(output_path and Path(output_path).exists())})

def download_route_v6(job_id):
    legacy.cleanup_jobs()
    with legacy.JOB_LOCK:
        job = legacy.JOBS.get(job_id)
        if not job:
            return "Job expired.", 404
        output_path = job.get("output_path")
        output_filename = job.get("output_filename") or "Definedge_Evidence_V6.zip"
    if not output_path:
        return "Output file unavailable.", 404
    path = Path(output_path)
    if not path.exists():
        return "Output file expired.", 404
    return send_file(path, mimetype="application/zip", as_attachment=True, download_name=output_filename, max_age=0)

def health_v6():
    missing = _drive_config_status()
    return {"status": "ok" if not missing else "configuration_incomplete", "version": "6.4", "reconstruction_engine": "V5 frozen + V6.4 session enrichment", "input_mode": "explicit_manual_zip_dynamic", "drive_configured": not bool(missing), "drive_auth_mode": drive_auth_mode(), "drive_oauth_write_ready": oauth_write_configured(), "missing_configuration": missing}

app.view_functions["home"] = home_v6
app.view_functions["send_otp_route"] = send_otp_route_v6
app.view_functions["collect_route"] = collect_route_v6
app.view_functions["status_route"] = status_route_v6
app.view_functions["download_route"] = download_route_v6
app.view_functions["health"] = health_v6
if "resend_otp_route" in app.view_functions:
    app.view_functions["resend_otp_route"] = resend_otp_route_v6
else:
    app.add_url_rule("/resend-otp", endpoint="resend_otp_route", view_func=resend_otp_route_v6, methods=["POST"])


def _shoonya_probe_page(message=None, error=None, result=None, status_code=200):
    config = shoonya_config_status()
    response = make_response(
        render_template(
            "shoonya_probe.html",
            configured=bool(config.get("configured")),
            message=message,
            error=error,
            result=result,
        )
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response, status_code


@app.get("/shoonya")
def shoonya_page_route():
    return _shoonya_probe_page()


@app.post("/shoonya/probe-page")
def shoonya_probe_page_route():
    collector_password = request.form.get("collector_password", "")
    expected_password = str(getattr(legacy, "COLLECTOR_PASSWORD", "") or "")
    if not expected_password or not secrets.compare_digest(
        collector_password,
        expected_password,
    ):
        return _shoonya_probe_page(
            error="Collector authentication failed.",
            status_code=401,
        )

    try:
        result = shoonya_run_probe(
            request.form.get("factor2", ""),
            lookback_days=request.form.get("lookback_days", "7"),
        )
        return _shoonya_probe_page(
            message="Shoonya read-only probe completed successfully.",
            result=result,
        )
    except Exception as exc:
        return _shoonya_probe_page(
            error=str(exc),
            status_code=400,
        )


@app.get("/shoonya/status")
def shoonya_status_route():
    response = jsonify(shoonya_config_status())
    response.headers["Cache-Control"] = "no-store"
    return response


@app.post("/shoonya/probe")
def shoonya_probe_route():
    collector_password = request.form.get("collector_password", "")
    expected_password = str(getattr(legacy, "COLLECTOR_PASSWORD", "") or "")
    if not expected_password or not secrets.compare_digest(
        collector_password,
        expected_password,
    ):
        return jsonify({
            "status": "error",
            "message": "Collector authentication failed.",
        }), 401

    try:
        result = shoonya_run_probe(
            request.form.get("factor2", ""),
            lookback_days=request.form.get("lookback_days", "7"),
        )
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as exc:
        response = jsonify({
            "status": "error",
            "message": str(exc),
            "read_only_probe": True,
            "orders_enabled": False,
        })
        response.headers["Cache-Control"] = "no-store"
        return response, 400


@app.post("/shoonya/relay-ingest")
def shoonya_relay_ingest_route():
    collector_password = request.form.get("collector_password", "")
    expected_password = str(getattr(legacy, "COLLECTOR_PASSWORD", "") or "")
    if not expected_password or not secrets.compare_digest(
        collector_password,
        expected_password,
    ):
        return jsonify({
            "status": "error",
            "message": "Collector authentication failed.",
        }), 401

    uploaded = request.files.get("package")
    if not uploaded or not uploaded.filename:
        return jsonify({
            "status": "error",
            "message": "A Shoonya relay ZIP package is required.",
        }), 400
    if not uploaded.filename.lower().endswith(".zip"):
        return jsonify({
            "status": "error",
            "message": "The relay package must be a ZIP file.",
        }), 400

    package_bytes = uploaded.read()
    if not package_bytes:
        return jsonify({
            "status": "error",
            "message": "The relay package is empty.",
        }), 400
    if len(package_bytes) > 10 * 1024 * 1024:
        return jsonify({
            "status": "error",
            "message": "The relay package exceeds the 10 MB safety limit.",
        }), 400

    try:
        with zipfile.ZipFile(io.BytesIO(package_bytes)) as archive:
            total_size = 0
            for member in archive.infolist():
                if member.is_dir():
                    continue
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise RuntimeError("Unsafe path found in relay ZIP.")
                total_size += member.file_size
                if member.file_size > 10 * 1024 * 1024 or total_size > 20 * 1024 * 1024:
                    raise RuntimeError("Relay ZIP contains an oversized file.")
    except zipfile.BadZipFile as exc:
        raise RuntimeError("Relay package is not a valid ZIP file.") from exc
    except Exception as exc:
        response = jsonify({
            "status": "error",
            "message": str(exc),
            "read_only_probe": True,
            "orders_enabled": False,
        })
        response.headers["Cache-Control"] = "no-store"
        return response, 400

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="shoonya_relay_", suffix=".zip", delete=False) as temp:
            temp.write(package_bytes)
            temp_path = Path(temp.name)
        uploaded_drive, manifest = upload_market_data_package(
            temp_path,
            EVIDENCE_FOLDER_ID,
            STATUS_FOLDER_ID or None,
            source_meta={
                "source": "local_shoonya_relay",
                "uploaded_filename": uploaded.filename,
            },
            job_summary={
                "read_only_probe": True,
                "orders_enabled": False,
            },
        )
        response = jsonify({
            "status": "ok",
            "read_only_probe": True,
            "orders_enabled": False,
            "drive_file_id": uploaded_drive.get("id", ""),
            "drive_file_name": uploaded_drive.get("name", ""),
            "drive_file_url": uploaded_drive.get("webViewLink", ""),
            "manifest_status": manifest.get("status", ""),
        })
        response.headers["Cache-Control"] = "no-store"
        return response
    except Exception as exc:
        response = jsonify({
            "status": "error",
            "message": str(exc),
            "read_only_probe": True,
            "orders_enabled": False,
        })
        response.headers["Cache-Control"] = "no-store"
        return response, 400
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
