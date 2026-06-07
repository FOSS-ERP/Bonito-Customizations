# -*- coding: utf-8 -*-
# Copyright (c) 2026, Bonito Designs Pvt Ltd and contributors
# For license information, please see license.txt

from __future__ import unicode_literals

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import flt, getdate, cstr


class TDSChallan(Document):
    def validate(self):
        self.validate_dates()
        self.validate_duplicate_vouchers()
        self.validate_allocated_amounts()
        self.calculate_totals()
        self.set_assessment_year()
        self.set_section_code()
        self.set_status()

    def before_submit(self):
        if not self.entries:
            frappe.throw(_("Cannot submit a TDS Challan without any linked vouchers"))
        self.validate_mandatory_on_submit()
        self.verify_tds_amounts_against_gl()

    def validate_mandatory_on_submit(self):
        """These fields are not reqd on save (to allow draft creation before
        challan details are available) but must be filled before submit."""
        mandatory_fields = {
            "challan_number": _("Challan Number"),
            "bsr_code": _("BSR Code"),
            "date_of_deposit": _("Date of Deposit"),
            "bank_name": _("Bank Name"),
            "payment_ref_type": _("Payment Reference Type"),
            "payment_entry": _("Payment Reference"),
        }

        missing = []
        for fieldname, label in mandatory_fields.items():
            if not self.get(fieldname):
                missing.append(label)

        if missing:
            frappe.throw(
                _("The following fields are required before submitting:<br><br>{0}").format(
                    "<br>".join("• " + f for f in missing)
                ),
                title=_("Missing Challan Details"),
            )

    def on_submit(self):
        self.set_status()
        self.stamp_vouchers()

    def before_cancel(self):
        self.unstamp_vouchers()
        self.ignore_linked_doctypes = ("Purchase Invoice", "Journal Entry")

    def on_cancel(self):
        self.set_status()

    def validate_dates(self):
        if self.period_from and self.period_to:
            if getdate(self.period_from) > getdate(self.period_to):
                frappe.throw(_("Period From cannot be after Period To"))

        if self.date_of_deposit and self.period_from:
            if getdate(self.date_of_deposit) < getdate(self.period_from):
                frappe.msgprint(
                    _("Date of Deposit {0} is before the period start {1}. Please verify.").format(
                        self.date_of_deposit, self.period_from
                    ),
                    indicator="orange",
                    alert=True,
                )

    def validate_duplicate_vouchers(self):
        """Ensure no voucher appears twice in this challan"""
        seen = set()
        for row in self.entries:
            key = (row.voucher_type, row.voucher_no)
            if key in seen:
                frappe.throw(
                    _("{0} {1} is added more than once in row {2}").format(
                        row.voucher_type, row.voucher_no, row.idx
                    )
                )
            seen.add(key)

    def validate_allocated_amounts(self):
        """Allocated amount cannot exceed TDS amount for any row"""
        for row in self.entries:
            if flt(row.allocated_amount) > flt(row.tds_amount):
                frappe.throw(
                    _("Row {0}: Allocated amount ({1}) cannot exceed TDS amount ({2}) for {3}").format(
                        row.idx,
                        frappe.format_value(row.allocated_amount, {"fieldtype": "Currency"}),
                        frappe.format_value(row.tds_amount, {"fieldtype": "Currency"}),
                        row.voucher_no,
                    )
                )
            if flt(row.allocated_amount) <= 0:
                frappe.throw(
                    _("Row {0}: Allocated amount must be greater than zero for {1}").format(
                        row.idx, row.voucher_no
                    )
                )

    def calculate_totals(self):
        self.total_tds_amount = flt(
            sum(flt(row.tds_amount) for row in self.entries), 2
        )
        self.allocated_amount = flt(
            sum(flt(row.allocated_amount) for row in self.entries), 2
        )

        allocatable = flt(self.total_challan_amount) - flt(self.total_interest) - flt(self.total_late_fee)
        self.unallocated_amount = flt(allocatable - flt(self.allocated_amount), 2)

    def set_assessment_year(self):
        """Derive assessment year from financial year."""
        if not self.financial_year:
            return
        fy_name = cstr(self.financial_year)
        fy_start = frappe.db.get_value("Fiscal Year", fy_name, "year_start_date")
        if fy_start:
            start_year = getdate(fy_start).year
            end_year = start_year + 1
            self.assessment_year = "{0}-{1}".format(end_year, str(end_year + 1)[-2:])
        elif "-" in fy_name:
            try:
                parts = fy_name.split("-")
                start = int(parts[0])
                self.assessment_year = "{0}-{1}".format(start + 1, start + 2)
            except (ValueError, IndexError):
                pass

    def set_section_code(self):
        """Auto-derive section code from the tds_section Select value."""
        if self.tds_section:
            from bonito_customizations.bonito_customizations.doctype.tds_challan.tds_section_mapping import (
                extract_section_code,
            )
            self.tds_section_code = extract_section_code(self.tds_section)

    def set_status(self):
        if self.docstatus == 0:
            self.status = "Draft"
        elif self.docstatus == 1:
            if flt(self.unallocated_amount, 2) == 0:
                self.status = "Reconciled"
            else:
                self.status = "Partially Reconciled"
        elif self.docstatus == 2:
            self.status = "Cancelled"

    def _get_account_list(self):
        """Parse the tds_account_head JSON field into a list of account names."""
        if not self.tds_account_head:
            return []
        try:
            accounts = json.loads(self.tds_account_head)
            if isinstance(accounts, list):
                return [a for a in accounts if a]
            # Backward compat: if it's a plain string (old format)
            return [accounts] if accounts else []
        except (json.JSONDecodeError, TypeError):
            # Backward compat: treat as a single account name
            return [self.tds_account_head] if self.tds_account_head else []

    def verify_tds_amounts_against_gl(self):
        """On submit, verify each entry's tds_amount still matches the GL.
        Uses a single bulk query across all accounts for this section."""
        if not self.entries:
            return

        account_list = self._get_account_list()
        if not account_list:
            frappe.throw(_("No TDS accounts configured. Please re-select the TDS Section."))

        voucher_keys = [(row.voucher_type, row.voucher_no) for row in self.entries]

        gl_data = frappe.db.sql(
            """
            SELECT voucher_type, voucher_no, SUM(credit - debit) AS tds_amount
            FROM `tabGL Entry`
            WHERE account IN %(accounts)s
              AND is_cancelled = 0
              AND (voucher_type, voucher_no) IN %(keys)s
            GROUP BY voucher_type, voucher_no
        """,
            {"accounts": account_list, "keys": voucher_keys},
            as_dict=True,
        )

        gl_map = {(r.voucher_type, r.voucher_no): flt(r.tds_amount, 2) for r in gl_data}

        mismatches = []
        for row in self.entries:
            actual_tds = gl_map.get((row.voucher_type, row.voucher_no), 0)
            if actual_tds != flt(row.tds_amount, 2):
                mismatches.append(
                    _("Row {0}: {1} {2} — Challan has {3} but GL shows {4}").format(
                        row.idx, row.voucher_type, row.voucher_no,
                        frappe.format_value(row.tds_amount, {"fieldtype": "Currency"}),
                        frappe.format_value(actual_tds, {"fieldtype": "Currency"}),
                    )
                )

        if mismatches:
            frappe.throw(
                _(
                    "TDS amounts do not match current GL records. "
                    "Please re-fetch or update before submitting:<br><br>{0}"
                ).format("<br>".join(mismatches)),
                title=_("TDS Amount Mismatch"),
            )

    def stamp_vouchers(self):
        """On submit, stamp each linked voucher with this challan reference."""
        pi_names = [row.voucher_no for row in self.entries if row.voucher_type == "Purchase Invoice"]
        je_names = [row.voucher_no for row in self.entries if row.voucher_type == "Journal Entry"]

        if pi_names:
            frappe.db.sql(
                """UPDATE `tabPurchase Invoice`
                   SET custom_tds_challan = %(challan)s
                   WHERE name IN %(names)s""",
                {"challan": self.name, "names": pi_names},
            )

        if je_names:
            frappe.db.sql(
                """UPDATE `tabJournal Entry`
                   SET custom_tds_challan = %(challan)s
                   WHERE name IN %(names)s""",
                {"challan": self.name, "names": je_names},
            )

    def unstamp_vouchers(self):
        """On cancel, clear challan reference."""
        pi_names = [row.voucher_no for row in self.entries if row.voucher_type == "Purchase Invoice"]
        je_names = [row.voucher_no for row in self.entries if row.voucher_type == "Journal Entry"]

        if pi_names:
            frappe.db.sql(
                """UPDATE `tabPurchase Invoice`
                   SET custom_tds_challan = ''
                   WHERE name IN %(names)s
                     AND custom_tds_challan = %(challan)s""",
                {"challan": self.name, "names": pi_names},
            )

        if je_names:
            frappe.db.sql(
                """UPDATE `tabJournal Entry`
                   SET custom_tds_challan = ''
                   WHERE name IN %(names)s
                     AND custom_tds_challan = %(challan)s""",
                {"challan": self.name, "names": je_names},
            )


