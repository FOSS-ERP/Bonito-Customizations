"""
Export Payment Reconciliation data for all suppliers from ERPNext.
Mirrors the Payment Reconciliation UI exactly by calling the same methods,
with a patch for the missing posting_date in unallocated PE results.

Usage:
    bench --site <sitename> execute export_payment_recon.main

    # Limit to specific suppliers
    bench --site <sitename> execute export_payment_recon.main --kwargs '{"suppliers": "SUP-001,SUP-002"}'
"""

import frappe
from frappe.utils import flt
import csv
import os
from datetime import date
from collections import defaultdict


def main(suppliers=None, output_dir=None):
    if output_dir is None:
        output_dir = frappe.get_site_path("private", "files")

    company = (
        frappe.defaults.get_defaults().get("company")
        or frappe.db.get_single_value("Global Defaults", "default_company")
    )
    if not company:
        print("No company found")
        return

    default_payable = frappe.db.get_value("Company", company, "default_payable_account")
    print(f"Company: {company}")
    print(f"Default Payable Account: {default_payable}")

    if suppliers:
        supplier_list = [s.strip() for s in suppliers.split(",")]
    else:
        supplier_list = frappe.get_all("Supplier", pluck="name", order_by="name")

    print(f"Total suppliers to process: {len(supplier_list)}")

    payment_rows = []
    invoice_rows = []
    errors = []
    skipped = 0

    for i, supplier in enumerate(supplier_list):
        if (i + 1) % 50 == 0:
            print(f"  Processing {i+1}/{len(supplier_list)}...")

        payable_account = get_payable_account(supplier, company, default_payable)
        if not payable_account:
            skipped += 1
            continue

        try:
            pr = frappe.new_doc("Payment Reconciliation")
            pr.company = company
            pr.party_type = "Supplier"
            pr.party = supplier
            pr.receivable_payable_account = payable_account
            # Don't set limit so we get everything
            # Don't set bank_cash_account so JE query uses "1=1" (no filter)
            # Don't set date/amount filters

            pr.get_unreconciled_entries()

            payments = pr.get("payments") or []
            invoices = pr.get("invoices") or []

            if not payments and not invoices:
                continue

            # ---- Payments / JEs ----
            for p in payments:
                # Fix: posting_date is not returned by the unallocated PE query
                # in get_advance_payment_entries(). Fetch it from the source doc.
                posting_date = p.get("posting_date")
                currency = p.get("currency")

                if not posting_date and p.get("reference_name"):
                    if p.get("reference_type") == "Payment Entry":
                        pe_data = frappe.db.get_value(
                            "Payment Entry", p.reference_name,
                            ["posting_date", "paid_to_account_currency"],
                            as_dict=True
                        )
                        if pe_data:
                            posting_date = pe_data.posting_date
                            currency = currency or pe_data.paid_to_account_currency
                    elif p.get("reference_type") == "Journal Entry":
                        posting_date = frappe.db.get_value(
                            "Journal Entry", p.reference_name, "posting_date"
                        )

                payment_rows.append({
                    "Supplier": supplier,
                    "Reference Type": p.get("reference_type", ""),
                    "Reference Name": p.get("reference_name", ""),
                    "Posting Date": str(posting_date or ""),
                    "Amount": flt(p.get("amount", 0)),
                    "Currency": currency or "INR",
                    "Is Advance": p.get("is_advance", ""),
                    "Remarks": (p.get("remarks") or "")[:200],
                })

            # ---- Invoices ----
            for inv in invoices:
                invoice_rows.append({
                    "Supplier": supplier,
                    "Invoice Type": inv.get("invoice_type", ""),
                    "Invoice Number": inv.get("invoice_number", ""),
                    "Invoice Date": str(inv.get("invoice_date") or ""),
                    "Amount": flt(inv.get("amount", 0)),
                    "Outstanding Amount": flt(inv.get("outstanding_amount", 0)),
                    "Currency": inv.get("currency") or "INR",
                })

        except Exception as e:
            errors.append({"supplier": supplier, "error": str(e)})
            continue

    # =========================================================================
    # Write CSVs
    # =========================================================================
    today_str = date.today().strftime("%Y-%m-%d")

    if payment_rows:
        f1 = os.path.join(output_dir, f"unreconciled_payments_{today_str}.csv")
        write_csv(f1, payment_rows)
        print(f"\nPayments CSV ({len(payment_rows)} rows): {f1}")
    else:
        print("\nNo unreconciled payments found.")

    if invoice_rows:
        f2 = os.path.join(output_dir, f"unreconciled_invoices_{today_str}.csv")
        write_csv(f2, invoice_rows)
        print(f"Invoices CSV ({len(invoice_rows)} rows): {f2}")
    else:
        print("No outstanding invoices found.")

    # Summary
    pay_totals = defaultdict(float)
    inv_totals = defaultdict(float)
    for r in payment_rows:
        pay_totals[r["Supplier"]] += r["Amount"]
    for r in invoice_rows:
        inv_totals[r["Supplier"]] += r["Outstanding Amount"]

    all_sup = sorted(set(list(pay_totals.keys()) + list(inv_totals.keys())))
    summary_rows = [{
        "Supplier": s,
        "Total Unreconciled Payments": round(pay_totals.get(s, 0), 2),
        "Total Outstanding Invoices": round(inv_totals.get(s, 0), 2),
        "Difference": round(pay_totals.get(s, 0) - inv_totals.get(s, 0), 2),
    } for s in all_sup]

    if summary_rows:
        f3 = os.path.join(output_dir, f"payment_recon_summary_{today_str}.csv")
        write_csv(f3, summary_rows)
        print(f"Summary CSV ({len(summary_rows)} rows): {f3}")

    if errors:
        f4 = os.path.join(output_dir, f"payment_recon_errors_{today_str}.csv")
        write_csv(f4, errors)
        print(f"Errors CSV ({len(errors)} rows): {f4}")

    print(f"\nSkipped (no payable account): {skipped}")
    print(f"Errors: {len(errors)}")
    print(f"Suppliers with data: {len(all_sup)}")

    return {
        "payment_count": len(payment_rows),
        "invoice_count": len(invoice_rows),
        "supplier_count": len(all_sup),
        "errors": len(errors),
    }


def get_payable_account(supplier, company, default_payable):
    acc = frappe.db.get_value(
        "Party Account",
        {"parenttype": "Supplier", "parent": supplier, "company": company},
        "account"
    )
    return acc or default_payable


def write_csv(filepath, rows):
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    with open(filepath, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
