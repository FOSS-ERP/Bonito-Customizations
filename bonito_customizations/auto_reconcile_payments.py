"""
Auto-reconcile payments against invoices for all suppliers using FIFO.
Uses the PaymentReconciliation doctype exactly as the UI does.

Two modes:
  DRY RUN (default) - CSV of proposed allocations for finance review
  EXECUTE           - performs reconciliation via pr.reconcile()

Usage:
    # Dry run
    bench --site <sitename> execute auto_reconcile_payments.main

    # Execute
    bench --site <sitename> execute auto_reconcile_payments.main --kwargs '{"execute": true}'

    # Specific suppliers
    bench --site <sitename> execute auto_reconcile_payments.main --kwargs '{"suppliers": "SUP-001,SUP-002"}'

    # Execute from approved dry-run CSV
    bench --site <sitename> execute auto_reconcile_payments.main --kwargs '{"execute": true, "approved_file": "/path/to/approved.csv"}'
"""

import frappe
from frappe.utils import flt, getdate
import csv
import os
from datetime import date
from collections import defaultdict


def main(execute=False, suppliers=None, approved_file=None, output_dir=None):
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
    print(f"Default Payable: {default_payable}")
    print(f"Mode: {'EXECUTE' if execute else 'DRY RUN'}")
    print("=" * 70)

    if execute and approved_file:
        return execute_from_approved_file(approved_file, company, default_payable)

    if suppliers:
        supplier_list = [s.strip() for s in suppliers.split(",")]
    else:
        supplier_list = frappe.get_all("Supplier", pluck="name", order_by="name")

    print(f"Suppliers to process: {len(supplier_list)}")

    all_allocations = []
    all_skipped = []
    supplier_summaries = []

    for i, supplier in enumerate(supplier_list):
        if (i + 1) % 50 == 0:
            print(f"  Processing {i+1}/{len(supplier_list)}...")

        payable_account = get_payable_account(supplier, company, default_payable)
        if not payable_account:
            continue

        try:
            result = process_supplier(supplier, company, payable_account, execute)
            if result:
                all_allocations.extend(result["allocations"])
                all_skipped.extend(result["skipped"])
                if result["allocations"]:
                    supplier_summaries.append({
                        "Supplier": supplier,
                        "Allocations": len(result["allocations"]),
                        "Total Allocated": round(sum(
                            a["Allocated Amount"] for a in result["allocations"]
                        ), 2),
                        "Advances": sum(
                            1 for a in result["allocations"] if a["Is Advance"] == "Yes"
                        ),
                        "Remaining Payments": round(result["remaining_payments"], 2),
                        "Remaining Invoices": round(result["remaining_invoices"], 2),
                    })
        except Exception as e:
            print(f"  Error for {supplier}: {e}")
            continue

    # Write output CSVs
    today_str = date.today().strftime("%Y-%m-%d")
    mode_tag = "executed" if execute else "proposed"

    if all_allocations:
        f1 = os.path.join(output_dir, f"recon_allocations_{mode_tag}_{today_str}.csv")
        write_csv(f1, all_allocations)
        print(f"\nAllocations CSV ({len(all_allocations)} rows): {f1}")

    if all_skipped:
        f2 = os.path.join(output_dir, f"recon_skipped_{today_str}.csv")
        write_csv(f2, all_skipped)
        print(f"Skipped CSV ({len(all_skipped)} rows): {f2}")

    if supplier_summaries:
        f3 = os.path.join(output_dir, f"recon_summary_{mode_tag}_{today_str}.csv")
        write_csv(f3, supplier_summaries)
        print(f"Summary CSV ({len(supplier_summaries)} rows): {f3}")

    total_amount = sum(a["Allocated Amount"] for a in all_allocations)
    print(f"\n{'='*60}")
    print(f"Total allocations: {len(all_allocations)}")
    print(f"Total amount: {total_amount:,.2f}")
    print(f"Total skipped: {len(all_skipped)}")

    if not execute and all_allocations:
        print(f"\n{'='*60}")
        print("DRY RUN COMPLETE. No changes were made.")
        print("Review the CSV, then re-run with execute=True to apply.")
        print("=" * 60)

    return {
        "allocations": len(all_allocations),
        "total_amount": total_amount,
        "skipped": len(all_skipped),
    }


