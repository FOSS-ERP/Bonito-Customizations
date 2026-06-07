# -*- coding: utf-8 -*-
# Copyright (c) 2026, Bonito Designs Pvt Ltd and contributors
# For license information, please see license.txt

"""
TDS Compliance Summary — Script Report

For a given period, shows:
  - TDS Deducted (credits to TDS payable accounts from PIs/JEs)
  - TDS Paid to Government (debits to TDS payable accounts from PEs/JEs)
  - TDS Outstanding (balance = deducted - paid)

Grouped by TDS section (194C, 194J, etc.) using the centralized
section-mapping module. This ensures ALL accounts — both from Tax
Withholding Categories and direct TDS GL accounts — are included.
"""

from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.utils import flt, getdate
from collections import OrderedDict


def execute(filters=None):
    validate_filters(filters)
    columns = get_columns(filters)
    data = get_data(filters)
    chart = get_chart(data)
    summary = get_report_summary(data)
    return columns, data, None, chart, summary


def validate_filters(filters):
    if not filters.get("company"):
        frappe.throw(_("Company is required"))
    if not filters.get("financial_year"):
        frappe.throw(_("Financial Year is required"))


def get_columns(filters):
    columns = [
        {
            "label": _("TDS Section"),
            "fieldname": "tds_section",
            "fieldtype": "Data",
            "width": 160,
        },
        {
            "label": _("TDS Account"),
            "fieldname": "account",
            "fieldtype": "Link",
            "options": "Account",
            "width": 280,
        },
        {
            "label": _("Source"),
            "fieldname": "source",
            "fieldtype": "Data",
            "width": 130,
            "description": "Whether from Tax Withholding Category or direct GL account",
        },
    ]

    if filters.get("group_by") == "Monthly":
        months = get_months_in_period(filters)
        for month_label, month_key in months:
            columns.append({
                "label": month_label,
                "fieldname": "deducted_" + month_key,
                "fieldtype": "Currency",
                "width": 120,
            })

    columns.extend([
        {
            "label": _("Opening Balance"),
            "fieldname": "opening_balance",
            "fieldtype": "Currency",
            "width": 140,
        },
        {
            "label": _("TDS Deducted"),
            "fieldname": "total_deducted",
            "fieldtype": "Currency",
            "width": 140,
        },
        {
            "label": _("TDS Paid"),
            "fieldname": "total_paid",
            "fieldtype": "Currency",
            "width": 140,
        },
        {
            "label": _("Outstanding"),
            "fieldname": "outstanding",
            "fieldtype": "Currency",
            "width": 140,
        },
        {
            "label": _("# Deductions"),
            "fieldname": "deduction_count",
            "fieldtype": "Int",
            "width": 110,
        },
        {
            "label": _("# Payments"),
            "fieldname": "payment_count",
            "fieldtype": "Int",
            "width": 110,
        },
    ])

    return columns