# ---------------------------------------------------------------------------
# Whitelisted methods
# ---------------------------------------------------------------------------

@frappe.whitelist()
def fetch_tds_vouchers(company, tds_account_head, period_from, period_to,
                       total_challan_amount, total_interest=0, total_late_fee=0,
                       challan_name=None, existing_vouchers=None):
    """
    Fetch Purchase Invoices AND Journal Entries with TDS deductions,
    allocated FIFO up to the allocatable challan amount.

    tds_account_head is now a JSON array of account names (all accounts
    for the selected TDS section). The GL query searches across ALL of them.

    Returns rows for client-side population.
    """
    if not tds_account_head:
        frappe.throw(_("Please set the TDS Section before fetching"))
    if not period_from or not period_to:
        frappe.throw(_("Please set Period From and Period To before fetching"))
    if not company:
        frappe.throw(_("Please set the Company before fetching"))

    # Parse the account list
    if isinstance(tds_account_head, str):
        try:
            account_list = json.loads(tds_account_head)
        except json.JSONDecodeError:
            account_list = [tds_account_head]
    elif isinstance(tds_account_head, list):
        account_list = tds_account_head
    else:
        account_list = [cstr(tds_account_head)]

    account_list = [a for a in account_list if a]
    if not account_list:
        frappe.throw(_("No TDS accounts found for the selected section"))

    total_challan_amount = flt(total_challan_amount)
    if total_challan_amount <= 0:
        frappe.throw(_("Please enter the Total Challan Amount before fetching"))

    allocatable = flt(total_challan_amount) - flt(total_interest) - flt(total_late_fee)
    if allocatable <= 0:
        frappe.throw(_("Allocatable amount (Challan - Interest - Late Fee) must be positive"))

    # Parse existing vouchers already in the form
    current_vouchers = set()
    already_allocated_total = 0.0
    if existing_vouchers:
        if isinstance(existing_vouchers, str):
            existing_vouchers = json.loads(existing_vouchers)
        for v in existing_vouchers:
            current_vouchers.add((v.get("voucher_type", ""), v.get("voucher_no", "")))
            already_allocated_total += flt(v.get("allocated_amount", 0))

    remaining = flt(allocatable - already_allocated_total, 2)
    if remaining <= 0:
        frappe.msgprint(
            _("Challan amount is already fully allocated by existing entries"),
            indicator="orange",
        )
        return {"entries": [], "added": 0, "skipped_linked": 0,
                "skipped_duplicate": 0, "remaining": 0}

    # Get vouchers already linked to OTHER submitted challans
    extra_condition = ""
    params = {}
    if challan_name and not cstr(challan_name).startswith("new-"):
        extra_condition = "AND tc.name != %(challan_name)s"
        params["challan_name"] = challan_name

    already_linked = frappe.db.sql(
        """
        SELECT tce.voucher_type, tce.voucher_no
        FROM `tabTDS Challan Entry` tce
        INNER JOIN `tabTDS Challan` tc ON tc.name = tce.parent
        WHERE tc.docstatus = 1
          {extra_condition}
    """.format(extra_condition=extra_condition),
        params,
        as_dict=True,
    )
    already_linked_set = set(
        (row.voucher_type, row.voucher_no) for row in already_linked
    )

    # -----------------------------------------------------------------------
    # Fetch GL entries for BOTH Purchase Invoices and Journal Entries
    # across ALL accounts for this section.
    # TDS deduction = credit to a TDS payable account
    # -----------------------------------------------------------------------
    gl_entries = frappe.db.sql(
        """
        SELECT
            gl.voucher_type,
            gl.voucher_no,
            gl.posting_date,
            SUM(gl.credit - gl.debit) AS tds_amount,
            gl.account AS tds_account
        FROM `tabGL Entry` gl
        WHERE gl.account IN %(accounts)s
            AND gl.voucher_type IN ('Purchase Invoice', 'Journal Entry')
            AND gl.is_cancelled = 0
            AND gl.posting_date BETWEEN %(from_date)s AND %(to_date)s
            AND gl.company = %(company)s
        GROUP BY gl.voucher_type, gl.voucher_no, gl.posting_date
        HAVING SUM(gl.credit - gl.debit) > 0
        ORDER BY gl.posting_date, gl.voucher_type, gl.voucher_no
    """,
        {
            "accounts": account_list,
            "from_date": period_from,
            "to_date": period_to,
            "company": company,
        },
        as_dict=True,
    )

    if not gl_entries:
        frappe.msgprint(
            _("No TDS entries found for the selected section between {0} and {1}.<br>"
              "Searched accounts: {2}").format(
                period_from, period_to, ", ".join(account_list)
            ),
            indicator="orange",
        )
        return {"entries": [], "added": 0, "skipped_linked": 0,
                "skipped_duplicate": 0, "remaining": remaining}

    entries = []
    added_count = 0
    skipped_linked = 0
    skipped_duplicate = 0

    for gl in gl_entries:
        if remaining <= 0:
            break

        vtype = gl.voucher_type
        vno = gl.voucher_no
        key = (vtype, vno)

        if key in already_linked_set:
            skipped_linked += 1
            continue

        if key in current_vouchers:
            skipped_duplicate += 1
            continue

        tds_amt = flt(gl.tds_amount, 2)

        # FIFO allocation
        if tds_amt <= remaining:
            alloc = tds_amt
        else:
            alloc = flt(remaining, 2)

        row_data = _build_entry_row(vtype, vno, gl.posting_date, tds_amt, alloc,
                                    gl.tds_account)
        if row_data:
            entries.append(row_data)
            current_vouchers.add(key)
            remaining = flt(remaining - alloc, 2)
            added_count += 1

    return {
        "entries": entries,
        "added": added_count,
        "skipped_linked": skipped_linked,
        "skipped_duplicate": skipped_duplicate,
        "remaining": remaining,
    }


