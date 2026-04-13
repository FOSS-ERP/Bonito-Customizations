"""
Fix TDS Category on Draft Purchase Invoices
=============================================
Run via: bench execute bonito_customizations.fix_tds_on_bulk_pis.fix_tds

Or paste into bench console:
    bench console
    >>> exec(open('apps/bonito_customizations/bonito_customizations/fix_tds_on_bulk_pis.py').read())

What it does:
  1. Reads the CSV to build a map: Purchase Receipt → correct TDS category
  2. Finds the draft Purchase Invoice linked to each Purchase Receipt
  3. Removes any existing TDS tax rows from the taxes table
  4. Sets the correct TDS category and recalculates taxes/totals
  5. Dry-run by default — set DRY_RUN = False to actually save

Author: Bonito Designs Tech Team
Date: 2026-02-20
"""

import frappe
import csv
from io import StringIO
from datetime import datetime

# ── Configuration ─────────────────────────────────────────────────────────────

DRY_RUN = False  # Set to False to actually save changes
CSV_FILE_URL = None  # Set to the file URL if running standalone, e.g. "/files/bulk_pi_template.csv"
BULK_PI_DOC_NAME = "BULK-PI-2026-00019"  # Set to the Bulk Purchase Invoice Creation doc name, e.g. "BPI-00001"

# ── Date parser (same as bulk_pi_native) ──────────────────────────────────────

def parse_date(date_str):
    if not date_str:
        return None
    date_str = date_str.strip()
    for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%m/%d/%Y"]:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _remove_tds_tax_rows(pi):
    """
    Remove all TDS-related rows from the PI's taxes child table.

    TDS rows are identified by:
      - add_deduct_tax == "Deduct" (TDS rows are always deductions), OR
      - account_head linked to a Tax Withholding Category account, OR
      - description containing "TDS" or "Tax Withholding"

    Returns the count of rows removed.
    """
    # Collect all account heads used by Tax Withholding Categories
    twc_accounts = set()
    twc_data = frappe.get_all(
        "Tax Withholding Account",
        filters={"company": pi.company or "Bonito Designs Pvt Ltd"},
        fields=["account"],
    )
    for row in twc_data:
        if row.account:
            twc_accounts.add(row.account)

    rows_to_remove = []
    for tax in pi.taxes:
        is_tds = False

        # Check 1: add_deduct_tax flag (most reliable)
        if getattr(tax, "add_deduct_tax", None) == "Deduct":
            is_tds = True

        # Check 2: account head matches a Tax Withholding Category account
        if tax.account_head in twc_accounts:
            is_tds = True

        # Check 3: description contains TDS-related keywords (fallback)
        desc = (tax.description or "").upper()
        if "TDS" in desc or "TAX WITHHOLDING" in desc:
            is_tds = True

        if is_tds:
            rows_to_remove.append(tax)

    for row in rows_to_remove:
        pi.taxes.remove(row)

    return len(rows_to_remove)


