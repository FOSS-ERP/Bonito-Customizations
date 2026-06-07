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
			"label": _("Purchase Order"),
			"fieldname": "purchase_order",
			"fieldtype": "Link",
			"options": "Purchase Order",
			"width": 150,
		},
		{
			"label": _("PO Date"),
			"fieldname": "po_date",
			"fieldtype": "Date",
			"width": 100,
		},
		{
			"label": _("Project"),
			"fieldname": "project",
			"fieldtype": "Link",
			"options": "Project",
			"width": 150,
		},
		{
			"label": _("Item Code"),
			"fieldname": "item_code",
			"fieldtype": "Link",
			"options": "Item",
			"width": 150,
		},
		{
			"label": _("Item Name"),
			"fieldname": "item_name",
			"fieldtype": "Data",
			"width": 180,
		},
		{
			"label": _("UOM"),
			"fieldname": "uom",
			"fieldtype": "Data",
			"width": 70,
		},
		# -- PO --
		{
			"label": _("PO Qty"),
			"fieldname": "po_qty",
			"fieldtype": "Float",
			"width": 90,
		},
		{
			"label": _("PO Rate"),
			"fieldname": "po_rate",
			"fieldtype": "Currency",
			"width": 110,
		},
		{
			"label": _("PO Amount"),
			"fieldname": "po_amount",
			"fieldtype": "Currency",
			"width": 120,
		},
		# -- PRE --
		{
			"label": _("Received Qty"),
			"fieldname": "received_qty",
			"fieldtype": "Float",
			"width": 100,
		},
		{
			"label": _("PRE Rate (Avg)"),
			"fieldname": "pre_rate",
			"fieldtype": "Currency",
			"width": 110,
		},
		{
			"label": _("PRE Amount"),
			"fieldname": "pre_amount",
			"fieldtype": "Currency",
			"width": 120,
		},
		{
			"label": _("Purchase Receipt(s)"),
			"fieldname": "purchase_receipts",
			"fieldtype": "Data",
			"width": 180,
		},
		{
			"label": _("PRE Date"),
			"fieldname": "pre_date",
			"fieldtype": "Data",
			"width": 120,
		},
		# -- PINV --
		{
			"label": _("Invoiced Qty"),
			"fieldname": "invoiced_qty",
			"fieldtype": "Float",
			"width": 100,
		},
		{
			"label": _("PINV Rate (Avg)"),
			"fieldname": "pinv_rate",
			"fieldtype": "Currency",
			"width": 110,
		},
		{
			"label": _("PINV Amount"),
			"fieldname": "pinv_amount",
			"fieldtype": "Currency",
			"width": 120,
		},
		{
			"label": _("Purchase Invoice(s)"),
			"fieldname": "purchase_invoices",
			"fieldtype": "Data",
			"width": 180,
		},
		{
			"label": _("PINV Date"),
			"fieldname": "pinv_date",
			"fieldtype": "Data",
			"width": 120,
		},
		# -- Gaps --
		{
			"label": _("Pending Receipt Qty"),
			"fieldname": "pending_receipt_qty",
			"fieldtype": "Float",
			"width": 120,
		},
		{
			"label": _("Pending Invoice Qty"),
			"fieldname": "pending_invoice_qty",
			"fieldtype": "Float",
			"width": 120,
		},
		{
			"label": _("Rate Match"),
			"fieldname": "rate_match",
			"fieldtype": "Data",
			"width": 100,
		},
		{
			"label": _("Status"),
			"fieldname": "item_status",
			"fieldtype": "Data",
			"width": 200,
		},
	]


# ======================================================================
# Supplier cache — fetch vendor_type (with graceful fallback)
# ======================================================================

def _fetch_supplier_map(supplier_names):
	supplier_map = {}
	if not supplier_names:
		return supplier_map

	# Base fields
	records = frappe.get_all(
		"Supplier",
		filters={"name": ["in", supplier_names]},
		fields=["name"],
	)
	for s in records:
		supplier_map[s.name] = {"vendor_type": ""}

	# Try fetching vendor_type (custom field, may not exist on staging)
	try:
		vt_data = frappe.get_all(
			"Supplier",
			filters={"name": ["in", supplier_names]},
			fields=["name", "vendor_type"],
		)
		for row in vt_data:
			if row.name in supplier_map:
				supplier_map[row.name]["vendor_type"] = row.get("vendor_type") or ""
	except Exception:
		pass

	return supplier_map


