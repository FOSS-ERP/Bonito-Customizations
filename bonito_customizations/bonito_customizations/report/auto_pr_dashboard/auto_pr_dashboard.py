"""
Auto Purchase Receipt Dashboard — Script Report

Shows Purchase Receipt creation stats by department (from linked PO),
split into OCR (created by Lambda) vs Manual.

Place in: bonito_customizations/bonito_customizations/report/auto_pr_dashboard/
"""

import frappe
from frappe import _


# The ERPNext user account used by the Lambda auto-PR function.
# PRs created by this user are counted as "Through OCR".
AUTO_PR_USER = "lambda-auto-pr@bonito.in"


def execute(filters=None):
	filters = filters or {}
	columns = get_columns(filters)
	data = get_data(filters)
	chart = get_chart(data, filters)
	summary = get_summary(data)
	return columns, data, None, chart, summary


def get_columns(filters):
	cols = [
		{
			"fieldname": "department",
			"label": _("Department"),
			"fieldtype": "Data",
			"width": 180,
		},
	]

	if filters.get("show_supplier"):
		cols.append({
			"fieldname": "supplier",
			"label": _("Supplier"),
			"fieldtype": "Link",
			"options": "Supplier",
			"width": 220,
		})

	cols.extend([
		{
			"fieldname": "total_receipts",
			"label": _("Total Receipts"),
			"fieldtype": "Int",
			"width": 130,
		},
		{
			"fieldname": "through_ocr",
			"label": _("Through OCR"),
			"fieldtype": "Int",
			"width": 120,
		},
		{
			"fieldname": "not_through_ocr",
			"label": _("Not Through OCR"),
			"fieldtype": "Int",
			"width": 140,
		},
		{
			"fieldname": "ocr_pct",
			"label": _("% Through OCR"),
			"fieldtype": "Percent",
			"width": 130,
		},
	])

	return cols


def get_data(filters):
	from_date = filters.get("from_date", "2026-04-01")
	to_date = filters.get("to_date", frappe.utils.today())
	show_supplier = filters.get("show_supplier")
	department_filter = filters.get("department")

	conditions = [
		"pr.docstatus IN (0, 1)",
		"pr.posting_date >= %(from_date)s",
		"pr.posting_date <= %(to_date)s",
	]
	values = {
		"from_date": from_date,
		"to_date": to_date,
		"auto_pr_user": AUTO_PR_USER,
	}

	if department_filter:
		conditions.append("IFNULL(po.main_department, 'Uncategorized') = %(department)s")
		values["department"] = department_filter

	where_clause = " AND ".join(conditions)

	if show_supplier:
		group_by = "department, pr.supplier"
		supplier_col = "pr.supplier AS supplier,"
	else:
		group_by = "department"
		supplier_col = ""

	query = """
		SELECT
			IFNULL(po.main_department, 'Uncategorized') AS department,
			{supplier_col}
			COUNT(DISTINCT pr.name) AS total_receipts,
			COUNT(DISTINCT CASE WHEN pr.owner = %(auto_pr_user)s THEN pr.name END) AS through_ocr,
			COUNT(DISTINCT CASE WHEN pr.owner != %(auto_pr_user)s THEN pr.name END) AS not_through_ocr
		FROM
			`tabPurchase Receipt` pr
		LEFT JOIN
			`tabPurchase Receipt Item` pri ON pri.parent = pr.name
		LEFT JOIN
			`tabPurchase Order` po ON po.name = pri.purchase_order
		WHERE
			{where_clause}
		GROUP BY
			{group_by}
		ORDER BY
			department, total_receipts DESC
	""".format(
		supplier_col=supplier_col,
		where_clause=where_clause,
		group_by=group_by,
	)

	rows = frappe.db.sql(query, values, as_dict=True)

	# Compute percentage
	for row in rows:
		total = row.get("total_receipts") or 0
		ocr = row.get("through_ocr") or 0
		row["ocr_pct"] = (ocr / total * 100) if total > 0 else 0

	# If not showing supplier, add a totals row
	if not show_supplier and rows:
		total_all = sum(r["total_receipts"] for r in rows)
		ocr_all = sum(r["through_ocr"] for r in rows)
		manual_all = sum(r["not_through_ocr"] for r in rows)
		rows.append({
			"department": "Total",
			"total_receipts": total_all,
			"through_ocr": ocr_all,
			"not_through_ocr": manual_all,
			"ocr_pct": (ocr_all / total_all * 100) if total_all > 0 else 0,
			"bold": 1,
		})

	return rows


def get_chart(data, filters):
	if filters.get("show_supplier"):
		return None

	# Exclude the totals row from the chart
	chart_data = [r for r in data if r.get("department") != "Total"]
	if not chart_data:
		return None

	return {
		"data": {
			"labels": [r["department"] for r in chart_data],
			"datasets": [
				{
					"name": _("Through OCR"),
					"values": [r["through_ocr"] for r in chart_data],
				},
				{
					"name": _("Not Through OCR"),
					"values": [r["not_through_ocr"] for r in chart_data],
				},
			],
		},
		"type": "bar",
		"colors": ["#2490ef", "#f8814f"],
		"barOptions": {"stacked": 1},
	}


def get_summary(data):
	if not data:
		return []

	# Use totals row if it exists, otherwise compute
	totals_row = next((r for r in data if r.get("department") == "Total"), None)
	if totals_row:
		total = totals_row["total_receipts"]
		ocr = totals_row["through_ocr"]
		manual = totals_row["not_through_ocr"]
		pct = totals_row["ocr_pct"]
	else:
		total = sum(r.get("total_receipts", 0) for r in data)
		ocr = sum(r.get("through_ocr", 0) for r in data)
		manual = sum(r.get("not_through_ocr", 0) for r in data)
		pct = (ocr / total * 100) if total > 0 else 0

	return [
		{"value": total, "label": _("Total Receipts"), "datatype": "Int"},
		{"value": ocr, "label": _("Through OCR"), "datatype": "Int", "indicator": "green"},
		{"value": manual, "label": _("Manual"), "datatype": "Int", "indicator": "orange"},
		{"value": round(pct, 1), "label": _("OCR %"), "datatype": "Percent", "indicator": "blue"},
	]
