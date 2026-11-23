import frappe
from grandstream_integration.utils import sync_active_calls, sync_ended_calls
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


@frappe.whitelist()
def sync_inbound_external_calls():
    settings = _get_settings('5gs6lirpf6')
    # ✅ لو مفيش last sync، ابدأ من امبارح مش دلوقتي
    start_time = settings.last_inbound_calls_sync or frappe.utils.add_days(frappe.utils.nowdate(), -1)
    records = get_cdr_by_start_time(start_time)

    if not records:
        return {"synced": 0, "skipped": 0}

    synced = 0
    skipped = 0

    for record in records:
        try:
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

            call_log = frappe.get_doc({
                "doctype":           "CRM Call Log",
                "id":                uniqueid,
                "name":              uniqueid,
                "custom_uniqueid":   uniqueid,
                "custom_linkedid":   uniqueid,
                "telephony_medium":  "Grandstream",
                "from":              caller_num,
                "to":                answered_by or sub_1.get("dst", ""),  # ✅
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

        except Exception as e:
            frappe.log_error(
                f"Inbound Sync Error:\n{str(e)}\n\nRecord:\n{record}",
                "Inbound Call Sync"
            )
            continue

    if synced > 0:
        frappe.db.set_value(
            "Grandstream Settings",
            '5gs6lirpf6',
            "last_inbound_calls_sync",
            frappe.utils.now()
        )
        frappe.db.commit()

    frappe.logger().info(f"[Inbound Sync] ✅ Synced: {synced} | ⏭ Skipped: {skipped}")
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