def process_supplier(supplier, company, payable_account, execute=False):
    """
    Use PaymentReconciliation doctype to get unreconciled entries
    (exactly as the UI does), then apply FIFO matching.
    """
    pr = frappe.new_doc("Payment Reconciliation")
    pr.company = company
    pr.party_type = "Supplier"
    pr.party = supplier
    pr.receivable_payable_account = payable_account

    pr.get_unreconciled_entries()

    raw_payments = pr.get("payments") or []
    raw_invoices = pr.get("invoices") or []

    if not raw_payments or not raw_invoices:
        return None

    # Build working lists with resolved posting_date
    # (posting_date is missing from unallocated PE query in v13 get_advance_payment_entries)
    payments = []
    for p in raw_payments:
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

        payments.append({
            "reference_type": p.get("reference_type"),
            "reference_name": p.get("reference_name"),
            "reference_row": p.get("reference_row"),
            "posting_date": getdate(posting_date) if posting_date else None,
            "amount": flt(p.get("amount")),
            "remaining": flt(p.get("amount")),
            "currency": currency or "INR",
            "is_advance": p.get("is_advance"),
        })

    invoices = []
    for inv in raw_invoices:
        invoices.append({
            "invoice_type": inv.get("invoice_type"),
            "invoice_number": inv.get("invoice_number"),
            "invoice_date": getdate(inv.get("invoice_date")) if inv.get("invoice_date") else None,
            "amount": flt(inv.get("amount")),
            "outstanding_amount": flt(inv.get("outstanding_amount")),
            "remaining": flt(inv.get("outstanding_amount")),
            "currency": inv.get("currency") or "INR",
        })

    # Sort by date for FIFO
    payments.sort(key=lambda x: x["posting_date"] or getdate("2000-01-01"))
    invoices.sort(key=lambda x: x["invoice_date"] or getdate("2000-01-01"))

    # FIFO matching
    allocations = []
    for pay in payments:
        if pay["remaining"] <= 0.005:
            continue

        for inv in invoices:
            if inv["remaining"] <= 0.005:
                continue

            if pay["currency"] != inv["currency"]:
                continue

            is_advance = (
                pay["posting_date"] and inv["invoice_date"]
                and pay["posting_date"] < inv["invoice_date"]
            )

            alloc_amount = round(min(pay["remaining"], inv["remaining"]), 2)
            if alloc_amount <= 0.005:
                continue

            allocations.append({
                "Supplier": supplier,
                "Payment Type": pay["reference_type"],
                "Payment Name": pay["reference_name"],
                "Payment Ref Row": pay["reference_row"] or "",
                "Payment Date": str(pay["posting_date"] or ""),
                "Payment Amount": pay["amount"],
                "Invoice Type": inv["invoice_type"],
                "Invoice Number": inv["invoice_number"],
                "Invoice Date": str(inv["invoice_date"] or ""),
                "Invoice Outstanding": inv["outstanding_amount"],
                "Allocated Amount": alloc_amount,
                "Is Advance": "Yes" if is_advance else "No",
                "Currency": pay["currency"],
            })

            pay["remaining"] = round(pay["remaining"] - alloc_amount, 2)
            inv["remaining"] = round(inv["remaining"] - alloc_amount, 2)

            if pay["remaining"] <= 0.005:
                break

    # Track unmatched
    skipped = []
    for pay in payments:
        if pay["remaining"] > 0.005:
            skipped.append({
                "Supplier": supplier,
                "Type": "Payment",
                "Reference": f"{pay['reference_type']}: {pay['reference_name']}",
                "Posting Date": str(pay["posting_date"] or ""),
                "Unmatched Amount": round(pay["remaining"], 2),
                "Reason": "No matching invoice",
            })
    for inv in invoices:
        if inv["remaining"] > 0.005:
            skipped.append({
                "Supplier": supplier,
                "Type": "Invoice",
                "Reference": f"{inv['invoice_type']}: {inv['invoice_number']}",
                "Posting Date": str(inv["invoice_date"] or ""),
                "Unmatched Amount": round(inv["remaining"], 2),
                "Reason": "No matching payment",
            })

    # Execute if requested
    if execute and allocations:
        execute_for_supplier(pr, allocations, supplier)

    remaining_payments = sum(p["remaining"] for p in payments if p["remaining"] > 0.005)
    remaining_invoices = sum(i["remaining"] for i in invoices if i["remaining"] > 0.005)

    return {
        "allocations": allocations,
        "skipped": skipped,
        "remaining_payments": remaining_payments,
        "remaining_invoices": remaining_invoices,
    }


