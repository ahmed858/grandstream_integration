frappe.query_reports["Marketer Call Scores"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: frappe.datetime.add_days(frappe.datetime.get_today(), -7),
			reqd: 1,
		},
		{
			fieldname: "to_date",
			label: __("To Date"),
			fieldtype: "Date",
			default: frappe.datetime.get_today(),
			reqd: 1,
		},
		{
			fieldname: "marketer",
			label: __("Marketer"),
			fieldtype: "Link",
			options: "User",
		},
		{
			fieldname: "call_type",
			label: __("Call Type"),
			fieldtype: "Select",
			options: ["", "Incoming", "Outgoing"],
		},
	],
};
