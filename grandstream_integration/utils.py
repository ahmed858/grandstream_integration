import asyncio
import uuid
import frappe
from panoramisk import Manager


def _get_settings(settings_name):
    return frappe.get_doc("Grandstream Settings", settings_name)


def _map_disposition(disposition: str) -> str:
    d = (disposition or "").upper()
    if d == "ANSWERED":  return "Completed"
    if d == "NO ANSWER": return "No Answer"
    if d == "BUSY":      return "Busy"
    return "Failed"


@frappe.whitelist()
def make_call(extension, destination, lead=None,deal=None):
    settings = _get_settings('00rv6m8dfv')

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
               "Channel": f"PJSIP/{extension}",
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
        response = loop.run_until_complete(_run())
        loop.close()
    except Exception as e:
        frappe.throw(msg="حدث خطأ أثناء إنشاء المكالمة، يرجى مراجعة المسؤول.", 
            title="فشل الاتصال")

    # استخراج الـ Linkedid
    linkedid = None
    items = response if isinstance(response, list) else [response]
    for item in items:
        if hasattr(item, "get"):
            linkedid =  item.get("Uniqueid") or item.get("UniqueID") or item.get("Linkedid") 
            if linkedid:
                break
    
    final_id   = linkedid  
    # frappe.msgprint(final_id)
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
            "start_time":        created_at,   # ← وقت بداية المكالمة للبحث في CDR لاحقاً
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

    except Exception as e:
        frappe.throw(msg="حدث خطأ أثناء إنشاء المكالمة، يرجى مراجعة المسؤول.", 
            title="فشل الاتصال")


def sync_active_calls():
    """
    يحدث الـ Call Logs اللي لسه شغالة من UCM Active Calls
    """
    from grandstream_integration.ucm_api import get_active_calls

    active_calls = get_active_calls()
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
    يدور على المكالمات اللي حالتها Initiated/Ringing/In Progress
    ولو مش موجودة في الـ active calls، يجيب الـ CDR ويحدثها
    """
    from grandstream_integration.ucm_api import get_active_calls, get_cdr_by_start_time

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

    # نستبعد المكالمات الـ active حالياً — اللي مش active هي اللي خلصت
    active_calls = get_active_calls()
    active_uniqueids = {c["uniqueid"] for c in active_calls if c.get("uniqueid")}

    ended_pending = [
        log for log in pending
        if log.get("custom_uniqueid") not in active_uniqueids
    ]

    if not ended_pending:
        return

    # نبني map من uniqueid → log name عشان نطابق بسرعة
    uid_to_log = {
        log["custom_uniqueid"]: log["name"]
        for log in ended_pending
        if log.get("custom_uniqueid")
    }

    # نجيب CDR بنافذة زمنية تغطي أقدم وأحدث مكالمة
    first = ended_pending[0]
    last  = ended_pending[-1]

    start_time = first.get("start_time") or first.get("creation")
    end_time   = last.get("start_time")  or last.get("creation")

    cdr_records = get_cdr_by_start_time(start_time, end_time)
    if not cdr_records:
        return

    updated = 0
    for record in cdr_records:
        # الـ response فيه main_cdr جوا أو الـ record نفسه مباشرة
        cdr = record.get("main_cdr") if isinstance(record, dict) and "main_cdr" in record else record
        if not isinstance(cdr, dict):
            continue

        uniqueid = cdr.get("uniqueid", "")
        log_name = uid_to_log.get(uniqueid)
        if not log_name:
            continue

        # استخراج البيانات من الـ CDR
        end_time_val = cdr.get("end")       # "2026-05-17 14:17:56"
        answer_time  = cdr.get("answer")    # "2026-05-17 14:16:21"
        duration     = cdr.get("duration")  # "97"  (seconds, total)
        billsec      = cdr.get("billsec")   # "95"  (seconds, talk time)
        disposition  = cdr.get("disposition", "")
        new_status   = _map_disposition(disposition)
        record_files   = cdr.get("recordfiles")

        try:
            call_log = frappe.get_doc("CRM Call Log", log_name)

            call_log.status   = new_status
            call_log.end_time = end_time_val

            # duration في Frappe CRM بالثواني — نستخدم billsec (وقت الكلام الفعلي)
            if billsec is not None:
                call_log.duration = int(billsec)
            elif duration is not None:
                call_log.duration = int(duration)

            # لو المكالمة اتردت خلي answer time هو الـ start الحقيقي
            if answer_time and str(answer_time).strip() not in ("", "0000-00-00 00:00:00"):
                call_log.start_time = answer_time

            call_log.recording_url = record_files.split('/')[1].replace('@','')
            
            call_log.save(ignore_permissions=True)
            frappe.publish_realtime("call_log_update", {
                "name":   log_name,
                "status": new_status,
            })
            print(f"[SYNC ENDED] {log_name} | uniqueid={uniqueid} | {disposition} → {new_status} | duration={billsec}s")
            updated += 1

        except Exception as e:
            frappe.log_error(f"[sync_ended_calls] فشل تحديث {log_name}: {str(e)}")

    if updated:
        frappe.db.commit()
        print(f"[SYNC ENDED] تم تحديث {updated} مكالمة")