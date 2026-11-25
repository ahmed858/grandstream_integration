import frappe
from grandstream_integration.utils import _map_disposition, _queue_ai_analysis, sync_active_calls, sync_ended_calls
from grandstream_integration.ucm_api import get_cdr_by_start_time, _get_settings

@frappe.whitelist()
def sync_calls():
    """
    Scheduled Job - All (كل دقيقة تقريباً)
    """
    try:
        
        sync_active_calls()
        sync_ended_calls()
    except Exception as e:
        frappe.log_error(f"Sync Calls Error: {str(e)}", "Grandstream Sync")


INBOUND_SYNC_OVERLAP_HOURS = 1


def _advance_inbound_sync_point(previous):
    new_point = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-INBOUND_SYNC_OVERLAP_HOURS)
    if previous and frappe.utils.get_datetime(previous) >= new_point:
        return  # never move it backwards
    frappe.db.set_value(
        "Grandstream Settings",
        '5gs6lirpf6',
        "last_inbound_calls_sync",
        new_point,
        update_modified=False,
    )
    frappe.db.commit()


@frappe.whitelist()
def sync_inbound_external_calls():
    """
    Every 30 minutes: create CRM Call Logs for external calls found in the UCM CDR.
    Despite the name it handles both directions: calls coming in from outside, and
    calls dialled out directly from Wave / desk phones (calls made with the CRM call
    button already have a log and are skipped).
    """
    settings = _get_settings('5gs6lirpf6')
    # ✅ لو مفيش last sync، ابدأ من امبارح مش دلوقتي
    start_time = settings.last_inbound_calls_sync or frappe.utils.add_days(frappe.utils.nowdate(), -1)
    result = _sync_external_calls(start_time)
    if result is None:
        return {"synced": 0, "skipped": 0}

    # Move the sync point forward on every successful fetch, not only when a call was
    # created. Otherwise, with no new inbound calls, every run re-reads everything since
    # the last inbound call. Keep one hour of overlap for calls still in progress at
    # fetch time; calls already in the CRM are skipped by custom_uniqueid.
    _advance_inbound_sync_point(settings.last_inbound_calls_sync)

    frappe.logger().info(f"[External Sync] ✅ Synced: {result['synced']} | ⏭ Skipped: {result['skipped']}")
    return result


def backfill_external_calls(since):
    """
    One-off, from bench console: import external calls (both directions) that started
    after `since`, e.g. backfill_external_calls("2026-11-01"). Calls already in the CRM
    are skipped. Does not move the scheduled sync point.
    """
    return _sync_external_calls(str(since))


