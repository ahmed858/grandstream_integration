"""
Custom fields for the AI call analysis.

Run once on production from `bench --site <site> console` (no `bench migrate` needed):
    from grandstream_integration.ai.setup import install
    install()
It is idempotent and also runs on every `bench migrate` (after_migrate hook).
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from grandstream_integration.ai.prompts import DEFAULT_COMPANY_CONTEXT
from grandstream_integration.ai.schemas import CUSTOMER_TYPES, INTEREST, SERVICES

# Roles that can read the scores, transcript and coaching (permlevel 1) on every call.
# Marketers will see their own calls through a dedicated page (phase 2).
MANAGER_ROLES = ("Sales Manager", "System Manager")


def _select(*options):
	return "\n" + "\n".join(options)


CALL_LOG_FIELDS = [
	{
		"fieldname": "custom_ai_section",
		"fieldtype": "Section Break",
		"label": "AI Call Analysis",
		"insert_after": "note",
		"collapsible": 1,
	},
	# permlevel 0: visible to anyone who can open the call (neutral information)
	{
		"fieldname": "custom_ai_status",
		"fieldtype": "Select",
		"label": "AI Status",
		"read_only": 1,
		"options": _select("Pending", "Processing", "Done", "Failed", "Skipped"),
		"insert_after": "custom_ai_section",
		"in_standard_filter": 1,
	},
	{
		"fieldname": "custom_ai_summary",
		"fieldtype": "Small Text",
		"label": "AI Summary",
		"read_only": 1,
		"insert_after": "custom_ai_status",
	},
	{
		"fieldname": "custom_ai_service",
		"fieldtype": "Select",
		"label": "AI Service",
		"read_only": 1,
		"options": _select(*SERVICES),
		"insert_after": "custom_ai_summary",
		"in_standard_filter": 1,
	},
	{
		"fieldname": "custom_ai_customer_type",
		"fieldtype": "Select",
		"label": "AI Customer Type",
		"read_only": 1,
		"options": _select(*CUSTOMER_TYPES),
		"insert_after": "custom_ai_service",
	},
	{
		"fieldname": "custom_ai_interested",
		"fieldtype": "Select",
		"label": "AI Customer Interest",
		"read_only": 1,
		"options": _select(*INTEREST),
		"insert_after": "custom_ai_customer_type",
		"in_standard_filter": 1,
	},
	{"fieldname": "custom_ai_col_1", "fieldtype": "Column Break", "insert_after": "custom_ai_interested"},
	{
		"fieldname": "custom_ai_marketer",
		"fieldtype": "Link",
		"options": "User",
		"label": "AI Marketer",
		"read_only": 1,
		"insert_after": "custom_ai_col_1",
		"in_standard_filter": 1,
	},
	{
		"fieldname": "custom_ai_next_step",
		"fieldtype": "Data",
		"label": "AI Next Step",
		"read_only": 1,
		"insert_after": "custom_ai_marketer",
	},
	{
		"fieldname": "custom_ai_next_step_date",
		"fieldtype": "Date",
		"label": "AI Next Step Date",
		"read_only": 1,
		"insert_after": "custom_ai_next_step",
	},
	{
		"fieldname": "custom_ai_model",
		"fieldtype": "Data",
		"label": "AI Model",
		"read_only": 1,
		"insert_after": "custom_ai_next_step_date",
	},
	{
		"fieldname": "custom_ai_analyzed_on",
		"fieldtype": "Datetime",
		"label": "AI Analyzed On",
		"read_only": 1,
		"insert_after": "custom_ai_model",
	},
	{
		"fieldname": "custom_ai_attempts",
		"fieldtype": "Int",
		"label": "AI Attempts",
		"read_only": 1,
		"insert_after": "custom_ai_analyzed_on",
	},
	{
		"fieldname": "custom_ai_error",
		"fieldtype": "Small Text",
		"label": "AI Error",
		"read_only": 1,
		"insert_after": "custom_ai_attempts",
	},
	# permlevel 1: the evaluation itself, managers only
	{
		"fieldname": "custom_ai_review_section",
		"fieldtype": "Section Break",
		"label": "AI Evaluation",
		"insert_after": "custom_ai_error",
		"collapsible": 1,
		"permlevel": 1,
	},
	{
		"fieldname": "custom_ai_success",
		"fieldtype": "Check",
		"label": "AI Call Successful",
		"read_only": 1,
		"insert_after": "custom_ai_review_section",
		"permlevel": 1,
	},
	{
		"fieldname": "custom_ai_overall_score",
		"fieldtype": "Float",
		"label": "AI Overall Score (of 5)",
		"precision": "2",
		"read_only": 1,
		"insert_after": "custom_ai_success",
		"permlevel": 1,
	},
	{
		"fieldname": "custom_ai_customer_profile",
		"fieldtype": "Small Text",
		"label": "AI Customer Profile",
		"read_only": 1,
		"insert_after": "custom_ai_overall_score",
		"permlevel": 1,
	},
	{
		"fieldname": "custom_ai_not_interested_check",
		"fieldtype": "Small Text",
		"label": "AI Not Interested Check",
		"read_only": 1,
		"insert_after": "custom_ai_customer_profile",
		"permlevel": 1,
	},
	{
		"fieldname": "custom_ai_coaching",
		"fieldtype": "Small Text",
		"label": "AI Coaching",
		"read_only": 1,
		"insert_after": "custom_ai_not_interested_check",
		"permlevel": 1,
	},
	{
		"fieldname": "custom_ai_scores",
		"fieldtype": "JSON",
		"label": "AI Scores",
		"read_only": 1,
		"insert_after": "custom_ai_coaching",
		"permlevel": 1,
	},
	{
		"fieldname": "custom_ai_objections",
		"fieldtype": "JSON",
		"label": "AI Objections",
		"read_only": 1,
		"insert_after": "custom_ai_scores",
		"permlevel": 1,
	},
	{
		"fieldname": "custom_ai_transcript",
		"fieldtype": "Long Text",
		"label": "AI Transcript",
		"read_only": 1,
		"insert_after": "custom_ai_objections",
		"permlevel": 1,
	},
]

# Grandstream Settings is submittable, so every field must be editable after submit.
SETTINGS_FIELDS = [
	{
		"fieldname": "ai_section",
		"fieldtype": "Section Break",
		"label": "AI Call Analysis",
		"insert_after": "last_inbound_calls_sync",
	},
	{
		"fieldname": "ai_enabled",
		"fieldtype": "Check",
		"label": "AI Enabled",
		"default": "0",
		"description": "Analyse new completed calls automatically. Untick to stop it at any time.",
		"insert_after": "ai_section",
	},
	{
		"fieldname": "gemini_api_key",
		"fieldtype": "Password",
		"label": "Gemini API Key",
		"insert_after": "ai_enabled",
	},
	{
		"fieldname": "gemini_model",
		"fieldtype": "Data",
		"label": "Gemini Model",
		"default": "gemini-3.5-flash",
		"insert_after": "gemini_api_key",
	},
	{
		"fieldname": "ai_min_duration",
		"fieldtype": "Int",
		"label": "Minimum Call Duration (seconds)",
		"default": "20",
		"insert_after": "gemini_model",
	},
	{"fieldname": "ai_col_1", "fieldtype": "Column Break", "insert_after": "ai_min_duration"},
	{
		"fieldname": "ai_add_note",
		"fieldtype": "Check",
		"label": "Add Summary Note to Lead/Deal",
		"default": "1",
		"insert_after": "ai_col_1",
	},
	{
		"fieldname": "agentops_api_key",
		"fieldtype": "Password",
		"label": "AgentOps API Key (optional)",
		"insert_after": "ai_add_note",
	},
	{
		"fieldname": "company_context",
		"fieldtype": "Long Text",
		"label": "Company Context for AI",
		"description": "What the companies sell and to whom. Add common objections and forbidden phrases here.",
		"insert_after": "agentops_api_key",
	},
]
for _field in SETTINGS_FIELDS:
	_field["allow_on_submit"] = 1


def install():
	create_custom_fields(
		{"CRM Call Log": CALL_LOG_FIELDS, "Grandstream Settings": SETTINGS_FIELDS}, update=True
	)
	_grant_evaluation_access()
	_fill_default_context()
	frappe.db.commit()
	print("AI call analysis fields are ready.")


def _grant_evaluation_access():
	from frappe.permissions import add_permission

	for role in MANAGER_ROLES:
		if frappe.db.exists("Role", role):
			add_permission("CRM Call Log", role, permlevel=1)


def _fill_default_context():
	"""Put the default company description in empty settings records, so it can be edited from the UI."""
	for name in frappe.get_all("Grandstream Settings", filters={"docstatus": ["<", 2]}, pluck="name"):
		if not frappe.db.get_value("Grandstream Settings", name, "company_context"):
			frappe.db.set_value(
				"Grandstream Settings",
				name,
				"company_context",
				DEFAULT_COMPANY_CONTEXT,
				update_modified=False,
			)
