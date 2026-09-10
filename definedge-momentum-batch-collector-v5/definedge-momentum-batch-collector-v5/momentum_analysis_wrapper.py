"""V6-only publication wrapper for momentum-pattern correlation artifacts."""

from momentum_pattern_analysis import append_analysis_to_zip


def wrap_momentum_analysis(production, original_worker):
    def wrapped(job_id, session_key, input_bytes, input_filename, before_minutes, after_minutes, include_chain, source_meta=None):
        original_publish = production.publish_package

        def publish_with_analysis(output_path, evidence_folder_id, status_folder_id=None, source_meta=None, job_summary=None):
            # Read the all-options observation manifest if the inner wrapper already
            # appended it. If not present, continue safely with an empty manifest.
            manifest = {}
            try:
                import json
                import zipfile
                with zipfile.ZipFile(output_path, "r") as zf:
                    target = "all_options/ALL_OPTIONS_OBSERVATION.json"
                    if target in zf.namelist():
                        manifest = json.loads(zf.read(target).decode("utf-8"))
            except Exception:
                manifest = {}

            pack = append_analysis_to_zip(output_path, manifest)
            meta = dict(source_meta or {})
            summary = dict(job_summary or {})
            if pack:
                meta["momentum_pattern_correlation"] = True
                summary.update({
                    "momentum_pattern_correlation": True,
                    "momentum_correlation_trade_queue_count": pack.get("executed_trade_queue_count", 0),
                    "momentum_correlation_no_trade_queue_count": pack.get("no_trade_strategy_queue_count", 0),
                })
            return original_publish(
                output_path,
                evidence_folder_id,
                status_folder_id,
                source_meta=meta,
                job_summary=summary,
            )

        production.publish_package = publish_with_analysis
        try:
            return original_worker(
                job_id=job_id,
                session_key=session_key,
                input_bytes=input_bytes,
                input_filename=input_filename,
                before_minutes=before_minutes,
                after_minutes=after_minutes,
                include_chain=include_chain,
                source_meta=source_meta,
            )
        finally:
            production.publish_package = original_publish

    return wrapped
