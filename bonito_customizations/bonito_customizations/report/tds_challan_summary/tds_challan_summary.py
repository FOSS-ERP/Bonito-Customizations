# -*- coding: utf-8 -*-
# Copyright (c) 2026, Bonito Designs Pvt Ltd and contributors
# For license information, please see license.txt

"""
TDS Challan Summary — Script Report

Shows deductee-wise breakdown of TDS deductions mapped to challans,
formatted for 26Q return filing reference. Includes both Purchase Invoices
and Journal Entries.

Updated to work with section-based challans (tds_section instead of
tax_withholding_category at the challan level).
"""

from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.utils import flt


def execute(filters=None):
    columns = get_columns()
    data = get_data(filters)
    return columns, data


def get_columns():
    return [
        {
            "label": _("Challan"),
            "fieldname": "challan",
            "fieldtype": "Link",
            "options": "TDS Challan",
            "width": 160,
        },
        {
            "label": _("Challan No."),
            "fieldname": "challan_number",
            "fieldtype": "Data",
            "width": 120,
        },
        {
            "label": _("TDS Section"),
            "fieldname": "tds_section_code",
            "fieldtype": "Data",
            "width": 100,
        },
        {
            "label": _("BSR Code"),
            "fieldname": "bsr_code",
            "fieldtype": "Data",
            "width": 100,
        },
        {
            "label": _("Deposit Date"),
            "fieldname": "date_of_deposit",
            "fieldtype": "Date",
            "width": 110,
        },
        {
            "label": _("Voucher Type"),
            "fieldname": "voucher_type",
            "fieldtype": "Data",
            "width": 130,
        },
        {
            "label": _("Voucher No"),
            "fieldname": "voucher_no",
            "fieldtype": "Data",
            "width": 180,
        },
        {
            "label": _("Supplier"),
            "fieldname": "supplier_name",
            "fieldtype": "Data",
            "width": 200,
        },
        {
            "label": _("Supplier PAN"),
            "fieldname": "supplier_pan",
            "fieldtype": "Data",
            "width": 120,
        },
        {
            "label": _("Posting Date"),
            "fieldname": "posting_date",
            "fieldtype": "Date",
            "width": 110,
        },
        {
            "label": _("Supplier Invoice No."),
            "fieldname": "bill_no",
            "fieldtype": "Data",
            "width": 140,
        },
        {
            "label": _("Taxable Amount"),
            "fieldname": "invoice_net_total",
            "fieldtype": "Currency",
            "width": 130,
        },
        {
            "label": _("TDS Amount"),
            "fieldname": "tds_amount",
            "fieldtype": "Currency",
            "width": 120,
        },
        {
            "label": _("Allocated Amount"),
            "fieldname": "allocated_amount",
            "fieldtype": "Currency",
            "width": 130,
        },
        {
            "label": _("Withholding Category"),
            "fieldname": "tax_withholding_category",
            "fieldtype": "Data",
            "width": 180,
        },
        {
            "label": _("TDS Account"),
            "fieldname": "tds_account",
            "fieldtype": "Data",
            "width": 200,
        },
        {
            "label": _("Quarter"),
            "fieldname": "quarter",
            "fieldtype": "Data",
            "width": 120,
        },
        {
            "label": _("Status"),
            "fieldname": "status",
            "fieldtype": "Data",
            "width": 120,
        },
    ]


