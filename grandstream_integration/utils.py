import asyncio
import frappe
from panoramisk import Manager

SETTINGS_DOCTYPE = "Grandstream Settings"


def _get_settings(settings_name=None):
    """
    Return the Grandstream Settings document.

    Resolution order:
    1. The Single document, if the DocType is a Single.
    2. The requested record, if it still exists.
    3. The oldest record of the DocType (covers deleted/renamed records).
    Throws a clear error if nothing is configured.
    """
    if frappe.get_meta(SETTINGS_DOCTYPE).issingle:
        return frappe.get_single(SETTINGS_DOCTYPE)

    if settings_name and frappe.db.exists(SETTINGS_DOCTYPE, settings_name):
        return frappe.get_doc(SETTINGS_DOCTYPE, settings_name)

    names = frappe.get_all(
        SETTINGS_DOCTYPE, pluck="name", order_by="creation asc", limit_page_length=1
    )
    if names:
        return frappe.get_doc(SETTINGS_DOCTYPE, names[0])

    frappe.throw(f"No {SETTINGS_DOCTYPE} record found. Please create one.")


def _map_disposition(disposition: str) -> str:
    d = (disposition or "").upper()
    if d == "ANSWERED":  return "Completed"
    if d == "NO ANSWER": return "No Answer"
    if d == "BUSY":      return "Busy"
    return "Failed"


