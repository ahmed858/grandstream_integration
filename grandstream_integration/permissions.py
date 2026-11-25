"""Who can see which CRM Call Log.

Frappe CRM gives every Sales User read access to every call log. Marketers should only
see their own calls, so these hooks narrow that down; managers keep seeing everything.
Frappe applies them on top of the role permissions, so they can only take access away.
"""

import frappe

# Roles that keep seeing every call log.
SEE_ALL_ROLES = {"System Manager", "Sales Manager"}

TABLE = "`tabCRM Call Log`"

# Call log fields that hold a user: made the call, answered it, or created the log.
USER_FIELDS = ("caller", "receiver", "owner")


def can_see_all_calls(user: str) -> bool:
	return user == "Administrator" or bool(SEE_ALL_ROLES.intersection(frappe.get_roles(user)))


def user_extensions(user: str) -> list[str]:
	"""PBX extensions mapped to the user. Synced calls store the extension in from/to."""
	extensions = frappe.get_all("User Extension", filters={"user": user}, pluck="extension")
	return [str(ext).strip() for ext in extensions if ext and str(ext).strip()]


def get_call_log_permission_query_conditions(user=None, doctype=None):
	"""List filter: own calls only, matched by user fields or by the user's extension."""
	user = user or frappe.session.user
	if can_see_all_calls(user):
		return ""

	me = frappe.db.escape(user)
	conditions = [f"{TABLE}.`{field}` = {me}" for field in USER_FIELDS]

	extensions = user_extensions(user)
	if extensions:
		values = ", ".join(frappe.db.escape(ext) for ext in extensions)
		conditions += [f"{TABLE}.`from` in ({values})", f"{TABLE}.`to` in ({values})"]

	return "(" + " or ".join(conditions) + ")"


def has_call_log_permission(doc, ptype=None, user=None, debug=False):
	"""Single call check (opening a call, playing its recording): same rule as the list filter."""
	user = user or frappe.session.user
	if ptype == "create" or not doc.get("name") or can_see_all_calls(user):
		return True

	if user in {doc.get(field) for field in USER_FIELDS}:
		return True

	extensions = set(user_extensions(user))
	numbers = {str(doc.get("from") or "").strip(), str(doc.get("to") or "").strip()}
	return bool(extensions & numbers)