def get_data(filters):
    company = filters.get("company")
    fy = filters.get("financial_year")

    fy_dates = frappe.db.get_value(
        "Fiscal Year", fy, ["year_start_date", "year_end_date"], as_dict=True
    )
    if not fy_dates:
        frappe.throw(_("Fiscal Year {0} not found").format(fy))

    from_date = fy_dates.year_start_date
    to_date = fy_dates.year_end_date

    if filters.get("quarter"):
        from_date, to_date = get_quarter_dates(fy_dates, filters.get("quarter"))

    # -----------------------------------------------------------------------
    # Get ALL TDS accounts using section-mapping module
    # -----------------------------------------------------------------------
    tds_accounts = get_all_tds_accounts(company, filters.get("tds_section"))

    if not tds_accounts:
        frappe.msgprint(
            _("No TDS accounts found for {0}").format(company), indicator="orange"
        )
        return []

    account_list = list(tds_accounts.keys())

    # -----------------------------------------------------------------------
    # Opening balance
    # -----------------------------------------------------------------------
    opening_data = frappe.db.sql(
        """
        SELECT
            gl.account,
            SUM(gl.credit - gl.debit) AS balance
        FROM `tabGL Entry` gl
        WHERE gl.account IN %(accounts)s
            AND gl.company = %(company)s
            AND gl.posting_date < %(from_date)s
            AND gl.is_cancelled = 0
        GROUP BY gl.account
    """,
        {
            "accounts": account_list,
            "company": company,
            "from_date": from_date,
        },
        as_dict=True,
    )
    opening_map = {row.account: flt(row.balance, 2) for row in opening_data}

    # -----------------------------------------------------------------------
    # Period GL entries
    # -----------------------------------------------------------------------
    deductions = frappe.db.sql(
        """
        SELECT
            gl.account,
            gl.posting_date,
            gl.voucher_type,
            gl.voucher_no,
            SUM(gl.credit) AS amount
        FROM `tabGL Entry` gl
        WHERE gl.account IN %(accounts)s
            AND gl.company = %(company)s
            AND gl.posting_date BETWEEN %(from_date)s AND %(to_date)s
            AND gl.is_cancelled = 0
            AND gl.credit > 0
        GROUP BY gl.account, gl.posting_date, gl.voucher_type, gl.voucher_no
        ORDER BY gl.account, gl.posting_date
    """,
        {
            "accounts": account_list,
            "company": company,
            "from_date": from_date,
            "to_date": to_date,
        },
        as_dict=True,
    )

    payments = frappe.db.sql(
        """
        SELECT
            gl.account,
            gl.posting_date,
            gl.voucher_type,
            gl.voucher_no,
            SUM(gl.debit) AS amount
        FROM `tabGL Entry` gl
        WHERE gl.account IN %(accounts)s
            AND gl.company = %(company)s
            AND gl.posting_date BETWEEN %(from_date)s AND %(to_date)s
            AND gl.is_cancelled = 0
            AND gl.debit > 0
        GROUP BY gl.account, gl.posting_date, gl.voucher_type, gl.voucher_no
        ORDER BY gl.account, gl.posting_date
    """,
        {
            "accounts": account_list,
            "company": company,
            "from_date": from_date,
            "to_date": to_date,
        },
        as_dict=True,
    )

    # -----------------------------------------------------------------------
    # Aggregate per account
    # -----------------------------------------------------------------------
    monthly_breakdown = filters.get("group_by") == "Monthly"
    months = get_months_in_period(filters) if monthly_breakdown else []

    result = OrderedDict()

    for account in sorted(account_list):
        info = tds_accounts[account]
        row = {
            "tds_section": info.get("section", ""),
            "account": account,
            "source": info.get("source", ""),
            "opening_balance": flt(opening_map.get(account, 0), 2),
            "total_deducted": 0.0,
            "total_paid": 0.0,
            "outstanding": 0.0,
            "deduction_count": 0,
            "payment_count": 0,
        }

        if monthly_breakdown:
            for _, month_key in months:
                row["deducted_" + month_key] = 0.0

        result[account] = row

    for d in deductions:
        if d.account in result:
            result[d.account]["total_deducted"] += flt(d.amount, 2)
            result[d.account]["deduction_count"] += 1

            if monthly_breakdown:
                month_key = getdate(d.posting_date).strftime("%Y_%m")
                field = "deducted_" + month_key
                if field in result[d.account]:
                    result[d.account][field] += flt(d.amount, 2)

    for p in payments:
        if p.account in result:
            result[p.account]["total_paid"] += flt(p.amount, 2)
            result[p.account]["payment_count"] += 1

    for account, row in result.items():
        row["total_deducted"] = flt(row["total_deducted"], 2)
        row["total_paid"] = flt(row["total_paid"], 2)
        row["outstanding"] = flt(
            row["opening_balance"] + row["total_deducted"] - row["total_paid"], 2
        )

    data = list(result.values())

    # Sort by section then account
    data.sort(key=lambda r: (r.get("tds_section", ""), r.get("account", "")))

    # Add totals row
    if data:
        totals = {
            "tds_section": "<b>Total</b>",
            "account": "",
            "source": "",
            "opening_balance": sum(r["opening_balance"] for r in data),
            "total_deducted": sum(r["total_deducted"] for r in data),
            "total_paid": sum(r["total_paid"] for r in data),
            "outstanding": sum(r["outstanding"] for r in data),
            "deduction_count": sum(r["deduction_count"] for r in data),
            "payment_count": sum(r["payment_count"] for r in data),
        }
        if monthly_breakdown:
            for _, month_key in months:
                field = "deducted_" + month_key
                totals[field] = sum(r.get(field, 0) for r in data)

        data.append(totals)

    return data