# ======================================================================
# Batch data fetcher
# ======================================================================

def _fetch_batch_data(po_item_names):
	"""Given a list of PO Item detail names, fetch linked
	PRE items and PINV items. Returns pre_map, pinv_map, pre_date_map, pinv_date_map."""

	# Purchase Receipt Items (uses purchase_order_item to link to PO Item)
	pre_items = frappe.get_all(
		"Purchase Receipt Item",
		filters={
			"purchase_order_item": ["in", po_item_names],
			"docstatus": 1,
		},
		fields=["parent", "purchase_order_item", "qty", "rate", "amount"],
	)

	pre_map = {}
	all_pre_names = set()
	for row in pre_items:
		key = row.purchase_order_item
		if key not in pre_map:
			pre_map[key] = {"qty": 0, "amount": 0, "names": set()}
		entry = pre_map[key]
		entry["qty"] += row.qty
		entry["amount"] += (row.amount or 0)
		entry["names"].add(row.parent)
		all_pre_names.add(row.parent)

	# PRE posting dates (bulk)
	pre_date_map = {}
	if all_pre_names:
		pre_docs = frappe.get_all(
			"Purchase Receipt",
			filters={"name": ["in", list(all_pre_names)], "docstatus": 1},
			fields=["name", "posting_date"],
		)
		for doc in pre_docs:
			pre_date_map[doc.name] = doc.posting_date

	# Purchase Invoice Items (uses po_detail to link to PO Item)
	pinv_items = frappe.get_all(
		"Purchase Invoice Item",
		filters={
			"po_detail": ["in", po_item_names],
			"docstatus": 1,
		},
		fields=["parent", "po_detail", "qty", "rate", "amount"],
	)

	pinv_map = {}
	all_pinv_names = set()
	for row in pinv_items:
		if row.po_detail not in pinv_map:
			pinv_map[row.po_detail] = {"qty": 0, "amount": 0, "names": set()}
		entry = pinv_map[row.po_detail]
		entry["qty"] += row.qty
		entry["amount"] += (row.amount or 0)
		entry["names"].add(row.parent)
		all_pinv_names.add(row.parent)

	# PINV posting dates (bulk)
	pinv_date_map = {}
	if all_pinv_names:
		pinv_docs = frappe.get_all(
			"Purchase Invoice",
			filters={"name": ["in", list(all_pinv_names)], "docstatus": 1},
			fields=["name", "posting_date"],
		)
		for doc in pinv_docs:
			pinv_date_map[doc.name] = doc.posting_date

	return pre_map, pinv_map, pre_date_map, pinv_date_map


# ======================================================================
# Row builder
# ======================================================================

def _build_rows(po_items_batch, po_header_map, supplier_map, pre_map, pinv_map,
				pre_date_map, pinv_date_map):
	rows = []
	for poi in po_items_batch:
		po = po_header_map.get(poi.parent, {})
		supplier = po.get("supplier", "")
		sup = supplier_map.get(supplier, {})

		po_qty = poi.qty or 0
		po_rate = poi.rate or 0
		po_amount = poi.amount or 0

		# PRE aggregates
		pre = pre_map.get(poi.name, {})
		received_qty = pre.get("qty", 0)
		pre_amount = pre.get("amount", 0)
		pre_rate = (pre_amount / received_qty) if received_qty else 0
		pre_names = sorted(pre.get("names", set()))

		# PRE dates
		pre_dates = []
		for prn in pre_names:
			d = pre_date_map.get(prn)
			if d:
				ds = str(d)
				if ds not in pre_dates:
					pre_dates.append(ds)

		# PINV aggregates
		pinv = pinv_map.get(poi.name, {})
		invoiced_qty = pinv.get("qty", 0)
		pinv_amount = pinv.get("amount", 0)
		pinv_rate = (pinv_amount / invoiced_qty) if invoiced_qty else 0
		pinv_names = sorted(pinv.get("names", set()))

		# PINV dates
		pinv_dates = []
		for pn in pinv_names:
			d = pinv_date_map.get(pn)
			if d:
				ds = str(d)
				if ds not in pinv_dates:
					pinv_dates.append(ds)

		# Gaps
		pending_receipt = po_qty - received_qty
		pending_invoice = po_qty - invoiced_qty

		# Rate match
		rate_match = ""
		if invoiced_qty > 0:
			if abs(po_rate - pinv_rate) < 0.01:
				rate_match = "Match"
			else:
				rate_match = "Mismatch"

		item_status = _get_item_status(po_qty, received_qty, invoiced_qty)

		rows.append({
			"supplier": supplier,
			"vendor_type": sup.get("vendor_type", ""),
			"purchase_order": poi.parent,
			"po_date": po.get("transaction_date"),
			"project": poi.get("project") or "",
			"item_code": poi.item_code,
			"item_name": poi.item_name,
			"uom": poi.uom,
			"po_qty": po_qty,
			"po_rate": po_rate,
			"po_amount": po_amount,
			"received_qty": received_qty,
			"pre_rate": pre_rate,
			"pre_amount": pre_amount,
			"purchase_receipts": ", ".join(pre_names) if pre_names else "",
			"pre_date": ", ".join(pre_dates) if pre_dates else "",
			"invoiced_qty": invoiced_qty,
			"pinv_rate": pinv_rate,
			"pinv_amount": pinv_amount,
			"purchase_invoices": ", ".join(pinv_names) if pinv_names else "",
			"pinv_date": ", ".join(pinv_dates) if pinv_dates else "",
			"pending_receipt_qty": pending_receipt,
			"pending_invoice_qty": pending_invoice,
			"rate_match": rate_match,
			"item_status": item_status,
		})

	return rows


