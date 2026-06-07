# Copyright (c) 2026, Bonito Designs Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _

PO_BATCH_SIZE = 500


def execute(filters=None):
	if not filters or not filters.get("fiscal_year") or not filters.get("company"):
		return get_columns(), []

	columns = get_columns()
	data = get_data(filters)
	return columns, data


def get_columns():
	return [
		{
			"label": _("Supplier"),
			"fieldname": "supplier",
			"fieldtype": "Link",
			"options": "Supplier",
			"width": 180,
		},
		{
			"label": _("Vendor Type"),
			"fieldname": "vendor_type",
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"label": _("Supplier Active/Inactive"),
			"fieldname": "supplier_active_inactive",
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"label": _("Supplier Type"),
			"fieldname": "supplier_type",
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"label": _("Department"),
			"fieldname": "department",
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"label": _("Purchase Order"),
			"fieldname": "purchase_order",
			"fieldtype": "Link",
			"options": "Purchase Order",
			"width": 160,
		},
		{
			"label": _("PO Date"),
			"fieldname": "po_date",
			"fieldtype": "Date",
			"width": 100,
		},
		{
			"label": _("PO Qty"),
			"fieldname": "po_qty",
			"fieldtype": "Data",
			"width": 80,
		},
		{
			"label": _("PO Grand Total"),
			"fieldname": "po_grand_total",
			"fieldtype": "Currency",
			"width": 130,
		},
		{
			"label": _("Purchase Receipt(s)"),
			"fieldname": "purchase_receipts",
			"fieldtype": "Data",
			"width": 200,
		},
		{
			"label": _("PRE Date"),
			"fieldname": "pre_date",
			"fieldtype": "Data",
			"width": 120,
		},
		{
			"label": _("PRE Total Qty"),
			"fieldname": "pre_total_qty",
			"fieldtype": "Data",
			"width": 100,
		},
		{
			"label": _("Purchase Invoice"),
			"fieldname": "purchase_invoice",
			"fieldtype": "Link",
			"options": "Purchase Invoice",
			"width": 180,
		},
		{
			"label": _("PINV Date"),
			"fieldname": "pinv_date",
			"fieldtype": "Date",
			"width": 100,
		},
		{
			"label": _("Supplier Invoice No"),
			"fieldname": "supplier_invoice_no",
			"fieldtype": "Data",
			"width": 150,
		},
		{
			"label": _("Supplier Invoice Date"),
			"fieldname": "supplier_invoice_date",
			"fieldtype": "Date",
			"width": 130,
		},
		{
			"label": _("TDS Section"),
			"fieldname": "tds_section",
			"fieldtype": "Data",
			"width": 150,
		},
		{
			"label": _("TDS Amount"),
			"fieldname": "tds_amount",
			"fieldtype": "Currency",
			"width": 110,
		},
		{
			"label": _("CGST"),
			"fieldname": "cgst",
			"fieldtype": "Currency",
			"width": 100,
		},
		{
			"label": _("SGST"),
			"fieldname": "sgst",
			"fieldtype": "Currency",
			"width": 100,
		},
		{
			"label": _("IGST"),
			"fieldname": "igst",
			"fieldtype": "Currency",
			"width": 100,
		},
		{
			"label": _("GST Paid"),
			"fieldname": "gst_paid",
			"fieldtype": "Currency",
			"width": 110,
		},
		{
			"label": _("PINV Grand Total"),
			"fieldname": "pinv_grand_total",
			"fieldtype": "Currency",
			"width": 140,
		},
		{
			"label": _("Net Total"),
			"fieldname": "net_total",
			"fieldtype": "Data",
			"width": 100,
		},
		{
			"label": _("Payment Date"),
			"fieldname": "payment_date",
			"fieldtype": "Date",
			"width": 120,
		},
		{
			"label": _("Payment Entry(s)"),
			"fieldname": "payment_entries",
			"fieldtype": "Data",
			"width": 200,
		},
		{
			"label": _("Total Paid"),
			"fieldname": "total_paid",
			"fieldtype": "Currency",
			"width": 130,
		},
		{
			"label": _("Outstanding"),
			"fieldname": "outstanding",
			"fieldtype": "Currency",
			"width": 130,
		},
		{
			"label": _("Status"),
			"fieldname": "status",
			"fieldtype": "Data",
			"width": 170,
		},
	]


