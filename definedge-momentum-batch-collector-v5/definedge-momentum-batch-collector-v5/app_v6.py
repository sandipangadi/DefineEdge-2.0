"""Compatibility shim for the existing Render V6 start command.

Production V6 code now lives in:
    /definedge-momentum-collector-v6-production/app_v6.py

The old V5 directory remains only because its app.py is the frozen,
proven reconstruction engine and Render currently starts from this path.
"""

import importlib.util
import os
import sys
from pathlib import Path

from flask import Response

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_APP = (
    REPO_ROOT
    / "definedge-momentum-collector-v6-production"
    / "app_v6.py"
)

if not PRODUCTION_APP.exists():
    raise RuntimeError(f"V6 production application not found: {PRODUCTION_APP}")

spec = importlib.util.spec_from_file_location(
    "definedge_v6_production",
    PRODUCTION_APP,
)
if spec is None or spec.loader is None:
    raise RuntimeError("Could not load V6 production application.")

production = importlib.util.module_from_spec(spec)
sys.modules["definedge_v6_production"] = production
spec.loader.exec_module(production)

app = production.app

# V7 research wrapper: preserve the proven V6/V5 evidence engine, then reuse the
# same authenticated Definedge session for a six-month NIFTY50 F&O Top20 study.
# A research failure never destroys the completed daily evidence package.
from historical_top20_research import run_six_month_top20
from all_options_capture import wrap_v6_job_worker
from momentum_analysis_wrapper import wrap_momentum_analysis

# V6-only layers: record all stock/index option strategies and append a research
# correlation pack for P&F/indicator-vs-momentum analysis. Frozen V5 stays intact.
_ORIGINAL_JOB_WORKER_V6 = wrap_momentum_analysis(
    production,
    wrap_v6_job_worker(production, production.job_worker_v6),
)


def _job_snapshot(job_id):
    with production.legacy.JOB_LOCK:
        return dict(production.legacy.JOBS.get(job_id, {}))


def job_worker_v6_with_top20_research(
    job_id,
    session_key,
    input_bytes,
    input_filename,
    before_minutes,
    after_minutes,
    include_chain,
    source_meta=None,
):
    _ORIGINAL_JOB_WORKER_V6(
        job_id=job_id,
        session_key=session_key,
        input_bytes=input_bytes,
        input_filename=input_filename,
        before_minutes=before_minutes,
        after_minutes=after_minutes,
        include_chain=include_chain,
        source_meta=source_meta,
    )

    if os.getenv("TOP20_SIX_MONTH_RESEARCH_ENABLED", "1").strip().lower() in {"0", "false", "no"}:
        return

    snapshot = _job_snapshot(job_id)
    if snapshot.get("status") != "done":
        return

    try:
        production.legacy.set_job(
            job_id,
            status="running",
            progress=99,
            message="Daily evidence published. Running automated six-month NIFTY50 F&O Top20 regime research...",
        )
        days = max(120, min(190, int(os.getenv("TOP20_RESEARCH_DAYS", "183"))))
        research_zip, research_summary = run_six_month_top20(session_key, days=days)

        uploaded, _manifest = production.publish_package(
            str(research_zip),
            production.EVIDENCE_FOLDER_ID,
            production.STATUS_FOLDER_ID or None,
            source_meta={
                "type": "nifty50_fno_top20_six_month_research",
                "trigger": "automatic_after_v6_evidence",
                "source_algostra_zip": (source_meta or {}).get("name", ""),
            },
            job_summary={
                "research_version": research_summary.get("research_version"),
                "top20": research_summary.get("top20", []),
                "validation": research_summary.get("validation", {}),
                "limitations": research_summary.get("limitations", []),
            },
        )

        latest = _job_snapshot(job_id)
        summary = dict(latest.get("summary", {}))
        summary.update({
            "top20_six_month_research": "completed",
            "top20_research_version": research_summary.get("research_version", ""),
            "top20_symbols": research_summary.get("top20", []),
            "top20_validation": research_summary.get("validation", {}),
            "top20_formula_features": research_summary.get("formula_features", {}),
            "top20_research_drive_file_id": uploaded.get("id", ""),
            "top20_research_drive_file_name": uploaded.get("name", ""),
            "top20_research_drive_file_url": uploaded.get("webViewLink", ""),
        })
        production.legacy.set_job(
            job_id,
            status="done",
            progress=100,
            message="Completed: daily evidence + six-month NIFTY50 F&O Top20 regime research published to Trading Brain Drive.",
            summary=summary,
        )
    except Exception as exc:
        latest = _job_snapshot(job_id)
        summary = dict(latest.get("summary", {}))
        summary.update({
            "top20_six_month_research": "error",
            "top20_research_error": str(exc),
        })
        production.legacy.set_job(
            job_id,
            status="done",
            progress=100,
            message=(
                "Daily evidence completed and published. Six-month Top20 research did not complete: "
                f"{exc}"
            ),
            summary=summary,
        )


