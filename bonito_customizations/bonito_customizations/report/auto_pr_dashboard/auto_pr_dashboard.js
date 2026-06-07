frappe.query_reports["Auto PR Dashboard"] = {
	filters: [
		{
			fieldname: "from_date",
			label: __("From Date"),
			fieldtype: "Date",
			default: "2026-04-01",
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
			fieldname: "department",
			label: __("Department"),
			fieldtype: "Link",
			options: "Department",
		},
		{
			fieldname: "show_supplier",
			label: __("Show Supplier Breakdown"),
			fieldtype: "Check",
			default: 0,
		},
	],

	formatter: function (value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		// Bold the totals row
		if (data && data.department === "Total") {
			value = "<b>" + value + "</b>";
		}

		// Color-code OCR percentage
		if (column.fieldname === "ocr_pct" && data) {
			if (data.ocr_pct >= 60) {
				value = "<span style='color: var(--green-500)'>" + value + "</span>";
			} else if (data.ocr_pct >= 30) {
				value = "<span style='color: var(--orange-500)'>" + value + "</span>";
			} else {
				value = "<span style='color: var(--red-500)'>" + value + "</span>";
			}
		}

		return value;
	},
};