def _fetch_supplier_map(supplier_names):
	supplier_map = {}
	if not supplier_names:
		return supplier_map

	supplier_records = frappe.get_all(
		"Supplier",
		filters={"name": ["in", supplier_names]},
		fields=["name", "disabled", "supplier_type"],
	)
	for s in supplier_records:
		supplier_map[s.name] = {
			"disabled": s.disabled,
			"supplier_type": s.supplier_type or "",
			"vendor_type": "",
			"department": "",
		}

	for custom_field in ["vendor_type", "department"]:
		try:
			custom_data = frappe.get_all(
				"Supplier",
				filters={"name": ["in", supplier_names]},
				fields=["name", custom_field],
			)
			for row in custom_data:
				if row.name in supplier_map:
					val = row.get(custom_field)
					supplier_map[row.name][custom_field] = val if val else ""
		except Exception:
			pass

	return supplier_map


def _fetch_batch_data(po_names):
	"""Fetch all downstream documents for a batch of PO names."""

	# Purchase Receipt Items -> get PRE names per PO
	pre_data = frappe.get_all(
		"Purchase Receipt Item",
		filters={"purchase_order": ["in", po_names], "docstatus": 1},
		fields=["parent", "purchase_order"],
	)
	po_pre_map = {}
	all_pre_names = set()
	for row in pre_data:
		if row.purchase_order not in po_pre_map:
			po_pre_map[row.purchase_order] = set()
		po_pre_map[row.purchase_order].add(row.parent)
		all_pre_names.add(row.parent)

	# Purchase Receipt dates (bulk fetch for all PREs in this batch)
	pre_date_map = {}
	if all_pre_names:
		pre_docs = frappe.get_all(
			"Purchase Receipt",
			filters={"name": ["in", list(all_pre_names)], "docstatus": 1},
			fields=["name", "posting_date"],
		)
		for doc in pre_docs:
			pre_date_map[doc.name] = doc.posting_date

	# Purchase Invoice Items
	pinv_item_data = frappe.get_all(
		"Purchase Invoice Item",
		filters={"purchase_order": ["in", po_names], "docstatus": 1},
		fields=["parent", "purchase_order"],
	)
	po_pinv_names_map = {}
	all_pinv_names = set()
	for row in pinv_item_data:
		po_pinv_names_map.setdefault(row.purchase_order, set()).add(row.parent)
		all_pinv_names.add(row.parent)

	pinv_details = {}
	pinv_tax_map = {}
	pe_data = {}
	je_data = {}

	if not all_pinv_names:
		return (po_pre_map, pre_date_map, po_pinv_names_map,
				pinv_details, pinv_tax_map, pe_data, je_data)

	pinv_list = list(all_pinv_names)

	# Purchase Invoice details (added posting_date)
	pinv_records = frappe.get_all(
		"Purchase Invoice",
		filters={"name": ["in", pinv_list], "docstatus": 1},
		fields=[
			"name", "grand_total", "outstanding_amount",
			"bill_no", "bill_date", "due_date",
			"posting_date", "tax_withholding_category"
		],
	)
	for pinv in pinv_records:
		pinv_details[pinv.name] = {
			"grand_total": pinv.grand_total,
			"outstanding": pinv.outstanding_amount,
			"bill_no": pinv.bill_no or "",
			"bill_date": pinv.bill_date,
			"due_date": pinv.due_date,
			"posting_date": pinv.posting_date,
			"tax_withholding_category": pinv.tax_withholding_category or "",
		}

	# Purchase Invoice taxes
	tax_rows = frappe.get_all(
		"Purchase Taxes and Charges",
		filters={"parent": ["in", pinv_list], "docstatus": 1},
		fields=["parent", "account_head", "tax_amount", "add_deduct_tax"],
	)
	for t in tax_rows:
		if t.parent not in pinv_tax_map:
			pinv_tax_map[t.parent] = {
				"cgst": 0, "sgst": 0, "igst": 0,
				"tds_amount": 0, "tds_account": "",
			}
		entry = pinv_tax_map[t.parent]
		head_lower = (t.account_head or "").lower()
		amount = t.tax_amount or 0

		if "cgst" in head_lower:
			entry["cgst"] += amount
		elif "sgst" in head_lower:
			entry["sgst"] += amount
		elif "igst" in head_lower:
			entry["igst"] += amount
		elif "tds" in head_lower or "tax deducted" in head_lower:
			entry["tds_amount"] += amount
			if not entry["tds_account"]:
				entry["tds_account"] = t.account_head

	# Payment Entries
	pe_refs = frappe.get_all(
		"Payment Entry Reference",
		filters={
			"reference_doctype": "Purchase Invoice",
			"reference_name": ["in", pinv_list],
			"docstatus": 1,
		},
		fields=["parent", "reference_name", "allocated_amount"],
	)
	for ref in pe_refs:
		pe_data.setdefault(ref.reference_name, []).append({
			"pe": ref.parent,
			"amount": ref.allocated_amount,
		})

	# Journal Entry payments
	je_refs = frappe.get_all(
		"Journal Entry Account",
		filters={
			"reference_type": "Purchase Invoice",
			"reference_name": ["in", pinv_list],
			"docstatus": 1,
		},
		fields=["parent", "reference_name", "debit_in_account_currency"],
	)
	for ref in je_refs:
		if ref.debit_in_account_currency > 0:
			je_data.setdefault(ref.reference_name, []).append({
				"je": ref.parent,
				"amount": ref.debit_in_account_currency,
			})

	return (po_pre_map, pre_date_map, po_pinv_names_map,
			pinv_details, pinv_tax_map, pe_data, je_data)


