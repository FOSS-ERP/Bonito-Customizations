# -*- coding: utf-8 -*-
# Copyright (c) 2026, Bonito Designs Pvt Ltd and contributors
# For license information, please see license.txt

"""
Server-side hooks for TDS Challan integrity.

Register these in your app's hooks.py:

doc_events = {
    "Purchase Invoice": {
        "before_cancel": "bonito_customizations.bonito_customizations.doctype.tds_challan.tds_challan_hooks.prevent_cancel_if_linked_to_challan"
    },
    "Journal Entry": {
        "before_cancel": "bonito_customizations.bonito_customizations.doctype.tds_challan.tds_challan_hooks.prevent_cancel_if_linked_to_challan"
    }
}
"""

from __future__ import unicode_literals

import frappe
from frappe import _
from frappe.utils import flt, cstr


def prevent_cancel_if_linked_to_challan(doc, method):
    """
    Prevent cancellation of Purchase Invoices or Journal Entries that are
    linked to a submitted TDS Challan.

    This fires on before_cancel. Since ERPNext amendment also triggers
    cancellation of the original, this covers both cancel and amend flows.

    The user's recourse:
    - For Purchase Invoices: create a Debit Note (return) instead of cancelling
    - For Journal Entries: create a reverse Journal Entry
    - If they absolutely must cancel: first cancel the TDS Challan, then
      cancel the voucher, then re-create the challan without it
    """
    challan = getattr(doc, "custom_tds_challan", None)

    if not challan:
        return

    # Verify the challan is still submitted (not cancelled itself)
    challan_docstatus = frappe.db.get_value("TDS Challan", challan, "docstatus")

    if challan_docstatus == 1:
        frappe.throw(
            _(
                "Cannot cancel {0} <b>{1}</b> because it is linked to submitted "
                "TDS Challan <b><a href='/app/tds-challan/{2}'>{2}</a></b>.<br><br>"
                "To proceed, either:<br>"
                "1. Cancel the TDS Challan first, then cancel this document, or<br>"
                "2. Create a {3} instead of cancelling."
            ).format(
                doc.doctype,
                doc.name,
                challan,
                "Debit Note (Purchase Return)" if doc.doctype == "Purchase Invoice" else "Reverse Journal Entry",
            ),
            title=_("Linked to TDS Challan"),
        )


def validate_challan_tds_amounts_on_submit(doc, method):
    """
    Optional additional hook: call this on TDS Challan's before_submit
    to verify that each entry's tds_amount still matches the current GL.

    This catches the case where a voucher was amended (after unlinking)
    and re-linked with a stale TDS amount.

    To use, add to the TDSChallan class's before_submit:
        validate_challan_tds_amounts_on_submit(self, None)

    Or register via hooks on TDS Challan's before_submit.
    """
    mismatches = []

    for row in doc.entries:
        # Get current TDS from GL
        current_tds = frappe.db.sql(
            """
            SELECT SUM(credit - debit) AS tds_amount
            FROM `tabGL Entry`
            WHERE account = %(account)s
              AND voucher_type = %(vtype)s
              AND voucher_no = %(vno)s
              AND is_cancelled = 0
        """,
            {
                "account": doc.tds_account_head,
                "vtype": row.voucher_type,
                "vno": row.voucher_no,
            },
            as_dict=True,
        )

        actual_tds = flt(current_tds[0].tds_amount, 2) if current_tds else 0

        if actual_tds != flt(row.tds_amount, 2):
            mismatches.append(
                _("Row {0}: {1} {2} — Challan has ₹{3} but GL shows ₹{4}").format(
                    row.idx,
                    row.voucher_type,
                    row.voucher_no,
                    flt(row.tds_amount, 2),
                    actual_tds,
                )
            )

    if mismatches:
        frappe.throw(
            _(
                "TDS amounts in the following entries do not match current GL records. "
                "Please re-fetch or correct before submitting:<br><br>{0}"
            ).format("<br>".join(mismatches)),
            title=_("TDS Amount Mismatch"),
        )
