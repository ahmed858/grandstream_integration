import frappe
import requests
import urllib3
from werkzeug.wrappers import Response
from grandstream_integration.ucm_api import _login  #

# تعطيل تحذيرات الـ SSL للسيرفر الداخلي
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning) #

@frappe.whitelist()
def get_recording_url(call_log_name: str):
    """جلب دفق التسجيل الصوتي من Grandstream ومحاكاة سلوك الـ CRM الأصلي"""
    if not call_log_name or not frappe.db.exists("CRM Call Log", call_log_name):
        frappe.throw("Call log not found", frappe.DoesNotExistError)

    log = frappe.get_doc("CRM Call Log", call_log_name) #

    # لو المكالمة مش تبع Grandstream، حوّلها فوراً للدالة الأصلية للـ CRM
    if log.telephony_medium != "Grandstream": #
        from crm.integrations.api import get_recording_url as crm_get_recording_url #
        return crm_get_recording_url(call_log_name) #

    filename = log.get("recording_url") #
    if not filename: #
        frappe.throw("Recording URL/Filename not found", frappe.DoesNotExistError)

    # جلب الإعدادات وعمل تسجيل دخول للسيرفر
    settings = frappe.get_doc("Grandstream Settings", '5gs6lirpf6') #
    cookie = _login(settings) #
    if not cookie: #
        frappe.throw("Failed to authenticate with Grandstream UCM Server.") #

    # بناء رابط الـ Grandstream المباشر للملف
    ucm_url = ( #
        f"https://{settings.ucm_host}:{settings.ucm_port}" #
        f"/api?action=recapi&filedir=monitor&filename={filename}&cookie={cookie}" #
    )
    print("UCM URL:", ucm_url)      # ✅ اطبع الـ URL عشان نتأكد من الـ filename
    print("Filename:", filename)
    try:
        # طلب الملف من السيرفر كـ stream
        with requests.get(ucm_url, verify=False, stream=True, timeout=15) as r: #
            print("Status:", r.status_code)
            print("Content-Type:", r.headers.get("Content-Type"))
            print("First 100 bytes:", r.content[:100])
            r.raise_for_status() #
            
            # بناء كائن الـ Response بنفس أسلوب الـ CRM الأصلي تماماً
            response = Response() #
            response.data = r.content #
            
            # إرسال نوع الميديا المناسب (الـ Grandstream بيسجل wav)
            response.mimetype = "audio/wav"
            
            # إضافة هيدرز إضافية لضمان قراءة المتصفح للمشغل والـ Seek بسلاسة
            response.headers.update({
                "Content-Disposition": f'inline; filename="{filename}"',
                "Accept-Ranges": "bytes"
            })
            return response #

    except requests.exceptions.RequestException as e: #
        frappe.log_error(message=str(e), title="Grandstream Recording Proxy Error") #
        frappe.throw("Error communicating with Grandstream server.") #
        return e