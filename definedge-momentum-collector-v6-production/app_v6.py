import importlib.util
import os
import secrets
import sys
import threading
import time
from hashlib import sha256
from pathlib import Path

from flask import jsonify, make_response, render_template, request
from jinja2 import FileSystemLoader

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
LEGACY_APP_PATH = (
    REPO_ROOT
    / "definedge-momentum-batch-collector-v5"
    / "definedge-momentum-batch-collector-v5"
    / "app.py"
)

# Production-only modules resolve from this clean V6 folder first.
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from drive_bridge import latest_zip_from_folder, publish_package

# Load V5 only as the frozen reconstruction/data engine.
if not LEGACY_APP_PATH.exists():
    raise RuntimeError(f"Frozen V5 engine not found: {LEGACY_APP_PATH}")

spec = importlib.util.spec_from_file_location(
    "definedge_v5_engine",
    LEGACY_APP_PATH,
)
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load the frozen V5 reconstruction engine.")

legacy = importlib.util.module_from_spec(spec)
sys.modules["definedge_v5_engine"] = legacy
spec.loader.exec_module(legacy)

app = legacy.app
app.template_folder = str(BASE_DIR / "templates")
app.jinja_loader = FileSystemLoader(str(BASE_DIR / "templates"))

INBOX_FOLDER_ID = os.getenv("GOOGLE_DRIVE_INBOX_FOLDER_ID", "").strip()
EVIDENCE_FOLDER_ID = os.getenv("GOOGLE_DRIVE_EVIDENCE_FOLDER_ID", "").strip()
STATUS_FOLDER_ID = os.getenv("GOOGLE_DRIVE_STATUS_FOLDER_ID", "").strip()


