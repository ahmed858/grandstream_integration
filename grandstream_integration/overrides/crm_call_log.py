import frappe
from crm.fcrm.doctype.crm_call_log.crm_call_log import CRMCallLog

class CustomCRMCallLog(CRMCallLog):
    def as_dict(self, *args, **kwargs):
        # تمرير الـ arguments بشكل ديناميكي وآمن تماماً مهما كان إصدار فابي
        d = super().as_dict(*args, **kwargs)
        
        # تنفيذ منطق جلب التسجيل لـ Grandstream
        if d.get("recording_url") and d.get("telephony_medium") == "Grandstream":
            d["recording_url_path"] = (
                f"/api/method/grandstream_integration.api.get_recording_url"
                f"?call_log_name={d.get('name')}"
            )
        return d