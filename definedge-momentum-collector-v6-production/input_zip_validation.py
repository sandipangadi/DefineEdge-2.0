import hashlib
import io
import re
import zipfile


def _positions_strategy(text):
    lines = text.splitlines()
    strategy = ""
    has_header = False
    for line in lines[:40]:
        stripped = line.strip()
        if stripped.lower().startswith("strategy:"):
            strategy = stripped.split(":", 1)[1].strip()
        if (
            "Entry Qty 1" in line
            and "Entry Price 1" in line
            and "Entry Time 1" in line
            and "Order Status" in line
        ):
            has_header = True
    if strategy and has_header:
        return strategy
    return ""


def _activity_strategy(text):
    first = text.splitlines()[0].strip() if text.splitlines() else ""
    if "Timestamp" not in first or "Category" not in first or "Message" not in first:
        return ""

    candidates = []
    for match in re.finditer(
        r"\bYour\s+(.+?)\s+strategy\s+(?:has|is|was|completed|hit|been)\b",
        text,
        flags=re.IGNORECASE,
    ):
        candidate = match.group(1).strip()
        if candidate:
            candidates.append(candidate)

    if not candidates:
        for line in text.splitlines():
            if "RECEIVED:" not in line.upper():
                continue
            # Typical AlgoStra raw row:
            # ...,RECEIVED:,LIQ20 BULL AP V1 : PANDF : ONE_MIN
            tail = line.split("RECEIVED:", 1)[-1].lstrip(", ")
            candidate = tail.split(" : ", 1)[0].strip().strip(",")
            if candidate:
                candidates.append(candidate)

    if not candidates:
        return ""

    counts = {}
    for item in candidates:
        counts[item] = counts.get(item, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def validate_and_sanitize_algostra_zip(zf):
    """Validate a current AlgoStra export ZIP without assuming a fixed strategy count.

    Requirements:
    - at least one valid Positions report with one or more trade rows
    - at least one valid Activity Log
    - every strategy represented by a Positions report must also have Activity Log evidence

    Exact duplicate CSV files are removed before the reconstruction engine runs. This
    prevents repeated browser downloads (for example duplicate rebalance_logs files)
    from double-counting activity events.
    """
    original_csv_names = [
        name for name in zf.namelist()
        if name.lower().endswith(".csv") and not name.endswith("/")
    ]
    if not original_csv_names:
        raise RuntimeError("The uploaded ZIP contains no CSV files.")

    unique_entries = []
    seen_hashes = {}
    duplicate_files = []
    unknown_files = []
    positions_files = []
    activity_files = []
    position_strategies = set()
    activity_strategies = set()

    for name in original_csv_names:
        raw = zf.read(name)
        digest = hashlib.sha256(raw).hexdigest()
        if digest in seen_hashes:
            duplicate_files.append({"filename": name, "duplicate_of": seen_hashes[digest]})
            continue
        seen_hashes[digest] = name

        text = raw.decode("utf-8-sig", errors="replace")
        p_strategy = _positions_strategy(text)
        a_strategy = _activity_strategy(text)

        if p_strategy:
            positions_files.append({"filename": name, "strategy": p_strategy})
            position_strategies.add(p_strategy)
            file_type = "positions"
        elif a_strategy:
            activity_files.append({"filename": name, "strategy": a_strategy})
            activity_strategies.add(a_strategy)
            file_type = "activity_log"
        else:
            unknown_files.append(name)
            file_type = "unknown"

        unique_entries.append((name, raw, file_type))

    if unknown_files:
        raise RuntimeError(
            "Unrecognized CSV file(s) in AlgoStra ZIP: " + ", ".join(unknown_files[:10])
        )
    if not positions_files:
        raise RuntimeError(
            "No valid AlgoStra Positions report found. For a zero-trade day, keep the logs for archive, but the market-path reconstruction requires at least one Positions trade."
        )
    if not activity_files:
        raise RuntimeError("No valid AlgoStra Activity Log found in the ZIP.")

    missing_logs = sorted(position_strategies - activity_strategies)
    if missing_logs:
        raise RuntimeError(
            "Missing Activity Log evidence for traded strategy/strategies: "
            + ", ".join(missing_logs)
            + ". Nothing was sent to Definedge."
        )

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as clean:
        for name, raw, _file_type in unique_entries:
            clean.writestr(name, raw)

    audit = {
        "csv_count_original": len(original_csv_names),
        "csv_count_sanitized": len(unique_entries),
        "positions_file_count": len(positions_files),
        "activity_file_count": len(activity_files),
        "duplicate_csv_count_removed": len(duplicate_files),
        "duplicate_files_removed": duplicate_files,
        "traded_strategies": sorted(position_strategies),
        "activity_strategies": sorted(activity_strategies),
        "activity_only_strategies": sorted(activity_strategies - position_strategies),
    }
    return out.getvalue(), audit