def _build_rows(purchase_orders, supplier_map, po_pre_map, pre_date_map,
				po_pinv_names_map, pinv_details, pinv_tax_map, pe_data, je_data):
	"""Build report rows for a batch of POs. One row per PO-PINV pair."""
	rows = []
	for po in purchase_orders:
		sup = supplier_map.get(po.supplier, {})
		pre_names = po_pre_map.get(po.name, set())
		pre_names_sorted = sorted(pre_names)
		pinv_names = po_pinv_names_map.get(po.name, set())
		pinv_names_sorted = sorted(pinv_names)

		# PRE dates: collect posting_dates for all PREs of this PO
		pre_dates = []
		for prn in pre_names_sorted:
			pd = pre_date_map.get(prn)
			if pd:
				date_str = str(pd)
				if date_str not in pre_dates:
					pre_dates.append(date_str)

		common = {
			"supplier": po.supplier,
			"vendor_type": sup.get("vendor_type", ""),
			"supplier_active_inactive": "Inactive" if sup.get("disabled") else "Active",
			"supplier_type": sup.get("supplier_type", ""),
			"department": sup.get("department", ""),
			"purchase_order": po.name,
			"po_date": po.transaction_date,
			"po_qty": "",
			"po_grand_total": po.grand_total,
			"purchase_receipts": ", ".join(pre_names_sorted) if pre_names_sorted else "",
			"pre_date": ", ".join(pre_dates) if pre_dates else "",
			"pre_total_qty": "",
			"net_total": "",
		}

		if not pinv_names_sorted:
			row = dict(common)
			row.update({
				"purchase_invoice": "",
				"pinv_date": None,
				"supplier_invoice_no": "",
				"supplier_invoice_date": None,
				"tds_section": "",
				"tds_amount": 0,
				"cgst": 0,
				"sgst": 0,
				"igst": 0,
				"gst_paid": 0,
				"pinv_grand_total": 0,
				"payment_date": None,
				"payment_entries": "",
				"total_paid": 0,
				"outstanding": 0,
				"status": _get_status(pre_names_sorted, False, 0, 0),
			})
			rows.append(row)
		else:
			for pn in pinv_names_sorted:
				pd = pinv_details.get(pn, {})
				tax_info = pinv_tax_map.get(pn, {})

				cgst = tax_info.get("cgst", 0)
				sgst = tax_info.get("sgst", 0)
				igst = tax_info.get("igst", 0)
				tds_amt = tax_info.get("tds_amount", 0)
				tds_section = pd.get("tax_withholding_category", "") or tax_info.get("tds_account", "")

				pe_set = set()
				pinv_paid = 0
				for entry in pe_data.get(pn, []):
					pe_set.add(entry["pe"])
					pinv_paid += entry["amount"]
				for entry in je_data.get(pn, []):
					pe_set.add(entry["je"])
					pinv_paid += entry["amount"]

				pe_names_sorted = sorted(pe_set)
				pinv_outstanding = pd.get("outstanding", 0)

				row = dict(common)
				row.update({
					"purchase_invoice": pn,
					"pinv_date": pd.get("posting_date"),
					"supplier_invoice_no": pd.get("bill_no", ""),
					"supplier_invoice_date": pd.get("bill_date"),
					"tds_section": tds_section,
					"tds_amount": tds_amt,
					"cgst": cgst,
					"sgst": sgst,
					"igst": igst,
					"gst_paid": cgst + sgst + igst,
					"pinv_grand_total": pd.get("grand_total", 0),
					"payment_date": pd.get("due_date"),
					"payment_entries": ", ".join(pe_names_sorted) if pe_names_sorted else "",
					"total_paid": pinv_paid,
					"outstanding": pinv_outstanding,
					"status": _get_status(pre_names_sorted, True, pinv_paid, pinv_outstanding),
				})
				rows.append(row)

	return rows