def _build_entry_row(voucher_type, voucher_no, posting_date, tds_amount,
                     allocated_amount, tds_account=""):
    """Build a child table row dict for the given voucher."""
    row = {
        "voucher_type": voucher_type,
        "voucher_no": voucher_no,
        "posting_date": cstr(posting_date),
        "tds_amount": tds_amount,
        "allocated_amount": allocated_amount,
        "tds_account": tds_account,
        "supplier": "",
        "supplier_name": "",
        "supplier_pan": "",
        "bill_no": "",
        "bill_date": "",
        "invoice_net_total": 0,
        "invoice_grand_total": 0,
        "tax_withholding_category": "",
        "purchase_order": "",
        "purchase_receipt": "",
        "project": "",
        "cost_center": "",
        "je_remark": "",
    }

    if voucher_type == "Purchase Invoice":
        pi = frappe.db.get_value(
            "Purchase Invoice",
            voucher_no,
            [
                "supplier", "supplier_name", "tax_id",
                "posting_date", "bill_no", "bill_date",
                "net_total", "grand_total", "tax_withholding_category",
            ],
            as_dict=True,
        )
        if not pi:
            return None

        first_item = frappe.db.get_value(
            "Purchase Invoice Item",
            {"parent": voucher_no, "idx": 1},
            ["purchase_order", "purchase_receipt", "project", "cost_center"],
            as_dict=True,
        )

        row["supplier"] = pi.supplier
        row["supplier_name"] = pi.supplier_name
        row["supplier_pan"] = _extract_pan(pi.tax_id)
        row["bill_no"] = pi.bill_no or ""
        row["bill_date"] = cstr(pi.bill_date) if pi.bill_date else ""
        row["invoice_net_total"] = flt(pi.net_total)
        row["invoice_grand_total"] = flt(pi.grand_total)
        row["tax_withholding_category"] = pi.tax_withholding_category or ""

        if first_item:
            row["purchase_order"] = first_item.purchase_order or ""
            row["purchase_receipt"] = first_item.purchase_receipt or ""
            row["project"] = first_item.project or ""
            row["cost_center"] = first_item.cost_center or ""

    elif voucher_type == "Journal Entry":
        je = frappe.db.get_value(
            "Journal Entry",
            voucher_no,
            ["posting_date", "remark", "user_remark"],
            as_dict=True,
        )
        if not je:
            return None

        row["je_remark"] = je.user_remark or je.remark or ""

        # Try to find the supplier from the JE accounts
        party_info = frappe.db.sql(
            """
            SELECT party_type, party
            FROM `tabJournal Entry Account`
            WHERE parent = %(je)s
              AND party_type = 'Supplier'
              AND party IS NOT NULL AND party != ''
            LIMIT 1
        """,
            {"je": voucher_no},
            as_dict=True,
        )

        if party_info:
            supplier = party_info[0].party
            row["supplier"] = supplier
            supplier_doc = frappe.db.get_value(
                "Supplier", supplier, ["supplier_name", "tax_id"], as_dict=True
            )
            if supplier_doc:
                row["supplier_name"] = supplier_doc.supplier_name or ""
                row["supplier_pan"] = _extract_pan(supplier_doc.tax_id)

    return row


def _extract_pan(tax_id):
    """Extract PAN from GSTIN (chars 3-12) or return as-is if already PAN."""
    if not tax_id:
        return ""
    tax_id = cstr(tax_id).strip()
    if len(tax_id) == 15:
        return tax_id[2:12]
    return tax_id