def _sync_external_calls(start_time):
    """Create logs for external calls since start_time. None when the CDR fetch returned nothing."""
    # get_cdr_by_start_time needs "YYYY-MM-DD HH:MM:SS"; accept a bare date or a datetime too.
    start_time = frappe.utils.get_datetime(start_time).strftime("%Y-%m-%d %H:%M:%S")
    records = get_cdr_by_start_time(start_time)

    if not records:
        return None

    synced = 0
    skipped = 0

    for record in records:
        try:
            # Outbound calls dialled directly from Wave / desk phones.
            # None means "not an outbound external call": fall through to the inbound check.
            created = _sync_outbound_record(record)
            if created is not None:
                if created:
                    synced += 1
                else:
                    skipped += 1
                continue

            sub_1 = record.get("sub_cdr_1", {})
            if not sub_1:
                skipped += 1
                continue

            is_inbound  = sub_1.get("userfield") == "Inbound"
            has_trunk   = sub_1.get("src_trunk_name", "") != ""
            is_external = len(str(sub_1.get("src", ""))) > 5

            if not (is_inbound and has_trunk and is_external):
                skipped += 1
                continue

            uniqueid   = sub_1.get("uniqueid", "")
            caller_num = sub_1.get("src", "")
            start      = sub_1.get("start", "")

            answered_by, final_status, billsec = _get_final_disposition(record)
            end_time = _get_end_time(record)

            if frappe.db.exists("CRM Call Log", {"custom_uniqueid": uniqueid}):
                skipped += 1
                continue

            recording_url = ""
            for sub in _get_all_sub_cdrs(record):
                rf = sub.get("recordfiles", "")
                if rf:
                    recording_url = rf.split("@")[0].split("/")[-1]
                    break

            # ✅ ابحث بالرقم المحلي الأول
            lead_name = frappe.db.get_value("CRM Lead", {"mobile_no": caller_num}, "name")

            # ✅ لو مش لاقي، جرب بالرقم الدولي
            if not lead_name:
                caller_intl = "966" + caller_num.lstrip('0')
                lead_name = frappe.db.get_value("CRM Lead", {"mobile_no": caller_intl}, "name")

            # Extension that answered, or the one dialled when nobody answered.
            to_number = answered_by or sub_1.get("dst", "")

            call_log = frappe.get_doc({
                "doctype":           "CRM Call Log",
                "id":                uniqueid,
                "name":              uniqueid,
                "custom_uniqueid":   uniqueid,
                "custom_linkedid":   uniqueid,
                "telephony_medium":  "Grandstream",
                "from":              caller_num,
                "to":                to_number,
                # The marketer behind that extension; empty for queues / ring groups.
                "receiver":          _user_for_extension(to_number),
                "type":              "Incoming",
                "status":            "Completed" if final_status == "ANSWERED" else "Missed",
                "duration":          int(billsec or 0),
                "start_time":        start,
                "end_time":          end_time,
                "recording_url":     recording_url,
                "reference_doctype": "CRM Lead" if lead_name else "",
                "reference_docname": lead_name or "",              # ✅ string مش object
            })
            call_log.insert(ignore_permissions=True)
            frappe.db.commit()
            synced += 1
            if call_log.status == "Completed" and recording_url:
                _queue_ai_analysis(call_log)

        except Exception as e:
            frappe.log_error(
                f"Inbound Sync Error:\n{str(e)}\n\nRecord:\n{record}",
                "Inbound Call Sync"
            )
            continue

    return {"synced": synced, "skipped": skipped}
    settings = _get_settings('5gs6lirpf6')
    start_time = settings.last_inbound_calls_sync or frappe.utils.now()
    records = get_cdr_by_start_time(start_time)
    # print(records)
    if not records:
        return {"synced": 0, "skipped": 0}

    synced = 0
    skipped = 0

    for record in records:
        try:
            # ─── 1. جيب sub_cdr_1 ───
            sub_1 = record.get("sub_cdr_1", {})
            if not sub_1:
                skipped += 1
                continue

            # ─── 2. فلتر External Inbound ───
            is_inbound  = sub_1.get("userfield") == "Inbound"
            has_trunk   = sub_1.get("src_trunk_name", "") != ""
            is_external = len(str(sub_1.get("src", ""))) > 5

            if not (is_inbound and has_trunk and is_external):
                skipped += 1
                continue

            # ─── 3. البيانات الأساسية ───
            main_cdr   = record.get("main_cdr", {})
            uniqueid   = sub_1.get("uniqueid", "")
            session_id = sub_1.get("session", "")
            caller_num = sub_1.get("src", "")       # رقم المتصل الخارجي
            start      = sub_1.get("start", "")
            trunk      = sub_1.get("src_trunk_name", "")

            # ─── 4. النتيجة النهائية ───
            answered_by, final_status, billsec = _get_final_disposition(record)
            end_time = _get_end_time(record)

            # ─── 5. تجنب التكرار بالـ uniqueid ───
            if frappe.db.exists("CRM Call Log", {"custom_uniqueid": uniqueid}):
                skipped += 1
                continue

            # ─── 6. Recording ───
            recording_url = ""
            for sub in _get_all_sub_cdrs(record):
                rf = sub.get("recordfiles", "")
                if rf:
                    # بيجي بالشكل ده: "2026-05/auto-xxx-src-dst.wav@"
                    recording_url = rf.split("@")[0].split("/")[-1]
                    break
            
            # get the refrance lead
            lead = frappe.get_doc("CRM Lead",{"mobile_no" :caller_num })
            if not lead:
                caller_num ="966" + caller_num.replace('0','')
                lead = frappe.get_list("CRM Lead",{"mobile_no" :caller_num })


            # ─── 7. إنشاء CRM Call Log ───
            call_log = frappe.get_doc({
                "doctype":          "CRM Call Log",
                "id":               uniqueid,
                "name":             uniqueid,
                "custom_uniqueid":  uniqueid,
                "custom_linkedid":  uniqueid,
                "telephony_medium": "Grandstream",
                "from":             caller_num,               # المتصل الخارجي
                "to":               answered_by or sub_1.get("src", ""),  # اللي رد
                "type":             "Incoming",
                "status":           "Completed" if final_status == "ANSWERED" else "Missed",
                "duration":         int(billsec or 0),
                "start_time":       start,
                "end_time":         end_time,
                "recording_url":    recording_url,
                "reference_doctype": "CRM Lead" if lead else "",
                "reference_docname":lead
                # "caller":           frappe.session.user,
            })
            call_log.insert(ignore_permissions=True)
            frappe.db.commit()
            synced += 1

        except Exception as e:
            frappe.log_error(
                f"Inbound Sync Error:\n{str(e)}\n\nRecord:\n{record}",
                "Inbound Call Sync"
            )
            continue

    # ─── 8. حدّث last_inbound_calls_sync ───
    
    frappe.db.set_value(
        "Grandstream Settings",
        '5gs6lirpf6',
        "last_inbound_calls_sync",
        frappe.utils.now()
    )
    frappe.db.commit()

    frappe.logger().info(f"[Inbound Sync] ✅ Synced: {synced} | ⏭ Skipped: {skipped}")
    return {"synced": synced, "skipped": skipped}