# collect_route_v6 resolves job_worker_v6 from its production-module globals at run
# time, so replacing it here upgrades the existing Render flow without rewriting
# the frozen V5 reconstruction engine or the production V6 routes.
production.job_worker_v6 = job_worker_v6_with_top20_research


def privacy_policy_v6():
    return Response(
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading Brain V6 Privacy Policy</title>
<style>body{font-family:Arial,sans-serif;max-width:820px;margin:40px auto;padding:0 18px;line-height:1.6;color:#172033}h1,h2{color:#172033}a{color:#2457c5}</style></head>
<body>
<h1>Trading Brain V6 Privacy Policy</h1>
<p><strong>Last updated:</strong> 7 September 2026</p>
<p>Trading Brain V6 is a private, personal-use evidence collection tool. It connects to Google Drive only to read designated AlgoStra source files and to write generated trading-evidence packages back to folders selected by the user.</p>
<h2>Data accessed</h2>
<p>The application may access files in Google Drive that the user explicitly authorizes, together with data required to run the Definedge evidence pipeline.</p>
<h2>How data is used</h2>
<p>Authorized Drive data is used only to ingest AlgoStra source ZIP files and publish generated trading-evidence packages back to folders selected by the user. The application does not sell user data or use Google Drive data for advertising.</p>
<h2>Storage and credentials</h2>
<p>OAuth credentials and API secrets are stored as private environment variables in the user's Render service and are not intentionally written into Google Drive or GitHub. Generated evidence packages are stored in the user's Google Drive. Temporary processing files may exist on the Render service while a collection job runs.</p>
<h2>Sharing</h2>
<p>The application does not intentionally share Google user data with third parties except the infrastructure services required to operate the user's private workflow, such as Google Drive, Render, GitHub, and Definedge, subject to their respective terms and privacy policies.</p>
<h2>Revoking access</h2>
<p>The user can revoke Google account access at any time from the Google Account security permissions page and can remove the OAuth credentials from the Render service environment.</p>
<h2>Contact</h2>
<p>For questions about this personal application, contact the developer through the support email shown on the Google OAuth consent screen.</p>
<p><a href="/">Return to Trading Brain V6</a></p>
</body></html>""",
        mimetype="text/html",
    )


def terms_v6():
    return Response(
        """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trading Brain V6 Terms</title>
<style>body{font-family:Arial,sans-serif;max-width:820px;margin:40px auto;padding:0 18px;line-height:1.6;color:#172033}h1,h2{color:#172033}a{color:#2457c5}</style></head>
<body>
<h1>Trading Brain V6 Terms of Use</h1>
<p>Trading Brain V6 is a private personal-use research and evidence collection tool. It is provided for the user's own trading research workflow and does not provide investment advice, brokerage services, or guarantees of trading performance.</p>
<p>The user is responsible for maintaining their own Google, Render, Definedge, and GitHub accounts and for safeguarding all credentials associated with those services.</p>
<p><a href="/">Return to Trading Brain V6</a></p>
</body></html>""",
        mimetype="text/html",
    )


if "privacy_policy_v6" not in app.view_functions:
    app.add_url_rule("/privacy", endpoint="privacy_policy_v6", view_func=privacy_policy_v6, methods=["GET"])

if "terms_v6" not in app.view_functions:
    app.add_url_rule("/terms", endpoint="terms_v6", view_func=terms_v6, methods=["GET"])
