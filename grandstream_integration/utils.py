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


def sync_active_calls():
    """
    Update call logs that are still live, using UCM active calls.
    """
    active_calls = _safe_get_active_calls()
    if not active_calls:
        return

    active_uniqueids = {c["uniqueid"]: c["status"] for c in active_calls if c.get("uniqueid")}

    pending = frappe.db.get_all(
        "CRM Call Log",
        filters={
            "status": ["in", ["Initiated", "Ringing"]],
            "telephony_medium": "Grandstream",
        },
        fields=["name", "custom_uniqueid"],
        limit=50,
    )

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
    Find call logs stuck in Initiated/Ringing/In Progress.
    If they are no longer in the UCM active calls, fetch the CDR and update them.
    """
    from grandstream_integration.ucm_api import get_cdr_by_start_time

    pending = frappe.db.get_all(
        "CRM Call Log",
        filters={
            "status": ["in", ["Initiated", "Ringing", "In Progress"]],
            "telephony_medium": "Grandstream",
        },
        fields=["name", "custom_uniqueid", "start_time", "creation"],
        order_by="creation asc",
    )

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

    # Map uniqueid -> log name for fast matching
    uid_to_log = {
        log["custom_uniqueid"]: log["name"]
        for log in ended_pending
        if log.get("custom_uniqueid")
    }

    # Query the CDR with a window covering the oldest and newest pending call
    first = ended_pending[0]
    last = ended_pending[-1]

    start_time = first.get("start_time") or first.get("creation")
    end_time = last.get("start_time") or last.get("creation")

    cdr_records = get_cdr_by_start_time(start_time, end_time)
    if not cdr_records:
        return

    updated = 0
    for record in cdr_records:
        # The response has either a nested main_cdr or the record itself
        cdr = record.get("main_cdr") if isinstance(record, dict) and "main_cdr" in record else record
        if not isinstance(cdr, dict):
            continue

        uniqueid = cdr.get("uniqueid", "")
        log_name = uid_to_log.get(uniqueid)
        if not log_name:
            continue

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

            # A missing/odd recording must never block the status update
            recording = _parse_recording(cdr.get("recordfiles"))
            if recording:
                call_log.recording_url = recording

            call_log.save(ignore_permissions=True)
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

    if updated:
        frappe.db.commit()
        print(f"[SYNC ENDED] updated {updated} calls")