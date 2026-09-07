"""Compatibility shim for the existing Render V6 start command.

Production V6 code now lives in:
    /definedge-momentum-collector-v6-production/app_v6.py

The old V5 directory remains only because its app.py is the frozen,
proven reconstruction engine and Render currently starts from this path.
"""

import importlib.util
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
<p>Authorized Drive data is used only to ingest AlgoStra source ZIP files and publish generated evidence ZIPs and manifests. The application does not sell user data or use Google Drive data for advertising.</p>
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
