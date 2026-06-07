// Copyright (c) 2026, Bonito Designs Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["PO to Payment Tracker"] = {
	filters: [
		{
			fieldname: "fiscal_year",
			label: __("Fiscal Year"),
			fieldtype: "Link",
			options: "Fiscal Year",
			default: frappe.defaults.get_user_default("fiscal_year"),
			reqd: 1
		},
		{
			fieldname: "supplier",
			label: __("Supplier"),
			fieldtype: "Link",
			options: "Supplier"
		},
		{
			fieldname: "company",
			label: __("Company"),
			fieldtype: "Link",
			options: "Company",
			default: frappe.defaults.get_user_default("Company"),
			reqd: 1
		}
	],

	formatter: function(value, row, column, data, default_formatter) {
		value = default_formatter(value, row, column, data);

		if (column.fieldname === "status") {
			if (data && data.status === "Fully Paid") {
				value = `<span style="color: green; font-weight: bold">${value}</span>`;
			} else if (data && data.status === "Partially Paid") {
				value = `<span style="color: orange; font-weight: bold">${value}</span>`;
			} else if (data && data.status === "Invoiced - Unpaid") {
				value = `<span style="color: red; font-weight: bold">${value}</span>`;
			} else if (data && data.status === "Received - Not Invoiced") {
				value = `<span style="color: #7b68ee; font-weight: bold">${value}</span>`;
			} else if (data && data.status === "Ordered - Not Received") {
				value = `<span style="color: gray; font-weight: bold">${value}</span>`;
			}
		}

		return value;
	}
};

