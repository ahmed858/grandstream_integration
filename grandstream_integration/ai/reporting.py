"""
Read helpers shared by the marketer page (/my-calls) and the manager report.

The evaluation fields are permlevel 1, so they are read here with frappe.get_all
(no field-level permission filtering) after the caller has checked who may see what.
"""

import json

import frappe

MANAGER_ROLES = {"Sales Manager", "System Manager"}

# Rubric criteria (Arabic, as Gemini returns them) -> English label and report column.
CRITERIA = [
	("الافتتاحية", "Opening", "opening"),
	("فهم الاحتياج", "Needs Discovery", "needs"),
	("العرض", "Offer", "offer"),
	("الاعتراضات", "Objections", "objections"),
	("الإغلاق", "Closing", "closing"),
	("الأسلوب", "Style", "style"),
]

LIST_FIELDS = [
	"name",
	"type",
	"start_time",
	"duration",
	"reference_doctype",
	"reference_docname",
	"custom_ai_marketer",
	"custom_ai_summary",
	"custom_ai_service",
	"custom_ai_interested",
	"custom_ai_success",
	"custom_ai_overall_score",
]

DETAIL_FIELDS = [
	*LIST_FIELDS,
	"recording_url",
	"custom_ai_customer_type",
	"custom_ai_next_step",
	"custom_ai_next_step_date",
	"custom_ai_customer_profile",
	"custom_ai_not_interested_check",
	"custom_ai_coaching",
	"custom_ai_scores",
	"custom_ai_objections",
	"custom_ai_transcript",
]


def is_manager(user=None):
	return bool(MANAGER_ROLES & set(frappe.get_roles(user)))


def parse_json(value):
	if not value:
		return []
	if isinstance(value, list | dict):
		return value
	try:
		return json.loads(value)
	except (TypeError, ValueError):
		return []


def analysed_calls(marketer=None, from_date=None, to_date=None, call_type=None, limit=None, fields=None):
	filters = {"telephony_medium": "Grandstream", "custom_ai_status": "Done"}
	if marketer:
		filters["custom_ai_marketer"] = marketer
	if call_type:
		filters["type"] = call_type
	if from_date and to_date:
		filters["start_time"] = ["between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"]]
	elif from_date:
		filters["start_time"] = [">=", f"{from_date} 00:00:00"]
	elif to_date:
		filters["start_time"] = ["<=", f"{to_date} 23:59:59"]
	return frappe.get_all(
		"CRM Call Log",
		filters=filters,
		fields=fields or LIST_FIELDS,
		order_by="start_time desc",
		limit=limit or 0,
	)
