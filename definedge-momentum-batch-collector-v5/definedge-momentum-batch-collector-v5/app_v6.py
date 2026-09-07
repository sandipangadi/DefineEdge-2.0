import os
import secrets
import threading
import time

import app as legacy
from drive_bridge import latest_zip_from_folder, publish_package
from flask import make_response, render_template, request

app = legacy.app

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


def home_v6():
    legacy.cleanup_jobs()
    return render_template(
        "index_v6.html",
        otp_sent=False,
        drive_missing=_drive_config_status(),
    )


def job_worker_v6(job_id, session_key, input_bytes, input_filename, before_minutes, after_minutes, include_chain, source_meta=None):
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
            raise RuntimeError("V5 analysis completed but output_path was not produced.")

        legacy.set_job(
            job_id,
            status="running",
            progress=97,
            message="Analysis complete. Publishing evidence package to Trading Brain Drive...",
        )

        uploaded, manifest = publish_package(
            output_path,
            EVIDENCE_FOLDER_ID,
            STATUS_FOLDER_ID or None,
            source_meta=source_meta or {},
            job_summary=job.get("summary", {}),
        )

        summary = dict(job.get("summary", {}))
        summary.update({
            "drive_file_id": uploaded.get("id", ""),
            "drive_file_name": uploaded.get("name", ""),
            "drive_file_url": uploaded.get("webViewLink", ""),
            "pipeline_version": "6.0",
            "ready_for_trading_brain": True,
        })

        legacy.set_job(
            job_id,
            status="done",
            progress=100,
            message="Completed and published to Trading Brain Drive. No manual download/upload is required.",
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
            raise RuntimeError("V6 Drive automation is not configured: " + ", ".join(missing))

        otp = request.form.get("otp", "").strip()
        state_id = request.cookies.get("de_state", "")
        if not otp:
            raise RuntimeError("Enter the Definedge OTP.")

        source_meta, input_bytes = latest_zip_from_folder(INBOX_FOLDER_ID)
        if not input_bytes:
            raise RuntimeError("Latest Drive AlgoStra ZIP is empty.")

        test_zf, _ = legacy.safe_zip_input(input_bytes)
        test_zf.close()

        before_minutes = max(0, min(60, int(request.form.get("before_min", "3"))))
        after_minutes = max(0, min(180, int(request.form.get("after_min", "15"))))
        include_chain = request.form.get("include_chain") == "yes"

        session_key = legacy.authenticate(state_id, otp)
        job_id = secrets.token_urlsafe(18)

        with legacy.JOB_LOCK:
            legacy.JOBS[job_id] = {
                "created": time.time(),
                "status": "queued",
                "progress": 1,
                "message": "Starting automated Drive ingestion...",
                "output_path": "",
                "output_filename": "",
                "summary": {},
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

        response = make_response(render_template("job.html", job_id=job_id))
        response.delete_cookie("de_state")
        return response

    except Exception as exc:
        return render_template(
            "index_v6.html",
            otp_sent=True,
            error=str(exc),
            drive_missing=_drive_config_status(),
        ), 500


# Replace only the views; all proven V5 market-data, parsing and analysis logic remains untouched.
app.view_functions["home"] = home_v6
app.view_functions["collect_route"] = collect_route_v6

# V5 send-otp, status, download and health routes remain active.
# The download route is retained as an emergency fallback even though V6 publishes to Drive.

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))