def _get_item_status(po_qty, received_qty, invoiced_qty):
	if po_qty <= 0:
		return ""

	if received_qty >= po_qty and invoiced_qty >= po_qty:
		return "Fully Invoiced & Received"

	if received_qty > po_qty:
		return "Over Received"

	if invoiced_qty > po_qty:
		return "Over Invoiced"

	if received_qty >= po_qty and invoiced_qty < po_qty:
		return "Fully Received - Not Invoiced"

	if received_qty > 0 and received_qty < po_qty:
		return "Partially Received"

	if invoiced_qty > 0 and invoiced_qty < po_qty:
		return "Partially Invoiced"

	return "Not Received"


# ======================================================================
# Main data function with batched pagination
# ======================================================================

def get_data(filters):
	fy = frappe.get_doc("Fiscal Year", filters.get("fiscal_year"))
	from_date = fy.year_start_date
	to_date = fy.year_end_date
	company = filters.get("company")
	supplier_filter = filters.get("supplier")
	item_filter = filters.get("item_code")

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

	# Supplier cache
	all_suppliers = frappe.get_all(
		"Purchase Order",
		filters=po_conditions,
		fields=["distinct supplier as supplier"],
	)
	supplier_names = [s.supplier for s in all_suppliers]
	supplier_map = _fetch_supplier_map(supplier_names)

	# PO Item filter
	po_item_extra_filters = {}
	if item_filter:
		po_item_extra_filters["item_code"] = item_filter

	data = []
	offset = 0

	while offset < total_pos:
		po_batch = frappe.get_all(
			"Purchase Order",
			filters=po_conditions,
			fields=["name", "supplier", "supplier_name", "transaction_date"],
			order_by="supplier asc, transaction_date asc, name asc",
			start=offset,
			page_length=PO_BATCH_SIZE,
		)

		if not po_batch:
			break

		po_names = [po.name for po in po_batch]

		# PO header lookup (includes project)
		po_header_map = {}
		for po in po_batch:
			po_header_map[po.name] = {
				"supplier": po.supplier,
				"supplier_name": po.supplier_name,
				"transaction_date": po.transaction_date,
			}

		# PO Items
		poi_filters = {
			"parent": ["in", po_names],
			"docstatus": 1,
		}
		poi_filters.update(po_item_extra_filters)

		po_items_batch = frappe.get_all(
			"Purchase Order Item",
			filters=poi_filters,
			fields=[
				"name", "parent", "item_code", "item_name",
				"qty", "rate", "amount", "uom", "project"
			],
			order_by="parent asc, idx asc",
		)

		if po_items_batch:
			po_item_names = [poi.name for poi in po_items_batch]
			pre_map, pinv_map, pre_date_map, pinv_date_map = _fetch_batch_data(po_item_names)

			batch_rows = _build_rows(
				po_items_batch, po_header_map, supplier_map,
				pre_map, pinv_map, pre_date_map, pinv_date_map
			)
			data.extend(batch_rows)

		frappe.db.commit()
		offset += PO_BATCH_SIZE

	return data