def get_data(filters):
    conditions = ["tc.docstatus = 1"]
    values = {}

    if filters.get("company"):
        conditions.append("tc.company = %(company)s")
        values["company"] = filters["company"]

    if filters.get("financial_year"):
        conditions.append("tc.financial_year = %(financial_year)s")
        values["financial_year"] = filters["financial_year"]

    if filters.get("quarter"):
        conditions.append("tc.quarter = %(quarter)s")
        values["quarter"] = filters["quarter"]

    if filters.get("tds_section"):
        conditions.append("tc.tds_section_code = %(tds_section)s")
        values["tds_section"] = filters["tds_section"]

    if filters.get("supplier"):
        conditions.append("tce.supplier = %(supplier)s")
        values["supplier"] = filters["supplier"]

    where_clause = " AND ".join(conditions)

    linked_data = frappe.db.sql(
        """
        SELECT
            tc.name AS challan,
            tc.challan_number,
            tc.tds_section_code,
            tc.bsr_code,
            tc.date_of_deposit,
            tc.quarter,
            tc.status,
            tce.voucher_type,
            tce.voucher_no,
            tce.supplier_name,
            tce.supplier_pan,
            tce.posting_date,
            tce.bill_no,
            tce.invoice_net_total,
            tce.tds_amount,
            tce.allocated_amount,
            tce.tax_withholding_category,
            tce.tds_account
        FROM `tabTDS Challan Entry` tce
        INNER JOIN `tabTDS Challan` tc ON tc.name = tce.parent
        WHERE {where_clause}
        ORDER BY tc.date_of_deposit, tc.name, tce.posting_date
    """.format(
            where_clause=where_clause
        ),
        values,
        as_dict=True,
    )

    if filters.get("show_unlinked"):
        unlinked_data = get_unlinked_vouchers(filters)
        linked_data.extend(unlinked_data)

    return linked_data


def get_unlinked_vouchers(filters):
    """Find Purchase Invoices with TDS that are NOT linked to any challan."""
    conditions = ["pi.docstatus = 1", "pi.apply_tds = 1"]
    values = {}

    if filters.get("company"):
        conditions.append("pi.company = %(company)s")
        values["company"] = filters["company"]

    if filters.get("financial_year"):
        fy = filters["financial_year"]
        fy_dates = frappe.db.get_value(
            "Fiscal Year", fy, ["year_start_date", "year_end_date"], as_dict=True
        )
        if fy_dates:
            conditions.append("pi.posting_date >= %(fy_start)s")
            conditions.append("pi.posting_date <= %(fy_end)s")
            values["fy_start"] = fy_dates.year_start_date
            values["fy_end"] = fy_dates.year_end_date

    if filters.get("tds_section"):
        from bonito_customizations.bonito_customizations.doctype.tds_challan.tds_section_mapping import (
            classify_category,
        )
        all_cats = frappe.db.get_all(
            "Tax Withholding Category", fields=["name"]
        )
        matching_cats = [
            c.name for c in all_cats
            if classify_category(c.name) == filters["tds_section"]
        ]
        if matching_cats:
            conditions.append(
                "pi.tax_withholding_category IN %(matching_cats)s"
            )
            values["matching_cats"] = matching_cats
        else:
            return []

    if filters.get("supplier"):
        conditions.append("pi.supplier = %(supplier)s")
        values["supplier"] = filters["supplier"]

    where_clause = " AND ".join(conditions)

    rows = frappe.db.sql(
        """
        SELECT
            '' AS challan,
            'NOT LINKED' AS challan_number,
            '' AS tds_section_code,
            '' AS bsr_code,
            NULL AS date_of_deposit,
            '' AS quarter,
            'Unlinked' AS status,
            'Purchase Invoice' AS voucher_type,
            pi.name AS voucher_no,
            pi.supplier_name,
            CASE
                WHEN LENGTH(pi.tax_id) = 15 THEN SUBSTRING(pi.tax_id, 3, 10)
                ELSE pi.tax_id
            END AS supplier_pan,
            pi.posting_date,
            pi.bill_no,
            pi.net_total AS invoice_net_total,
            pi.tax_withholding_category,
            '' AS tds_account,
            (
                SELECT SUM(gl.credit - gl.debit)
                FROM `tabGL Entry` gl
                WHERE gl.voucher_no = pi.name
                  AND gl.voucher_type = 'Purchase Invoice'
                  AND gl.is_cancelled = 0
                  AND gl.account LIKE '%%TDS%%'
                  AND gl.credit > gl.debit
            ) AS tds_amount,
            0 AS allocated_amount
        FROM `tabPurchase Invoice` pi
        WHERE {where_clause}
          AND (pi.custom_tds_challan IS NULL OR pi.custom_tds_challan = '')
          AND pi.name NOT IN (
              SELECT tce.voucher_no
              FROM `tabTDS Challan Entry` tce
              INNER JOIN `tabTDS Challan` tc ON tc.name = tce.parent
              WHERE tc.docstatus = 1
                AND tce.voucher_type = 'Purchase Invoice'
          )
        ORDER BY pi.posting_date
    """.format(
            where_clause=where_clause
        ),
        values,
        as_dict=True,
    )
    return rows