# ─── Outbound calls dialled outside the CRM ────────────────

# Shorter destinations are service/short codes (e.g. "33"), not customer numbers.
MIN_EXTERNAL_NUMBER_LENGTH = 6


def _sync_outbound_record(record: dict):
    """
    Create a CRM Call Log for an outbound external call dialled outside the CRM.
    Returns None if the record is not an outbound external call, False if it was
    skipped (already in the CRM), True if a log was created.

    A single-leg call comes from the UCM as a flat record (no sub_cdr_1); a multi-leg
    call has main_cdr + sub_cdr_N. Both are handled.
    """
    legs = _get_all_sub_cdrs(record) or ([record] if record.get("uniqueid") else [])
    if not legs:
        return None
    first = legs[0]

    customer_num = str(first.get("dst", "") or "")
    is_outbound = first.get("userfield") == "Outbound"
    to_trunk = bool(first.get("dst_trunk_name"))
    if not (is_outbound and to_trunk and len(customer_num) >= MIN_EXTERNAL_NUMBER_LENGTH):
        return None

    uniqueid = first.get("uniqueid", "")
    # Calls made with the CRM button already have a log (make_call stores the UCM id).
    # Check every id of the call so a button call is never logged twice.
    ids = {leg.get("uniqueid") for leg in legs} | {(record.get("main_cdr") or {}).get("uniqueid")}
    ids = [i for i in ids if i]
    if not uniqueid or frappe.db.exists("CRM Call Log", {"custom_uniqueid": ["in", ids]}) \
            or frappe.db.exists("CRM Call Log", {"name": ["in", ids]}):
        return False

    answered = [leg for leg in legs if leg.get("disposition") == "ANSWERED" and int(leg.get("billsec") or 0) > 0]
    disposition = "ANSWERED" if answered else (legs[-1].get("disposition") or "NO ANSWER")
    billsec = int((answered[-1] if answered else first).get("billsec") or 0)
    end_time = next((leg.get("end") for leg in reversed(legs) if leg.get("end")), "")

    recording_url = ""
    for leg in legs:
        rf = leg.get("recordfiles", "")
        if rf:
            recording_url = rf.split("@")[0].split("/")[-1]
            break

    extension = str(first.get("src", "") or "")
    marketer = _user_for_extension(extension)
    lead_name = _find_lead_by_number(customer_num)

    call_log = frappe.get_doc({
        "doctype":           "CRM Call Log",
        "id":                uniqueid,
        "name":              uniqueid,
        "custom_uniqueid":   uniqueid,
        "custom_linkedid":   uniqueid,
        "telephony_medium":  "Grandstream",
        "from":              extension,
        "to":                customer_num,
        "type":              "Outgoing",
        "status":            _map_disposition(disposition),
        "duration":          billsec,
        "start_time":        first.get("start", ""),
        "end_time":          end_time,
        "recording_url":     recording_url,
        "caller":            marketer,
        "reference_doctype": "CRM Lead" if lead_name else "",
        "reference_docname": lead_name or "",
    })
    call_log.insert(ignore_permissions=True)
    frappe.db.commit()
    if call_log.status == "Completed" and recording_url:
        _queue_ai_analysis(call_log)
    return True


