# bonito_customizations/overrides/purchase_invoice_return.py

import frappe
from frappe import _


@frappe.whitelist()
def get_original_invoice_tax_tds(original_invoice):
    """
    Fetch tax rows and TDS info from the original Purchase Invoice.
    Returns GST rows, TDS row, and rounding row separately.
    """
    if not original_invoice:
        return None

    original = frappe.get_doc("Purchase Invoice", original_invoice)

    result = {
        "taxes_and_charges_template": original.taxes_and_charges or None,
        "taxes": [],
        "tax_withholding_category": original.tax_withholding_category or None,
        "tds_row": None,
        "rounding_row": None,
        "rounding_adjustment": original.rounding_adjustment or 0,
    }

    tds_accounts = _get_tds_accounts()
    rounding_accounts = _get_rounding_accounts(original.company)

    for tax in original.taxes:
        tax_data = {
            "charge_type": tax.charge_type,
            "account_head": tax.account_head,
            "description": tax.description,
            "rate": tax.rate,
            "cost_center": tax.cost_center,
            "add_deduct_tax": tax.add_deduct_tax,
            "category": tax.category,
            "tax_amount": tax.tax_amount,
            "total": tax.total,
            "row_id": tax.row_id,
            "included_in_print_rate": tax.included_in_print_rate,
            "included_in_paid_amount": tax.included_in_paid_amount,
        }

        if tax.account_head in tds_accounts:
            result["tds_row"] = tax_data
        elif tax.account_head in rounding_accounts or "round" in (tax.description or "").lower():
            result["rounding_row"] = tax_data
        else:
            result["taxes"].append(tax_data)

    return result


def _get_tds_accounts():
    """Get all accounts linked to Tax Withholding Categories."""
    accounts = frappe.get_all(
        "Tax Withholding Account",
        filters={"parenttype": "Tax Withholding Category"},
        fields=["account"],
    )
    return {a.account for a in accounts}


def _get_rounding_accounts(company):
    """Get round-off account for the company."""
    accounts = set()
    round_off_account = frappe.db.get_value(
        "Company", company, "round_off_account"
    )
    if round_off_account:
        accounts.add(round_off_account)
    return accounts


def _has_real_tax_rows(doc):
    """Check if the taxes table has any real (non-empty) rows."""
    if not doc.taxes:
        return False
    for row in doc.taxes:
        if row.account_head:
            return True
    return False


def before_save_populate_return_taxes(doc, method=None):
    """
    Hook: Purchase Invoice -> before_save

    For returns/debit notes: ensure taxes and TDS from the original
    invoice are present.

    TDS GL LOGIC:
    Original invoice:
      - TDS row: Deduct, +744.22
      - GL: Credit TDS Payable 744.22
      - Effect: reduces creditor payable (supplier gets less)

    Return (what we need):
      - GL: Debit TDS Payable 744.22 (reverse the original credit)
      - To achieve this: Add, +744.22
      - On negative base (-43,908.98), adding +744.22 = -43,164.76
      - ERPNext sees "Add" on TDS account → debits it in GL

    So on the return, TDS flips from Deduct → Add, amount stays positive.
    """
    if not doc.is_return or not doc.return_against:
        return

    original = frappe.get_doc("Purchase Invoice", doc.return_against)

    if not original.taxes:
        return

    tds_accounts = _get_tds_accounts()
    rounding_accounts = _get_rounding_accounts(doc.company)

    # Disable TDS engine so it doesn't interfere
    doc.apply_tds = 0
    doc.tax_withholding_category = ""

    if not _has_real_tax_rows(doc):
        # Nothing populated yet — copy everything from original
        doc.taxes = []

        if original.taxes_and_charges:
            doc.taxes_and_charges = original.taxes_and_charges

        for tax in original.taxes:
            row = doc.append("taxes", {})
            _copy_tax_row_for_return(row, tax, tds_accounts, rounding_accounts)

    else:
        # Taxes already populated (client script added GST rows).
        # Check for missing TDS and rounding rows and add them.
        existing_accounts = []
        for row in doc.taxes:
            if row.account_head:
                existing_accounts.append(row.account_head)

        for tax in original.taxes:
            is_tds = tax.account_head in tds_accounts
            is_rounding = (
                tax.account_head in rounding_accounts
                or "round" in (tax.description or "").lower()
            )

            if (is_tds or is_rounding) and tax.account_head not in existing_accounts:
                row = doc.append("taxes", {})
                _copy_tax_row_for_return(row, tax, tds_accounts, rounding_accounts)

    frappe.msgprint(
        "Taxes and TDS copied from original invoice "
        + "<b>" + doc.return_against + "</b>",
        alert=True,
        indicator="green",
    )


def _copy_tax_row_for_return(row, original_tax, tds_accounts, rounding_accounts):
    """
    Copy a tax row from original invoice to return, with correct handling.

    - GST rows: copied as-is (rate-based); ERPNext auto-computes negative
      amounts from the negative net_total
    - TDS rows: Actual/Add with POSITIVE amount. On original it was Deduct
      (credits TDS Payable). On return we flip to Add so ERPNext debits
      TDS Payable — correctly reversing the original.
    - Rounding rows: Actual with NEGATED amount (reverses the rounding)
    """
    is_tds = original_tax.account_head in tds_accounts
    is_rounding = (
        original_tax.account_head in rounding_accounts
        or "round" in (original_tax.description or "").lower()
    )

    if is_tds:
        # TDS: flip Deduct → Add, keep amount positive.
        # Original: Deduct +744.22 → GL credits TDS Payable
        # Return:   Add    +744.22 → GL debits TDS Payable ✓
        row.category = "Total"
        row.charge_type = "Actual"
        row.account_head = original_tax.account_head
        row.description = original_tax.description or "TDS"
        row.add_deduct_tax = "Add"  # FLIPPED from original's "Deduct"
        row.rate = 0
        row.tax_amount = abs(original_tax.tax_amount)  # POSITIVE
        row.cost_center = original_tax.cost_center
        row.included_in_print_rate = 0
        row.included_in_paid_amount = 0

    elif is_rounding:
        # Rounding: negate the original rounding amount
        row.category = original_tax.category or "Total"
        row.charge_type = "Actual"
        row.account_head = original_tax.account_head
        row.description = original_tax.description or "Round Off"
        row.add_deduct_tax = original_tax.add_deduct_tax
        row.rate = 0
        row.tax_amount = -1 * original_tax.tax_amount  # NEGATE
        row.cost_center = original_tax.cost_center
        row.included_in_print_rate = 0
        row.included_in_paid_amount = 0

    else:
        # GST / other percentage-based taxes: copy as-is
        row.charge_type = original_tax.charge_type
        row.account_head = original_tax.account_head
        row.description = original_tax.description
        row.rate = original_tax.rate
        row.cost_center = original_tax.cost_center
        row.add_deduct_tax = original_tax.add_deduct_tax
        row.category = original_tax.category
        row.included_in_print_rate = original_tax.included_in_print_rate
        row.included_in_paid_amount = original_tax.included_in_paid_amount
        if original_tax.row_id:
            row.row_id = original_tax.row_id
