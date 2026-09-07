# Definedge Momentum Collector V6 Production

This folder is the single production control layer for the Definedge Momentum evidence pipeline.

## Architecture

- `app_v6.py` owns every user-visible Flask route: home, OTP, collect, status and health.
- The proven V5 `app.py` is loaded only as a frozen reconstruction engine for parsing, Definedge market-data retrieval, MFE/MAE, chain reconstruction, duplicate mapping and ZIP construction.
- `drive_bridge.py` owns Google Drive inbox reading and evidence/manifest publishing.
- `templates/index_v6.html` and `templates/job_v6.html` are the only production UI templates.
- `requirements.txt` and `render.yaml` are the authoritative production deployment files.

## Important

The old V2/V3/V5 folders are historical/reference code and are not production entrypoints. Do not point Render to `app:app` from the V5 folder.

The production start command is:

`gunicorn --workers 1 --threads 4 --timeout 300 app_v6:app --bind 0.0.0.0:$PORT`

The intended Render root directory is:

`definedge-momentum-collector-v6-production`

## V6 flow

Drive AlgoStra Inbox -> Definedge OTP -> frozen V5 reconstruction engine -> option/underlying/ATM chain ticks -> MFE/MAE/give-back/duplicates/strategy comparison -> Drive Daily Evidence + manifest.
