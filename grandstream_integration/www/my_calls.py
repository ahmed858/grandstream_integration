"""
/my-calls: each marketer sees the AI evaluation of their own calls, to learn from them.
Managers (Sales Manager / System Manager) can open any marketer with ?marketer=<user>.
"""

import frappe
from frappe import _

from grandstream_integration.ai.reporting import (
	CRITERIA,
	DETAIL_FIELDS,
	analysed_calls,
	is_manager,
	parse_json,
)

no_cache = 1
PAGE_SIZE = 50


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/my-calls"
		raise frappe.Redirect

	user = frappe.session.user
	manager = is_manager(user)
	marketer = frappe.form_dict.get("marketer") if manager and frappe.form_dict.get("marketer") else user

	context.no_cache = 1
	context.show_sidebar = False
	context.title = _("My Call Reviews")
	context.marketer = marketer
	context.is_manager = manager

	call_name = frappe.form_dict.get("call")
	if call_name:
		context.call = _call_detail(call_name, user, manager)
		return context

	calls = analysed_calls(marketer=marketer, limit=PAGE_SIZE)
	scores = [c.custom_ai_overall_score for c in calls if c.custom_ai_overall_score]
	context.calls = calls
	context.summary = {
		"count": len(calls),
		"avg": round(sum(scores) / len(scores), 2) if scores else None,
		"success": sum(1 for c in calls if c.custom_ai_success),
	}
	return context


def _call_detail(call_name, user, manager):
	rows = frappe.get_all(
		"CRM Call Log",
		filters={"name": call_name, "custom_ai_status": "Done"},
		fields=DETAIL_FIELDS,
		limit=1,
	)
	if not rows or (rows[0].custom_ai_marketer != user and not manager):
		raise frappe.PermissionError(_("You can only open your own calls."))

	call = rows[0]
	labels = {ar: en for ar, en, _key in CRITERIA}
	call.scores = [
		{**s, "label": labels.get(s.get("criterion"), s.get("criterion"))}
		for s in parse_json(call.custom_ai_scores)
	]
	call.objections = parse_json(call.custom_ai_objections)
	call.coaching = [
		line.lstrip("- ").strip() for line in (call.custom_ai_coaching or "").splitlines() if line.strip()
	]
	call.recording_path = (
		f"/api/method/grandstream_integration.api.get_recording_url?call_log_name={call.name}"
		if call.recording_url
		else None
	)
	return call
