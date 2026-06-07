// Copyright (c) 2026, Bonito Designs Pvt. Ltd. and contributors
// For license information, please see license.txt

frappe.query_reports["PO to Payment Tracker Item Level"] = {
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
			fieldname: "item_code",
			label: __("Item Code"),
			fieldtype: "Link",
			options: "Item"
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

		if (column.fieldname === "item_status") {
			if (data && data.item_status === "Fully Invoiced & Received") {
				value = `<span style="color: green; font-weight: bold">${value}</span>`;
			} else if (data && data.item_status === "Fully Received - Not Invoiced") {
				value = `<span style="color: #7b68ee; font-weight: bold">${value}</span>`;
			} else if (data && data.item_status === "Partially Received") {
				value = `<span style="color: orange; font-weight: bold">${value}</span>`;
			} else if (data && data.item_status === "Partially Invoiced") {
				value = `<span style="color: orange; font-weight: bold">${value}</span>`;
			} else if (data && data.item_status === "Not Received") {
				value = `<span style="color: gray; font-weight: bold">${value}</span>`;
			} else if (data && data.item_status === "Over Received") {
				value = `<span style="color: red; font-weight: bold">${value}</span>`;
			} else if (data && data.item_status === "Over Invoiced") {
				value = `<span style="color: red; font-weight: bold">${value}</span>`;
			}
		}

		if (column.fieldname === "rate_match") {
			if (data && data.rate_match === "Mismatch") {
				value = `<span style="color: red; font-weight: bold">${value}</span>`;
			} else if (data && data.rate_match === "Match") {
				value = `<span style="color: green">${value}</span>`;
			}
		}

		return value;
	}
};