def fix_tds():
    """Main function to fix TDS on draft PIs."""

    # ── Step 1: Get the CSV content ───────────────────────────────────────
    csv_content = None

    if BULK_PI_DOC_NAME:
        doc = frappe.get_doc("Bulk Purchase Invoice Creation", BULK_PI_DOC_NAME)
        if not doc.csv_file:
            print("ERROR: No CSV file attached to the Bulk PI doc")
            return
        file_doc = frappe.get_doc("File", {"file_url": doc.csv_file})
        raw = file_doc.get_content()
        csv_content = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    elif CSV_FILE_URL:
        file_doc = frappe.get_doc("File", {"file_url": CSV_FILE_URL})
        raw = file_doc.get_content()
        csv_content = raw.decode("utf-8-sig") if isinstance(raw, bytes) else raw
    else:
        print("ERROR: Set either BULK_PI_DOC_NAME or CSV_FILE_URL at the top of the script")
        return

    # ── Step 2: Parse CSV → { purchase_receipt: tds_category } ────────────
    reader = csv.DictReader(StringIO(csv_content))
    pr_tds_map = {}
    for row in reader:
        pr_no = (row.get("Purchase Receipt No") or "").strip()
        tds = (row.get("TDS") or "").strip()
        if pr_no and tds:
            pr_tds_map[pr_no] = tds

    print(f"CSV loaded: {len(pr_tds_map)} Purchase Receipts with TDS categories")

    # ── Step 3: Find draft PIs linked to these Purchase Receipts ──────────
    pr_names = list(pr_tds_map.keys())

    pi_items = frappe.get_all(
        "Purchase Invoice Item",
        filters={
            "purchase_receipt": ["in", pr_names],
            "docstatus": 0,
        },
        fields=["parent", "purchase_receipt"],
        group_by="parent",
    )

    pr_to_pi = {}
    for item in pi_items:
        pr_to_pi[item.purchase_receipt] = item.parent

    print(f"Found {len(pr_to_pi)} draft Purchase Invoices linked to these PRs")

    # ── Step 4: Fix each PI ───────────────────────────────────────────────
    fixed = 0
    skipped_already_correct = 0
    skipped_not_found = 0
    errors = []

    for pr_no, correct_tds in pr_tds_map.items():
        pi_name = pr_to_pi.get(pr_no)

        if not pi_name:
            skipped_not_found += 1
            continue

        try:
            pi = frappe.get_doc("Purchase Invoice", pi_name)

            current_tds = pi.tax_withholding_category or ""
            current_tax_row_count = len([
                t for t in pi.taxes
                if getattr(t, "add_deduct_tax", None) == "Deduct"
                or "TDS" in (t.description or "").upper()
            ])

            # Skip only if TDS category matches AND there's exactly one TDS row
            if current_tds == correct_tds and current_tax_row_count <= 1:
                skipped_already_correct += 1
                continue

            # Report what we're fixing
            reason = []
            if current_tds != correct_tds:
                reason.append(f"TDS: '{current_tds}' → '{correct_tds}'")
            if current_tax_row_count > 1:
                reason.append(f"duplicate TDS rows: {current_tax_row_count} → 1")

            print(f"  {pi_name} ({pr_no}): {', '.join(reason)}")

            if not DRY_RUN:
                # Step A: Remove ALL existing TDS tax rows
                removed = _remove_tds_tax_rows(pi)

                # Step B: Set the correct TDS category
                pi.apply_tds = 1
                pi.tax_withholding_category = correct_tds

                # Step C: Let ERPNext re-apply the correct TDS
                # This calls set_tax_witholding_clauses which adds the
                # correct TDS row based on the new category
                pi.run_method("set_missing_values")

                # Step D: Override TDS again in case set_missing_values
                # pulled the supplier default (the original bug)
                pi.tax_withholding_category = correct_tds

                # Step E: Recalculate totals
                pi.run_method("calculate_taxes_and_totals")
                pi.save()

            fixed += 1

        except Exception as e:
            errors.append(f"{pi_name} ({pr_no}): {str(e)[:200]}")
            print(f"  ERROR {pi_name}: {e}")

    # ── Step 5: Summary ───────────────────────────────────────────────────
    if not DRY_RUN:
        frappe.db.commit()

    print("\n" + "=" * 60)
    print(f"{'DRY RUN — no changes saved' if DRY_RUN else 'CHANGES SAVED'}")
    print("=" * 60)
    print(f"Total PRs in CSV:          {len(pr_tds_map)}")
    print(f"Draft PIs found:           {len(pr_to_pi)}")
    print(f"Fixed (TDS corrected):     {fixed}")
    print(f"Skipped (already correct): {skipped_already_correct}")
    print(f"Skipped (PI not found):    {skipped_not_found}")
    print(f"Errors:                    {len(errors)}")

    if errors:
        print("\nErrors:")
        for err in errors:
            print(f"  - {err}")

    if DRY_RUN and fixed > 0:
        print(f"\n→ Set DRY_RUN = False and re-run to apply {fixed} changes")

    return {
        "fixed": fixed,
        "already_correct": skipped_already_correct,
        "not_found": skipped_not_found,
        "errors": len(errors),
    }


# ── Auto-execute when pasted into bench console ──────────────────────────────
if __name__ != "builtins":
    fix_tds()