def _to_int(value):
    """Convert a CDR value to int, or return None if it is empty/invalid."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _valid_datetime(value):
    """Return the value if it is a usable datetime string, else None."""
    if value is None or str(value).strip() in ("", "0000-00-00 00:00:00"):
        return None
    return value


def _parse_recording(record_files):
    """
    Extract the recording file name from the CDR 'recordfiles' field.
    Returns None when the field is empty or has an unexpected format.
    """
    if not record_files or "/" not in record_files:
        return None
    return record_files.split("/")[1].replace("@", "") or None


def _queue_ai_analysis(call_log):
    """
    Queue the AI analysis of a finished call (does nothing unless AI is enabled).
    Any error is logged and swallowed: the AI feature must never break call syncing.
    """
    try:
        from grandstream_integration.ai.analysis import enqueue_analysis

        enqueue_analysis(call_log)
    except Exception:
        frappe.log_error(
            title=f"Grandstream: failed to queue AI analysis for {call_log.name}",
            message=frappe.get_traceback(),
        )


def _safe_get_active_calls():
    """
    Fetch active calls from UCM.
    Returns a list (possibly empty) on success, or None if UCM/settings
    are unavailable, so callers can skip the run instead of crashing.
    """
    from grandstream_integration.ucm_api import get_active_calls

    try:
        return get_active_calls() or []
    except Exception:
        frappe.log_error(
            title="Grandstream: failed to fetch active calls",
            message=frappe.get_traceback(),
        )
        return None


@frappe.whitelist()
def make_call(extension, destination, lead=None, deal=None):
    settings = _get_settings()

    async def _run():
        manager = Manager(
            host=settings.ucm_host,
            port=int(settings.ucm_port),
            username=settings.ami_user,
            secret=settings.get_password("ami_secret"),
        )
        try:
            await manager.connect()
            response = await manager.send_action({
                "Action":   "Originate",
                "Channel":  f"PJSIP/{extension}",
                "Exten":    destination,
                "Context":  "from-internal",
                "Timeout":  30000,
                "Priority": 1,
                "Async":    "true",
            })
            return response
        finally:
            manager.close()

    try:
        loop = asyncio.new_event_loop()
        try:
            response = loop.run_until_complete(_run())
        finally:
            loop.close()
    except Exception:
        # Keep the real cause in the Error Log; show a friendly message to the user
        frappe.log_error(title="Grandstream: originate failed", message=frappe.get_traceback())
        frappe.throw(msg="حدث خطأ أثناء إنشاء المكالمة، يرجى مراجعة المسؤول.",
                     title="فشل الاتصال")

    # Extract the Linkedid from the AMI response
    linkedid = None
    items = response if isinstance(response, list) else [response]
    for item in items:
        if hasattr(item, "get"):
            linkedid = item.get("Uniqueid") or item.get("UniqueID") or item.get("Linkedid")
            if linkedid:
                break

    final_id = linkedid
    created_at = frappe.utils.now_datetime().strftime("%Y-%m-%d %H:%M:%S")

    print(f"[CALL] Originate | linkedid={final_id} | {extension} → {destination}")

    try:
        call_log = frappe.get_doc({
            "doctype":           "CRM Call Log",
            "id":                final_id,
            "custom_uniqueid":   final_id,
            "custom_linkedid":   final_id,
            "from":              extension,
            "to":                destination,
            "type":              "Outgoing",
            "status":            "Initiated",
            "start_time":        created_at,  # used later to search the CDR
            "telephony_medium":  "Grandstream",
            "reference_doctype": "CRM Lead" if lead else ("CRM Deal" if deal else None),
            "reference_docname": lead or deal or None,
            "caller":            frappe.session.user
        })
        call_log.insert(ignore_permissions=True)
        frappe.db.commit()

        return {
            "success":  True,
            "call_log": call_log.name,
            "linkedid": final_id,
        }

    except Exception:
        frappe.log_error(title="Grandstream: call log insert failed", message=frappe.get_traceback())
        frappe.throw(msg="حدث خطأ أثناء إنشاء المكالمة، يرجى مراجعة المسؤول.",
                     title="فشل الاتصال")


PENDING_STATUSES = ["Initiated", "Ringing", "In Progress"]

# A call still pending after this many hours is not live any more. The scheduled syncs only
# look this far back, so logs that never got closed can't stretch the CDR request over
# months (a ~10 MB response that the UCM drops mid-transfer).
# Older logs are closed from the console with close_stale_pending_calls().
PENDING_LOOKBACK_HOURS = 6


def _recent_pending_logs(fields, limit=None):
    since = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-PENDING_LOOKBACK_HOURS)
    return frappe.db.get_all(
        "CRM Call Log",
        filters={
            "status": ["in", PENDING_STATUSES],
            "telephony_medium": "Grandstream",
            "creation": [">=", since],
        },
        fields=fields,
        order_by="creation asc",
        limit=limit,
    )


def sync_active_calls():
    """
    Update call logs that are still live, using UCM active calls.
    """
    active_calls = _safe_get_active_calls()
    if not active_calls:
        return

    active_uniqueids = {c["uniqueid"]: c["status"] for c in active_calls if c.get("uniqueid")}

    pending = _recent_pending_logs(["name", "custom_uniqueid"], limit=50)

    for log in pending:
        uid = log.get("custom_uniqueid")
        if uid and uid in active_uniqueids:
            new_status = active_uniqueids[uid]
            frappe.db.set_value("CRM Call Log", log["name"], "status", new_status)
            frappe.publish_realtime("call_log_update", {"name": log["name"], "status": new_status})
            print(f"[SYNC] {log['name']} → {new_status}")

    frappe.db.commit()


def sync_ended_calls():
    """
    Find recent call logs stuck in Initiated/Ringing/In Progress.
    If they are no longer in the UCM active calls, fetch the CDR and update them.
    """
    from grandstream_integration.ucm_api import get_cdr_by_start_time

    pending = _recent_pending_logs(["name", "custom_uniqueid", "start_time", "creation"])
    if not pending:
        return

    # Exclude currently active calls: whatever is not active has ended.
    # None means UCM is unreachable: skip the run so nothing is marked ended by mistake.
    active_calls = _safe_get_active_calls()
    if active_calls is None:
        return
    active_uniqueids = {c["uniqueid"] for c in active_calls if c.get("uniqueid")}

    ended_pending = [
        log for log in pending
        if log.get("custom_uniqueid") not in active_uniqueids
    ]

    if not ended_pending:
        return

    # Query the CDR with a window covering the oldest and newest pending call
    # (at most PENDING_LOOKBACK_HOURS wide).
    first = ended_pending[0]
    last = ended_pending[-1]

    start_time = first.get("start_time") or first.get("creation")
    end_time = last.get("start_time") or last.get("creation")

    cdr_records = get_cdr_by_start_time(start_time, end_time)
    if not cdr_records:
        return

    updated = _close_from_cdr(ended_pending, cdr_records)
    if updated:
        frappe.db.commit()
        print(f"[SYNC ENDED] updated {updated} calls")


def close_stale_pending_calls(dry_run=True, fail_unmatched=False):
    """
    One-off, from bench console: close Grandstream call logs still in Initiated / Ringing /
    In Progress after PENDING_LOOKBACK_HOURS, which the scheduled sync no longer looks at.

    The CDR is fetched one day at a time. Logs found in it get their real status, duration
    and recording. dry_run=True (default) only reports. fail_unmatched=True also marks the
    logs that have no CDR record (the call never reached the PBX) as Failed.
    AI analysis is not queued for these old calls.
    """
    from grandstream_integration.ucm_api import get_cdr_by_start_time

    since = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-PENDING_LOOKBACK_HOURS)
    stale = frappe.db.get_all(
        "CRM Call Log",
        filters={
            "status": ["in", PENDING_STATUSES],
            "telephony_medium": "Grandstream",
            "creation": ["<", since],
        },
        fields=["name", "custom_uniqueid", "start_time", "creation"],
        order_by="creation asc",
        limit_page_length=0,
    )

    by_day = {}
    for log in stale:
        started = frappe.utils.get_datetime(log.get("start_time") or log.get("creation"))
        by_day.setdefault(started.date(), []).append((started, log))

    matched, unmatched, updated = [], [], 0
    for day in sorted(by_day):
        entries = by_day[day]
        logs = [log for _, log in entries]
        records = get_cdr_by_start_time(min(t for t, _ in entries), max(t for t, _ in entries))
        found = _match_cdr(logs, records or [])
        matched += [log for log in logs if log["name"] in found]
        unmatched += [log for log in logs if log["name"] not in found]
        if not dry_run:
            updated += _close_from_cdr(logs, records or [], queue_ai=False)
            frappe.db.commit()

    if fail_unmatched and not dry_run:
        for log in unmatched:
            frappe.db.set_value("CRM Call Log", log["name"], "status", "Failed")
        updated += len(unmatched)
        frappe.db.commit()

    return {
        "stale": len(stale),
        "days": len(by_day),
        "found_in_cdr": len(matched),
        "not_in_cdr": len(unmatched),
        "updated": updated,
    }


def _cdr_legs(record):
    """main_cdr and every sub_cdr_N of a multi-leg record, or the flat record itself."""
    if not isinstance(record, dict):
        return []
    if "main_cdr" not in record:
        return [record]
    legs = [record["main_cdr"]]
    i = 1
    while f"sub_cdr_{i}" in record:
        legs.append(record[f"sub_cdr_{i}"])
        i += 1
    return [leg for leg in legs if isinstance(leg, dict)]


def _match_cdr(logs, cdr_records):
    """Map log name -> its CDR record. A log matches when any leg carries its uniqueid."""
    uid_to_log = {log["custom_uniqueid"]: log["name"] for log in logs if log.get("custom_uniqueid")}
    found = {}
    for record in cdr_records:
        for leg in _cdr_legs(record):
            log_name = uid_to_log.get(leg.get("uniqueid", ""))
            if log_name and log_name not in found:
                found[log_name] = record
    return found


def _close_from_cdr(logs, cdr_records, queue_ai=True):
    """Write status, times, duration and recording from the CDR onto the matching logs."""
    updated = 0
    for log_name, record in _match_cdr(logs, cdr_records).items():
        legs = _cdr_legs(record)
        # main_cdr (or the flat record) has the call totals
        cdr = legs[0]
        uniqueid = cdr.get("uniqueid", "")
        disposition = cdr.get("disposition", "")
        new_status = _map_disposition(disposition)

        try:
            call_log = frappe.get_doc("CRM Call Log", log_name)
            call_log.status = new_status

            end_time_val = _valid_datetime(cdr.get("end"))
            if end_time_val:
                call_log.end_time = end_time_val

            # Duration in Frappe CRM is in seconds: prefer billsec (talk time)
            seconds = _to_int(cdr.get("billsec"))
            if seconds is None:
                seconds = _to_int(cdr.get("duration"))
            if seconds is not None:
                call_log.duration = seconds

            # If the call was answered, the answer time is the real start
            answer_time = _valid_datetime(cdr.get("answer"))
            if answer_time:
                call_log.start_time = answer_time

            # A missing/odd recording must never block the status update.
            # The recording can sit on a sub leg rather than on main_cdr.
            recording = next(
                (r for r in (_parse_recording(leg.get("recordfiles")) for leg in legs) if r), None
            )
            if recording:
                call_log.recording_url = recording

            call_log.save(ignore_permissions=True)
            if queue_ai and new_status == "Completed" and call_log.recording_url:
                _queue_ai_analysis(call_log)
            frappe.publish_realtime("call_log_update", {
                "name":   log_name,
                "status": new_status,
            })
            print(f"[SYNC ENDED] {log_name} | uniqueid={uniqueid} | {disposition} → {new_status} | duration={seconds}s")
            updated += 1

        except Exception:
            frappe.log_error(
                title=f"Grandstream: sync_ended_calls failed for {log_name}",
                message=frappe.get_traceback(),
            )

    return updated