def execute_for_supplier(pr, allocations, supplier):
    """
    Execute reconciliation using the PaymentReconciliation doctype's reconcile() method.
    This mirrors what the UI does: set invoice_type, invoice_number, allocated_amount
    on the payments child table rows, then call reconcile().
    """
    # The v13 reconcile() expects the payments child table rows to have
    # invoice_number (formatted as "Invoice Type | Invoice Number"),
    # and allocated_amount set on them.
    # It then calls self.get_invoice_entries() again internally,
    # validates, and processes.

    # Re-fetch to get fresh state
    pr.get_unreconciled_entries()

    payments_by_key = {}
    for p in (pr.get("payments") or []):
        key = (p.reference_type, p.reference_name, p.get("reference_row") or "")
        payments_by_key[key] = p

    # Set allocations on the payment rows
    # In v13 UI, each payment row can only be matched to one invoice.
    # For a payment split across multiple invoices, we need multiple reconcile() calls
    # or the payment appears multiple times (once per against_order).

    # Group allocations by payment
    allocs_by_payment = defaultdict(list)
    for a in allocations:
        key = (a["Payment Type"], a["Payment Name"], a.get("Payment Ref Row") or "")
        allocs_by_payment[key].append(a)

    total_done = 0

    for pay_key, pay_allocs in allocs_by_payment.items():
        for alloc in pay_allocs:
            # Re-fetch each time since state changes after each reconcile
            pr.get_unreconciled_entries()

            p_row = None
            for p in (pr.get("payments") or []):
                pk = (p.reference_type, p.reference_name, p.get("reference_row") or "")
                if pk == pay_key:
                    p_row = p
                    break

            if not p_row:
                print(f"    {supplier}: Payment {pay_key} no longer unreconciled, skipping")
                continue

            # Verify invoice still outstanding
            inv_found = False
            for inv in (pr.get("invoices") or []):
                if inv.invoice_type == alloc["Invoice Type"] and inv.invoice_number == alloc["Invoice Number"]:
                    inv_found = True
                    break

            if not inv_found:
                print(f"    {supplier}: Invoice {alloc['Invoice Number']} no longer outstanding, skipping")
                continue

            actual_amount = min(
                flt(alloc["Allocated Amount"]),
                flt(p_row.amount),
                flt(inv.outstanding_amount),
            )

            if actual_amount <= 0.005:
                continue

            # Set the allocation on the payment row (v13 format)
            p_row.invoice_number = f"{alloc['Invoice Type']} | {alloc['Invoice Number']}"
            p_row.allocated_amount = actual_amount
            p_row.invoice_type = alloc["Invoice Type"]

            try:
                pr.reconcile(args=None)
                frappe.db.commit()
                total_done += 1
            except Exception as e:
                frappe.db.rollback()
                print(f"    FAILED {supplier}: {alloc['Payment Name']} -> {alloc['Invoice Number']}: {e}")

    if total_done:
        print(f"  {supplier}: reconciled {total_done}/{len(allocations)} allocations")


def execute_from_approved_file(filepath, company, default_payable):
    """Execute from a reviewed/approved dry-run CSV."""
    if not os.path.exists(filepath):
        print(f"File not found: {filepath}")
        return

    with open(filepath, "r") as f:
        reader = csv.DictReader(f)
        all_allocations = list(reader)

    for a in all_allocations:
        a["Allocated Amount"] = float(a["Allocated Amount"])

    by_supplier = defaultdict(list)
    for a in all_allocations:
        by_supplier[a["Supplier"]].append(a)

    print(f"Approved file: {len(all_allocations)} allocations across {len(by_supplier)} suppliers")

    total_done = 0
    total_failed = 0

    for supplier, allocs in by_supplier.items():
        payable_account = get_payable_account(supplier, company, default_payable)
        if not payable_account:
            print(f"  {supplier}: no payable account, skipping")
            total_failed += len(allocs)
            continue

        pr = frappe.new_doc("Payment Reconciliation")
        pr.company = company
        pr.party_type = "Supplier"
        pr.party = supplier
        pr.receivable_payable_account = payable_account

        execute_for_supplier(pr, allocs, supplier)
        # Count is printed inside execute_for_supplier

    print(f"\nExecution from approved file complete.")


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
