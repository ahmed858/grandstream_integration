import frappe
from werkzeug.wrappers import Response

from grandstream_integration.ucm_api import fetch_recording


@frappe.whitelist()
def get_recording_url(call_log_name: str):
    """جلب التسجيل الصوتي من Grandstream ومحاكاة سلوك الـ CRM الأصلي"""
    if not call_log_name or not frappe.db.exists("CRM Call Log", call_log_name):
        frappe.throw("Call log not found", frappe.DoesNotExistError)

    log = frappe.get_doc("CRM Call Log", call_log_name)
    # Only users who can open this call may listen to it.
    log.check_permission("read")

    # لو المكالمة مش تبع Grandstream، حوّلها فوراً للدالة الأصلية للـ CRM
    if log.telephony_medium != "Grandstream":
        from crm.integrations.api import get_recording_url as crm_get_recording_url

        return crm_get_recording_url(call_log_name)

    filename = log.get("recording_url")
    if not filename:
        frappe.throw("Recording URL/Filename not found", frappe.DoesNotExistError)

    try:
        audio = fetch_recording(filename)
    except Exception:
        frappe.log_error(title="Grandstream Recording Proxy Error", message=frappe.get_traceback())
        frappe.throw("Error communicating with Grandstream server.")

    # بناء كائن الـ Response بنفس أسلوب الـ CRM الأصلي
    response = Response()
    response.data = audio
    response.mimetype = "audio/wav"  # الـ Grandstream بيسجل wav
    response.headers["Content-Disposition"] = f'inline; filename="{filename}"'
    return response