def _user_for_extension(extension):
    """The user mapped to a PBX extension in User Extension, or None."""
    extension = str(extension or "").strip()
    if not extension:
        return None
    return frappe.db.get_value("User Extension", {"extension": extension}, "user")


def backfill_call_receivers(dry_run=True):
    """
    One-off, from bench console: fill `receiver` on synced incoming calls that have none,
    using today's User Extension mapping for the extension in `to`.
    dry_run=True (default) only counts; pass dry_run=False to write. `modified` is kept.
    """
    extension_users = {
        str(row.extension).strip(): row.user
        for row in frappe.get_all("User Extension", fields=["extension", "user"])
        if row.extension and row.user
    }
    logs = frappe.get_all(
        "CRM Call Log",
        filters={"type": "Incoming", "telephony_medium": "Grandstream", "receiver": ["is", "not set"]},
        fields=["name", "to"],
        limit_page_length=0,
    )
    matches = [
        (log.name, extension_users[str(log.to).strip()])
        for log in logs
        if log.to and str(log.to).strip() in extension_users
    ]

    if not dry_run:
        for name, user in matches:
            frappe.db.set_value("CRM Call Log", name, "receiver", user, update_modified=False)
        frappe.db.commit()

    return {
        "incoming_without_receiver": len(logs),
        "matched": len(matches),
        "updated": 0 if dry_run else len(matches),
    }


def _find_lead_by_number(number: str):
    """Match a CRM Lead by mobile number, as stored locally (05...) or internationally (966... / +966...)."""
    number = str(number or "").strip()
    if not number:
        return None
    local = number.lstrip("+")
    if local.startswith("966"):
        local = "0" + local[3:]
    intl = "966" + local.lstrip("0")
    for candidate in (number, local, intl, "+" + intl):
        lead = frappe.db.get_value("CRM Lead", {"mobile_no": candidate}, "name")
        if lead:
            return lead
    return None


# ─── Helper Functions ───────────────────────────────────────

def _get_all_sub_cdrs(record: dict) -> list:

    subs = []
    i = 1
    while True:
        key = f"sub_cdr_{i}"
        if key in record:
            subs.append(record[key])
            i += 1
        else:
            break
    return subs


def _get_final_disposition(record: dict):
    """
    ارجع (answered_by, status, billsec)
    """
    subs = _get_all_sub_cdrs(record)
    answered = [
        s for s in subs
        if s.get("disposition") == "ANSWERED" and int(s.get("billsec", 0)) > 0
    ]
    if answered:
        last = answered[-1]
        return last.get("dst"), "ANSWERED", last.get("billsec", 0)
    return None, "NO ANSWER", 0


def _get_end_time(record: dict) -> str:
    """
    جيب وقت نهاية المكالمة من آخر sub_cdr
    """
    subs = _get_all_sub_cdrs(record)
    for sub in reversed(subs):
        if sub.get("end"):
            return sub.get("end")
    return ""