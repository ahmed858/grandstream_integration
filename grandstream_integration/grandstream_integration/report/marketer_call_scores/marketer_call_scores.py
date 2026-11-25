"""
Weekly (or any date range) scorecard per marketer: number of analysed calls,
average overall score, success rate, and the average of each rubric criterion.
Restricted to managers through the report's roles.
"""

import frappe
from frappe import _

from grandstream_integration.ai.reporting import CRITERIA, analysed_calls, is_manager, parse_json


def execute(filters=None):
	filters = frappe._dict(filters or {})
	if not is_manager():
		frappe.throw(_("Not permitted"), frappe.PermissionError)

	if not filters.get("from_date") or not filters.get("to_date"):
		filters.to_date = frappe.utils.nowdate()
		filters.from_date = frappe.utils.add_days(filters.to_date, -7)

	fields = [
		"custom_ai_marketer",
		"type",
		"custom_ai_success",
		"custom_ai_overall_score",
		"custom_ai_interested",
		"custom_ai_scores",
	]
	calls = analysed_calls(
		marketer=filters.get("marketer"),
		from_date=filters.from_date,
		to_date=filters.to_date,
		call_type=filters.get("call_type"),
		fields=fields,
	)

	agg = {}
	for c in calls:
		m = c.custom_ai_marketer or "—"
		a = agg.setdefault(
			m,
			{
				"calls": 0,
				"success": 0,
				"interested": 0,
				"score": [],
				"crit": {k: [] for _ar, _en, k in CRITERIA},
			},
		)
		a["calls"] += 1
		a["success"] += 1 if c.custom_ai_success else 0
		a["interested"] += 1 if c.custom_ai_interested == "مهتم" else 0
		if c.custom_ai_overall_score:
			a["score"].append(c.custom_ai_overall_score)
		by_name = {s.get("criterion"): s.get("score") for s in parse_json(c.custom_ai_scores)}
		for ar, _en, key in CRITERIA:
			if isinstance(by_name.get(ar), int | float):
				a["crit"][key].append(by_name[ar])

	data = []
	for marketer, a in sorted(agg.items(), key=lambda kv: _mean(kv[1]["score"]) or 0, reverse=True):
		row = {
			"marketer": marketer if marketer != "—" else None,
			"calls": a["calls"],
			"avg_score": _mean(a["score"]),
			"success_rate": round(100 * a["success"] / a["calls"]) if a["calls"] else 0,
			"interested_rate": round(100 * a["interested"] / a["calls"]) if a["calls"] else 0,
		}
		for _ar, _en, key in CRITERIA:
			row[key] = _mean(a["crit"][key])
		data.append(row)

	return _columns(), data


def _mean(values):
	return round(sum(values) / len(values), 2) if values else None


def _columns():
	cols = [
		{
			"fieldname": "marketer",
			"label": _("Marketer"),
			"fieldtype": "Link",
			"options": "User",
			"width": 200,
		},
		{"fieldname": "calls", "label": _("Calls"), "fieldtype": "Int", "width": 70},
		{
			"fieldname": "avg_score",
			"label": _("Avg Score"),
			"fieldtype": "Float",
			"precision": "2",
			"width": 100,
		},
		{"fieldname": "success_rate", "label": _("Success %"), "fieldtype": "Percent", "width": 100},
		{"fieldname": "interested_rate", "label": _("Interested %"), "fieldtype": "Percent", "width": 110},
	]
	for _ar, en, key in CRITERIA:
		cols.append({"fieldname": key, "label": _(en), "fieldtype": "Float", "precision": "2", "width": 110})
	return cols