def _get_status(pre_names, has_pinv, paid, outstanding):
	if not pre_names and not has_pinv:
		return "Ordered - Not Received"
	if pre_names and not has_pinv:
		return "Received - Not Invoiced"
	if has_pinv and outstanding <= 0 and paid > 0:
		return "Fully Paid"
	if has_pinv and paid > 0 and outstanding > 0:
		return "Partially Paid"
	if has_pinv and paid == 0:
		return "Invoiced - Unpaid"
	return "In Progress"


def get_data(filters):
	fy = frappe.get_doc("Fiscal Year", filters.get("fiscal_year"))
	from_date = fy.year_start_date
	to_date = fy.year_end_date
	company = filters.get("company")
	supplier_filter = filters.get("supplier")

	po_conditions = {
		"docstatus": 1,
		"company": company,
		"transaction_date": ["between", [from_date, to_date]],
	}
	if supplier_filter:
		po_conditions["supplier"] = supplier_filter

	count_result = frappe.get_all(
		"Purchase Order",
		filters=po_conditions,
		fields=["count(name) as total"],
	)
	total_pos = count_result[0].total if count_result else 0
	if not total_pos:
		return []

	all_suppliers = frappe.get_all(
		"Purchase Order",
		filters=po_conditions,
		fields=["distinct supplier as supplier"],
	)
	supplier_names = [s.supplier for s in all_suppliers]
	supplier_map = _fetch_supplier_map(supplier_names)

	data = []
	offset = 0

	while offset < total_pos:
		po_batch = frappe.get_all(
			"Purchase Order",
			filters=po_conditions,
			fields=[
				"name", "supplier", "supplier_name", "transaction_date",
				"grand_total", "status"
			],
			order_by="supplier asc, transaction_date asc, name asc",
			start=offset,
			page_length=PO_BATCH_SIZE,
		)

		if not po_batch:
			break

		po_names = [po.name for po in po_batch]

		(po_pre_map, pre_date_map, po_pinv_names_map, pinv_details,
		 pinv_tax_map, pe_data, je_data) = _fetch_batch_data(po_names)

		batch_rows = _build_rows(
			po_batch, supplier_map, po_pre_map, pre_date_map,
			po_pinv_names_map, pinv_details, pinv_tax_map, pe_data, je_data
		)
		data.extend(batch_rows)

		frappe.db.commit()
		offset += PO_BATCH_SIZE

	return data