def get_all_tds_accounts(company, tds_section_filter=None):
    """Get all TDS accounts for the company, combining:
    1. Accounts from Tax Withholding Categories (mapped to sections)
    2. Direct TDS GL accounts from Chart of Accounts

    Returns: {account_name: {"section": "194C", "source": "Category/Direct"}}
    """
    from bonito_customizations.bonito_customizations.doctype.tds_challan.tds_section_mapping import (
        classify_category,
        classify_account,
    )

    result = {}

    # Source 1: Tax Withholding Categories
    rows = frappe.db.sql(
        """
        SELECT twa.account, twc.name AS category
        FROM `tabTax Withholding Account` twa
        INNER JOIN `tabTax Withholding Category` twc ON twc.name = twa.parent
        WHERE twa.company = %(company)s
        ORDER BY twa.account
    """,
        {"company": company},
        as_dict=True,
    )

    for row in rows:
        section = classify_category(row.category)
        if not section:
            continue
        if tds_section_filter and section != tds_section_filter:
            continue
        if row.account not in result:
            result[row.account] = {
                "section": section,
                "source": "Withholding Category",
                "categories": [],
            }
        result[row.account]["categories"].append(row.category)

    # Source 2: Direct TDS GL accounts
    tds_gl_accounts = frappe.db.get_all(
        "Account",
        filters={
            "company": company,
            "root_type": "Liability",
            "is_group": 0,
            "name": ("like", "%TDS%"),
        },
        fields=["name", "account_name"],
    )

    for acc in tds_gl_accounts:
        section = classify_account(acc.account_name)
        if not section:
            continue
        if tds_section_filter and section != tds_section_filter:
            continue
        if acc.name not in result:
            result[acc.name] = {
                "section": section,
                "source": "Direct GL Account",
            }
        # If already added from category, mark as "Both"
        elif result[acc.name]["source"] == "Withholding Category":
            result[acc.name]["source"] = "Both"

    return result


def get_quarter_dates(fy_dates, quarter):
    import datetime
    start_year = getdate(fy_dates.year_start_date).year
    end_year = getdate(fy_dates.year_end_date).year

    quarter_map = {
        "Q1 (Apr-Jun)": (datetime.date(start_year, 4, 1), datetime.date(start_year, 6, 30)),
        "Q2 (Jul-Sep)": (datetime.date(start_year, 7, 1), datetime.date(start_year, 9, 30)),
        "Q3 (Oct-Dec)": (datetime.date(start_year, 10, 1), datetime.date(start_year, 12, 31)),
        "Q4 (Jan-Mar)": (datetime.date(end_year, 1, 1), datetime.date(end_year, 3, 31)),
    }

    if quarter in quarter_map:
        return quarter_map[quarter]

    return fy_dates.year_start_date, fy_dates.year_end_date


def get_months_in_period(filters):
    fy = filters.get("financial_year")
    fy_dates = frappe.db.get_value(
        "Fiscal Year", fy, ["year_start_date", "year_end_date"], as_dict=True
    )
    if not fy_dates:
        return []

    from_date = getdate(fy_dates.year_start_date)
    to_date = getdate(fy_dates.year_end_date)

    if filters.get("quarter"):
        from_date, to_date = get_quarter_dates(fy_dates, filters.get("quarter"))
        from_date = getdate(from_date)
        to_date = getdate(to_date)

    months = []
    current = from_date.replace(day=1)
    while current <= to_date:
        label = current.strftime("%b %Y")
        key = current.strftime("%Y_%m")
        months.append((label, key))
        if current.month == 12:
            current = current.replace(year=current.year + 1, month=1)
        else:
            current = current.replace(month=current.month + 1)

    return months


def get_chart(data):
    if not data:
        return None

    chart_data = [r for r in data if not str(r.get("tds_section", "")).startswith("<b>")]

    if not chart_data:
        return None

    labels = [r["tds_section"] + " / " + r["account"] if r["account"] else r["tds_section"]
              for r in chart_data]

    return {
        "data": {
            "labels": labels,
            "datasets": [
                {
                    "name": _("TDS Deducted"),
                    "values": [r["total_deducted"] for r in chart_data],
                },
                {
                    "name": _("TDS Paid"),
                    "values": [r["total_paid"] for r in chart_data],
                },
                {
                    "name": _("Outstanding"),
                    "values": [r["outstanding"] for r in chart_data],
                },
            ],
        },
        "type": "bar",
        "colors": ["#7cd6fd", "#5e64ff", "#ff5858"],
    }


def get_report_summary(data):
    if not data:
        return None

    totals = data[-1] if data else {}

    return [
        {
            "value": flt(totals.get("opening_balance", 0)),
            "label": _("Opening Balance"),
            "datatype": "Currency",
            "indicator": "blue",
        },
        {
            "value": flt(totals.get("total_deducted", 0)),
            "label": _("Total Deducted"),
            "datatype": "Currency",
            "indicator": "green",
        },
        {
            "value": flt(totals.get("total_paid", 0)),
            "label": _("Total Paid"),
            "datatype": "Currency",
            "indicator": "blue",
        },
        {
            "value": flt(totals.get("outstanding", 0)),
            "label": _("Outstanding"),
            "datatype": "Currency",
            "indicator": "orange" if flt(totals.get("outstanding", 0)) > 0 else "green",
        },
    ]