def _drive_config_status():
    missing = []
    if not os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip():
        missing.append("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not INBOX_FOLDER_ID:
        missing.append("GOOGLE_DRIVE_INBOX_FOLDER_ID")
    if not EVIDENCE_FOLDER_ID:
        missing.append("GOOGLE_DRIVE_EVIDENCE_FOLDER_ID")
    return missing


def _render_index(otp_sent=False, message=None, error=None, status_code=200):
    return (
        render_template(
            "index_v6.html",
            otp_sent=otp_sent,
            message=message,
            error=error,
            drive_missing=_drive_config_status(),
        ),
        status_code,
    )


def home_v6():
    legacy.cleanup_jobs()
    return render_template(
        "index_v6.html",
        otp_sent=False,
        drive_missing=_drive_config_status(),
    )


def send_otp_route_v6():
    try:
        password = request.form.get("collector_password", "")
        state_id = legacy.send_definedge_otp(password)

        response = make_response(
            render_template(
                "index_v6.html",
                otp_sent=True,
                message="OTP sent. Password accepted for this session.",
                drive_missing=_drive_config_status(),
            )
        )
        response.set_cookie(
            "de_state",
            state_id,
            max_age=legacy.OTP_TTL_SECONDS,
            httponly=True,
            secure=True,
            samesite="Lax",
        )
        return response

    except Exception as exc:
        return _render_index(
            otp_sent=False,
            error=str(exc),
            status_code=400,
        )


def resend_otp_route_v6():
    """Send a new OTP without asking for the collector password again."""
    try:
        legacy.cleanup_states()
        state_id = request.cookies.get("de_state", "")
        state = legacy.OTP_STATES.get(state_id)

        if not state or not state.get("password_verified"):
            raise RuntimeError(
                "Collector session expired. Enter the collector password and send a new OTP."
            )

        response = legacy.HTTP.get(
            legacy.LOGIN_URL + legacy.API_TOKEN,
            headers={"api_secret": legacy.API_SECRET},
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(
                f"Definedge fresh OTP request failed ({response.status_code}): "
                f"{response.text[:500]}"
            )

        data = response.json()
        otp_token = data.get("otp_token")
        if not otp_token:
            raise RuntimeError(
                "Definedge returned success but no otp_token for the fresh OTP."
            )

        state["otp_token"] = otp_token
        state["created"] = time.time()
        state["password_verified"] = True

        page = make_response(
            render_template(
                "index_v6.html",
                otp_sent=True,
                message="Fresh Definedge OTP sent. Use the newest OTP only.",
                drive_missing=_drive_config_status(),
            )
        )
        page.set_cookie(
            "de_state",
            state_id,
            max_age=legacy.OTP_TTL_SECONDS,
            httponly=True,
            secure=True,
            samesite="Lax",
        )
        return page

    except Exception as exc:
        return _render_index(
            otp_sent=False,
            error=str(exc),
            status_code=400,
        )


def authenticate_v6(state_id, otp):
    """Definedge login step 2 matching the current official pyintegrate client.

    Official flow:
      ac = sha256(otp_token + otp + api_secret)
      POST JSON {"otp_token": ..., "otp": ..., "ac": ...}
    """
    legacy.cleanup_states()
    state = legacy.OTP_STATES.get(state_id)
    if not state or not state.get("password_verified"):
        raise RuntimeError(
            "OTP session expired. Use Send Fresh OTP or start again with the collector password."
        )

    otp_code = str(otp or "").strip()
    if not otp_code:
        raise RuntimeError("Enter the Definedge OTP.")

    otp_token = str(state.get("otp_token") or "").strip()
    if not otp_token:
        raise RuntimeError("OTP token is missing. Click Send Fresh OTP.")

    ac = sha256(
        f"{otp_token}{otp_code}{legacy.API_SECRET}".encode("utf-8")
    ).hexdigest()

    response = legacy.HTTP.post(
        legacy.TOKEN_URL,
        json={
            "otp_token": otp_token,
            "otp": otp_code,
            "ac": ac,
        },
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=30,
    )

    if not response.ok:
        detail = response.text[:500]
        try:
            body = response.json()
            if isinstance(body, dict):
                detail = (
                    body.get("message")
                    or body.get("error_description")
                    or body.get("error")
                    or detail
                )
        except Exception:
            pass
        raise RuntimeError(
            f"Definedge OTP authentication failed ({response.status_code}): {detail}. "
            "Use Send Fresh OTP and enter the newest OTP."
        )

    try:
        data = response.json()
    except Exception as exc:
        raise RuntimeError(
            "Definedge authentication returned a non-JSON response."
        ) from exc

    session_key = data.get("api_session_key") or legacy.extract_session_key(data)
    if not session_key:
        raise RuntimeError(
            "Definedge authentication succeeded but no usable api_session_key was returned."
        )

    legacy.OTP_STATES.pop(state_id, None)
    return session_key


def job_worker_v6(
    job_id,
    session_key,
    input_bytes,
    input_filename,
    before_minutes,
    after_minutes,
    include_chain,
    source_meta=None,
):
    try:
        legacy.build_v5_package(
            job_id,
            session_key,
            input_bytes,
            input_filename,
            before_minutes,
            after_minutes,
            include_chain,
        )

        with legacy.JOB_LOCK:
            job = dict(legacy.JOBS.get(job_id, {}))

        output_path = job.get("output_path")
        if not output_path:
            raise RuntimeError(
                "Reconstruction completed but no evidence ZIP was produced."
            )

        source_path = Path(output_path)
        v6_name = source_path.name.replace(
            "Momentum_Full_Analysis_V5",
            "Momentum_Full_Evidence_V6",
        )
        target_path = source_path.with_name(v6_name)
        if target_path != source_path:
            source_path.replace(target_path)
            output_path = str(target_path)
            legacy.set_job(
                job_id,
                output_path=output_path,
                output_filename=target_path.name,
            )

        legacy.set_job(
            job_id,
            status="running",
            progress=97,
            message="Evidence reconstructed. Publishing package to Trading Brain Drive...",
        )

        uploaded, manifest = publish_package(
            output_path,
            EVIDENCE_FOLDER_ID,
            STATUS_FOLDER_ID or None,
            source_meta=source_meta or {},
            job_summary=job.get("summary", {}),
        )

        with legacy.JOB_LOCK:
            latest_job = dict(legacy.JOBS.get(job_id, {}))

        summary = dict(latest_job.get("summary", {}))
        summary.update(
            {
                "drive_file_id": uploaded.get("id", ""),
                "drive_file_name": uploaded.get("name", ""),
                "drive_file_url": uploaded.get("webViewLink", ""),
                "pipeline_version": "6.1.2",
                "reconstruction_engine": "V5 frozen",
                "ready_for_trading_brain": True,
                "manifest_status": manifest.get("status", ""),
            }
        )

        legacy.set_job(
            job_id,
            status="done",
            progress=100,
            message="Completed and published to Trading Brain Drive.",
            summary=summary,
        )

    except Exception as exc:
        legacy.set_job(
            job_id,
            status="error",
            progress=100,
            message=str(exc),
        )


def collect_route_v6():
    try:
        missing = _drive_config_status()
        if missing:
            raise RuntimeError(
                "V6 Drive automation is not configured: " + ", ".join(missing)
            )

        otp = request.form.get("otp", "").strip()
        state_id = request.cookies.get("de_state", "")
        if not otp:
            raise RuntimeError("Enter the Definedge OTP.")

        source_meta, input_bytes = latest_zip_from_folder(INBOX_FOLDER_ID)
        if not input_bytes:
            raise RuntimeError("Latest Drive AlgoStra ZIP is empty.")

        test_zf, _ = legacy.safe_zip_input(input_bytes)
        test_zf.close()

        before_minutes = max(
            0,
            min(60, int(request.form.get("before_min", "3"))),
        )
        after_minutes = max(
            0,
            min(180, int(request.form.get("after_min", "15"))),
        )
        include_chain = request.form.get("include_chain") == "yes"

        session_key = authenticate_v6(state_id, otp)
        job_id = secrets.token_urlsafe(18)

        with legacy.JOB_LOCK:
            legacy.JOBS[job_id] = {
                "created": time.time(),
                "status": "queued",
                "progress": 1,
                "message": "Starting V6 Drive evidence pipeline...",
                "output_path": "",
                "output_filename": "",
                "summary": {
                    "pipeline_version": "6.1.2",
                    "ready_for_trading_brain": False,
                },
            }

        thread = threading.Thread(
            target=job_worker_v6,
            kwargs={
                "job_id": job_id,
                "session_key": session_key,
                "input_bytes": input_bytes,
                "input_filename": source_meta.get("name", "drive_inbox.zip"),
                "before_minutes": before_minutes,
                "after_minutes": after_minutes,
                "include_chain": include_chain,
                "source_meta": source_meta,
            },
            daemon=True,
        )
        thread.start()

        response = make_response(
            render_template("job_v6.html", job_id=job_id)
        )
        response.delete_cookie("de_state")
        return response

    except Exception as exc:
        return _render_index(
            otp_sent=True,
            error=str(exc),
            status_code=400,
        )


def status_route_v6(job_id):
    legacy.cleanup_jobs()

    with legacy.JOB_LOCK:
        job = legacy.JOBS.get(job_id)
        if not job:
            return (
                jsonify(
                    {
                        "status": "missing",
                        "progress": 100,
                        "message": "This job expired or the service restarted.",
                    }
                ),
                404,
            )
        snapshot = dict(job)

    summary = dict(snapshot.get("summary", {}))
    status = snapshot.get("status", "missing")
    progress = snapshot.get("progress", 0)
    message = snapshot.get("message", "")

    if status == "done" and not summary.get("ready_for_trading_brain"):
        status = "running"
        progress = min(96, progress or 96)
        message = "Evidence reconstructed. Preparing Drive publication..."

    return jsonify(
        {
            "status": status,
            "progress": progress,
            "message": message,
            "summary": summary,
            "download_ready": (
                status == "done" and bool(snapshot.get("output_path"))
            ),
        }
    )


def health_v6():
    missing = _drive_config_status()
    return {
        "status": "ok" if not missing else "configuration_incomplete",
        "version": "6.1.2",
        "reconstruction_engine": "V5 frozen",
        "drive_configured": not bool(missing),
        "missing_configuration": missing,
    }


# V6 owns every user-visible route. V5 is engine-only.
app.view_functions["home"] = home_v6
app.view_functions["send_otp_route"] = send_otp_route_v6
app.view_functions["collect_route"] = collect_route_v6
app.view_functions["status_route"] = status_route_v6
app.view_functions["health"] = health_v6

# V6-only retry route.
if "resend_otp_v6" not in app.view_functions:
    app.add_url_rule(
        "/resend-otp",
        endpoint="resend_otp_v6",
        view_func=resend_otp_route_v6,
        methods=["POST"],
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.getenv("PORT", "10000")),
    )
