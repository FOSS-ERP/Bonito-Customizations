"""
Bulk Purchase Invoice Creation (Extended + Performance Optimized)
==================================================================
Supports TWO modes:
  1. "From Purchase Receipt" — original flow, creates PI linked to PR
  2. "Direct (No Receipt)"  — creates PI directly from CSV without PO/PRE

Performance optimizations:
  - BatchCache: per-batch caching for db.exists, accounting period, and duplicate checks
  - Tax template docs cached and reused across invoices
  - Batch supplier lookups during CSV load

Installation:
    REPLACES: apps/bonito_customizations/bonito_customizations/bulk_pi_native.py

    hooks.py remains unchanged:
    doc_events = {
        "Bulk Purchase Invoice Creation": {
            "before_save": "bonito_customizations.bulk_pi_native.before_save",
        }
    }

Recommended indexes (run once via bench mariadb):
    CREATE INDEX idx_pi_bill_no_supplier
        ON `tabPurchase Invoice` (bill_no, supplier, docstatus);
    CREATE INDEX idx_pi_item_purchase_receipt
        ON `tabPurchase Invoice Item` (purchase_receipt, docstatus);
    CREATE INDEX idx_accounting_period_dates
        ON `tabAccounting Period` (company, start_date, end_date);

FIX (2026-02-20): TDS category from CSV was being overwritten by supplier
    defaults. `set_missing_values` fetches the supplier's default
    tax_withholding_category, overwriting the explicit CSV value.
    Fix: TDS fields are now set AFTER set_missing_values in both
    receipt-based and direct invoice creation flows.

ENHANCEMENT (2026-02-25): Added Posting Date column to both receipt-based
    and direct CSV flows. Previously, posting date defaulted to today.
    Now users can specify a posting date per invoice row. Validation
    checks accounting period closure for the specified posting date.
    Defaults to today if left blank in CSV.

Author: Bonito Designs Tech Team
"""

import frappe
from frappe import _
import csv
from datetime import datetime
from io import StringIO

# Import ERPNext's native method for receipt-based flow
from erpnext.stock.doctype.purchase_receipt.purchase_receipt import make_purchase_invoice


# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_CREDIT_TO = "Creditors - BDPL"

MODE_RECEIPT = "From Purchase Receipt"
MODE_DIRECT = "Direct (No Receipt)"


# ══════════════════════════════════════════════════════════════════════════════
#  BATCH CACHE — eliminates redundant DB queries within a single batch run
# ══════════════════════════════════════════════════════════════════════════════

class BatchCache:
    """
    Per-batch cache that avoids repeated identical DB queries.
    Created fresh for each validate or create operation.
    Typical savings: 60-80% fewer queries when rows share suppliers,
    cost centers, tax templates, and posting dates.
    """

    def __init__(self):
        self._exists = {}            # (doctype, name) -> bool
        self._period_closed = {}     # posting_date_str -> bool
        self._supplier_invoice = {}  # (supplier, bill_no) -> pi_name or None
        self._pi_from_pr = {}        # pr_name -> pi_name or None
        self._tax_template_docs = {} # template_name -> doc

    def exists(self, doctype, name):
        """Cached frappe.db.exists — avoids repeated lookups for same Supplier, Item, etc."""
        key = (doctype, name)
        if key not in self._exists:
            self._exists[key] = bool(frappe.db.exists(doctype, name))
        return self._exists[key]

    def is_accounting_period_closed(self, posting_date, company="Bonito Designs Pvt Ltd"):
        """Cached accounting period check — same date always returns same result within a batch."""
        key = str(posting_date)
        if key not in self._period_closed:
            self._period_closed[key] = _raw_check_accounting_period_closed(key, company)
        return self._period_closed[key]

    def is_duplicate_supplier_invoice(self, supplier, bill_no, exclude_name=None):
        """Cached duplicate supplier invoice check for validation phase."""
        key = (supplier, bill_no)
        if key not in self._supplier_invoice:
            self._supplier_invoice[key] = _raw_check_duplicate_supplier_invoice(
                supplier, bill_no, exclude_name
            )
        return self._supplier_invoice[key]

    def check_pi_exists_for_pr(self, pr_name):
        """Cached check: does a PI already exist for this Purchase Receipt?"""
        if pr_name not in self._pi_from_pr:
            self._pi_from_pr[pr_name] = _raw_check_pi_exists(pr_name)
        return self._pi_from_pr[pr_name]

    def get_tax_template_doc(self, template_name):
        """Cached tax template document — avoids re-fetching the same template per invoice."""
        if template_name not in self._tax_template_docs:
            self._tax_template_docs[template_name] = frappe.get_doc(
                "Purchase Taxes and Charges Template", template_name
            )
        return self._tax_template_docs[template_name]


# ── Raw DB query functions (used by BatchCache) ──────────────────────────────

def _raw_check_accounting_period_closed(posting_date, company):
    """Raw DB check — is the accounting period closed for PI on this date?"""
    closed_periods = frappe.get_all(
        "Accounting Period",
        filters={
            "company": company,
            "start_date": ["<=", posting_date],
            "end_date": [">=", posting_date],
        },
        fields=["name"],
    )
    for period in closed_periods:
        closed_docs = frappe.get_all(
            "Closed Document",
            filters={
                "parent": period.name,
                "document_type": "Purchase Invoice",
                "closed": 1,
            },
        )
        if closed_docs:
            return True
    return False


def _raw_check_duplicate_supplier_invoice(supplier, bill_no, exclude_name=None):
    """Raw DB check — does a PI with this supplier + bill_no already exist?"""
    filters = {
        "bill_no": bill_no,
        "supplier": supplier,
        "docstatus": ["!=", 2],
    }
    if exclude_name:
        filters["name"] = ["!=", exclude_name]
    existing = frappe.get_all("Purchase Invoice", filters=filters, fields=["name"])
    return existing[0].name if existing else None


def _raw_check_pi_exists(pr_name):
    """Raw DB check — does a PI exist for this Purchase Receipt?"""
    existing = frappe.get_all(
        "Purchase Invoice Item",
        filters={"purchase_receipt": pr_name, "docstatus": ["!=", 2]},
        fields=["parent"],
        limit=1,
    )
    return existing[0].parent if existing else None


# ── Shared Helpers ────────────────────────────────────────────────────────────

def parse_date(date_str):
    """Parse date string supporting multiple formats. Returns YYYY-MM-DD or None."""
    if not date_str:
        return None
    date_str = date_str.strip()
    for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%m/%d/%Y"]:
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_amount(val):
    """Parse an amount string, handling commas and blanks."""
    if not val:
        return 0.0
    val = str(val).strip().replace(",", "")
    try:
        return float(val)
    except ValueError:
        return 0.0


# ── before_save Hook ──────────────────────────────────────────────────────────

def before_save(doc, method=None):
    """Prevent modification of attachment on submitted documents."""
    if doc.docstatus == 1:
        old_doc = doc.get_doc_before_save()
        if old_doc and old_doc.csv_file != doc.csv_file:
            frappe.throw(_("Cannot modify CSV attachment on a submitted document"))


# ══════════════════════════════════════════════════════════════════════════════
#  CSV LOADING
# ══════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def load_csv_to_items(doc_name):
    """Parse CSV and populate child tables. Dispatches based on mode."""
    doc = frappe.get_doc("Bulk Purchase Invoice Creation", doc_name)

    if not doc.csv_file:
        frappe.throw(_("Please upload a CSV file first"))

    file_doc = frappe.get_doc("File", {"file_url": doc.csv_file})
    content_raw = file_doc.get_content()
    csv_content = content_raw.decode("utf-8-sig") if isinstance(content_raw, bytes) else content_raw

    if doc.mode == MODE_DIRECT:
        return _load_csv_direct(doc, csv_content)
    else:
        return _load_csv_receipt(doc, csv_content)


def _load_csv_receipt(doc, csv_content):
    """Load CSV for receipt-based mode. Batch-fetches suppliers for all PRs in one query."""
    reader = csv.DictReader(StringIO(csv_content))
    doc.items = []

    # First pass: collect all rows and PR numbers
    rows = []
    pr_numbers = set()
    for row in reader:
        pr_no = (row.get("Purchase Receipt No") or "").strip()
        if not pr_no:
            continue
        rows.append(row)
        pr_numbers.add(pr_no)

    # Batch fetch suppliers for all PRs in ONE query instead of N queries
    supplier_map = {}
    if pr_numbers:
        pr_list = frappe.get_all(
            "Purchase Receipt",
            filters={"name": ["in", list(pr_numbers)]},
            fields=["name", "supplier"],
        )
        supplier_map = {pr.name: pr.supplier for pr in pr_list}

    today = frappe.utils.today()
    count = 0
    for row in rows:
        pr_no = (row.get("Purchase Receipt No") or "").strip()
        posting_date = parse_date(row.get("Posting Date")) or today
        doc.append("items", {
            "purchase_receipt": pr_no,
            "supplier": supplier_map.get(pr_no, ""),
            "supplier_invoice_no": (row.get("Supplier Invoice No") or "").strip(),
            "supplier_invoice_date": parse_date(row.get("Supplier Invoice Date")),
            "posting_date": posting_date,
            "tax_template": (row.get("Tax Template") or "").strip(),
            "tds_category": (row.get("TDS") or "").strip(),
            "remarks": (row.get("Remarks") or "").strip(),
            "row_status": "Pending",
        })
        count += 1

    doc.save()
    return {"invoices": count, "total_items": count}


def _load_csv_direct(doc, csv_content):
    """
    Load CSV for direct mode with header+continuation pattern.

    CSV columns (header row):
        Supplier, Supplier Invoice No, Supplier Invoice Date, Posting Date,
        Cost Center, Project, Price List, Tax Template, TDS, Remarks,
        Item Code, Expense Head, Accepted Qty, Rate

    - Posting Date: defaults to today if blank
    - Department: auto-fetched from Supplier master (not in CSV)
    - Continuation rows (no Supplier) inherit the current invoice header.
    """
    reader = csv.DictReader(StringIO(csv_content))

    doc.direct_invoices = []
    doc.direct_invoice_items = []

    rows = list(reader)

    # Batch fetch departments from supplier masters
    supplier_names = set()
    for row in rows:
        s = (row.get("Supplier") or "").strip()
        if s:
            supplier_names.add(s)

    dept_map = {}
    if supplier_names:
        supplier_docs = frappe.get_all(
            "Supplier",
            filters={"name": ["in", list(supplier_names)]},
            fields=["name", "department"],
        )
        dept_map = {s.name: s.department for s in supplier_docs if s.department}

    current_invoice = None
    inv_count = 0
    item_count = 0
    today = frappe.utils.today()

    for row in rows:
        supplier = (row.get("Supplier") or "").strip()
        item_code = (row.get("Item Code") or "").strip()

        if supplier:
            posting_date = parse_date(row.get("Posting Date")) or today
            inv_row = doc.append("direct_invoices", {
                "supplier": supplier,
                "supplier_invoice_no": (row.get("Supplier Invoice No") or "").strip(),
                "supplier_invoice_date": parse_date(row.get("Supplier Invoice Date")),
                "posting_date": posting_date,
                "cost_center": (row.get("Cost Center") or "").strip(),
                "department": dept_map.get(supplier, ""),
                "project": (row.get("Project") or "").strip(),
                "price_list": (row.get("Price List") or "").strip(),
                "tax_template": (row.get("Tax Template") or "").strip(),
                "tds_category": (row.get("TDS") or "").strip(),
                "remarks": (row.get("Remarks") or "").strip(),
                "row_status": "Pending",
            })
            current_invoice = inv_row
            inv_count += 1

            if item_code:
                doc.append("direct_invoice_items", {
                    "invoice_idx": inv_row.idx,
                    "item_code": item_code,
                    "expense_head": (row.get("Expense Head") or "").strip(),
                    "qty": parse_amount(row.get("Accepted Qty")),
                    "rate": parse_amount(row.get("Rate")),
                })
                item_count += 1

        elif item_code and current_invoice:
            doc.append("direct_invoice_items", {
                "invoice_idx": current_invoice.idx,
                "item_code": item_code,
                "expense_head": (row.get("Expense Head") or "").strip(),
                "qty": parse_amount(row.get("Accepted Qty")),
                "rate": parse_amount(row.get("Rate")),
            })
            item_count += 1

    doc.save()
    return {"invoices": inv_count, "total_items": item_count}


# ══════════════════════════════════════════════════════════════════════════════
#  VALIDATION
# ══════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def validate_items(doc_name):
    """Validate items. Creates a fresh BatchCache for the entire validation pass."""
    doc = frappe.get_doc("Bulk Purchase Invoice Creation", doc_name)
    cache = BatchCache()

    if doc.mode == MODE_DIRECT:
        return _validate_direct(doc, cache)
    else:
        return _validate_receipt(doc, cache)


def _validate_receipt(doc, cache):
    """Validate receipt-based items with caching."""
    valid_count = 0
    invalid_count = 0
    today = frappe.utils.today()

    for item in doc.items:
        errors = []
        pr_name = item.purchase_receipt

        if not cache.exists("Purchase Receipt", pr_name):
            errors.append("Purchase Receipt not found")
        else:
            pr_status = frappe.get_value("Purchase Receipt", pr_name, "docstatus")
            if pr_status != 1:
                errors.append("Purchase Receipt is not submitted")

            existing_pi = cache.check_pi_exists_for_pr(pr_name)
            if existing_pi:
                item.purchase_invoice = existing_pi
                errors.append(f"Purchase Invoice {existing_pi} already exists")

        if not item.supplier_invoice_no:
            errors.append("Missing supplier invoice number")

        if not item.supplier_invoice_date:
            errors.append("Missing supplier invoice date")

        if not item.supplier:
            if cache.exists("Purchase Receipt", pr_name):
                item.supplier = frappe.get_value("Purchase Receipt", pr_name, "supplier")
            else:
                errors.append("Supplier could not be determined")

        # Default posting_date to today if blank
        if not item.posting_date:
            item.posting_date = today

        # Accounting period closed check (cached per posting date)
        if item.posting_date and cache.is_accounting_period_closed(str(item.posting_date)):
            errors.append(f"Accounting period is closed for posting date {item.posting_date}")

        if errors:
            item.row_status = "Failed"
            item.error_message = "; ".join(errors)
            invalid_count += 1
        else:
            item.row_status = "Valid"
            item.error_message = None
            valid_count += 1

    doc.save()
    return {"total": len(doc.items), "valid": valid_count, "invalid": invalid_count}


def _validate_direct(doc, cache):
    """Validate direct (no receipt) items with caching."""
    valid_count = 0
    invalid_count = 0

    for inv in doc.direct_invoices:
        errors = []

        # 1. Supplier
        if not inv.supplier:
            errors.append("Supplier is required")
        elif not cache.exists("Supplier", inv.supplier):
            errors.append(f"Supplier '{inv.supplier}' not found")

        # 2. Supplier Invoice No
        if not inv.supplier_invoice_no:
            errors.append("Supplier Invoice No is required")

        # 3. Supplier Invoice Date
        if not inv.supplier_invoice_date:
            errors.append("Supplier Invoice Date is required or has invalid format")

        # 4. Posting Date defaults to today — always valid, skip check

        # 5. Accounting period closed check (cached per posting date)
        if inv.posting_date and cache.is_accounting_period_closed(str(inv.posting_date)):
            errors.append(f"Accounting period is closed for posting date {inv.posting_date}")

        # 6. Duplicate supplier invoice in existing PIs (cached per supplier+bill_no)
        if inv.supplier and inv.supplier_invoice_no:
            dup = cache.is_duplicate_supplier_invoice(inv.supplier, inv.supplier_invoice_no)
            if dup:
                errors.append(
                    f"Supplier Invoice No '{inv.supplier_invoice_no}' already exists "
                    f"in Purchase Invoice {dup}"
                )

        # 7. Duplicate within batch (Python scan — fine for typical batch sizes)
        if inv.supplier and inv.supplier_invoice_no:
            dups_in_batch = [
                r for r in doc.direct_invoices
                if r.idx != inv.idx
                and r.supplier == inv.supplier
                and r.supplier_invoice_no == inv.supplier_invoice_no
            ]
            if dups_in_batch:
                errors.append(
                    f"Duplicate Supplier Invoice No '{inv.supplier_invoice_no}' "
                    f"for supplier '{inv.supplier}' within this batch"
                )

        # 8-12. Entity existence checks (all cached)
        if inv.cost_center and not cache.exists("Cost Center", inv.cost_center):
            errors.append(f"Cost Center '{inv.cost_center}' not found")

        if inv.department and not cache.exists("Department", inv.department):
            errors.append(f"Department '{inv.department}' not found")

        if inv.project and not cache.exists("Project", inv.project):
            errors.append(f"Project '{inv.project}' not found")

        if inv.tax_template:
            if not cache.exists("Purchase Taxes and Charges Template", inv.tax_template):
                errors.append(f"Tax Template '{inv.tax_template}' not found")

        if inv.tds_category:
            if not cache.exists("Tax Withholding Category", inv.tds_category):
                errors.append(f"TDS Category '{inv.tds_category}' not found")

        if inv.price_list and not cache.exists("Price List", inv.price_list):
            errors.append(f"Price List '{inv.price_list}' not found")

        # 13. Items validation (item & expense head existence cached)
        linked_items = [i for i in doc.direct_invoice_items if i.invoice_idx == inv.idx]
        if not linked_items:
            errors.append("No items found for this invoice")
        else:
            for item in linked_items:
                if not item.item_code:
                    errors.append("Item Code is required for all items")
                elif not cache.exists("Item", item.item_code):
                    errors.append(f"Item '{item.item_code}' not found")
                if not item.qty or item.qty <= 0:
                    errors.append(f"Qty must be > 0 for item '{item.item_code}'")
                if not item.rate or item.rate <= 0:
                    errors.append(f"Rate must be > 0 for item '{item.item_code}'")
                if item.expense_head:
                    if not cache.exists("Account", item.expense_head):
                        errors.append(f"Expense Head '{item.expense_head}' not found")
        if errors:
            inv.row_status = "Failed"
            inv.error_message = "; ".join(errors)
            invalid_count += 1
        else:
            inv.row_status = "Valid"
            inv.error_message = None
            valid_count += 1

    doc.save()
    return {"total": len(doc.direct_invoices), "valid": valid_count, "invalid": invalid_count}


# ══════════════════════════════════════════════════════════════════════════════
#  INVOICE CREATION
# ══════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def start_invoice_creation(doc_name):
    """Start invoice creation as a background job. Dispatches based on mode."""
    doc = frappe.get_doc("Bulk Purchase Invoice Creation", doc_name)

    if doc.mode == MODE_DIRECT:
        valid_items = [i for i in doc.direct_invoices if i.row_status == "Valid"]
    else:
        valid_items = [i for i in doc.items if i.row_status == "Valid"]

    if not valid_items:
        frappe.throw(_("No valid items to process. Run validation first."))

    doc.processing_status = "In Progress"
    doc.processed_count = 0
    doc.success_count = 0
    doc.failed_count = 0
    doc.skipped_count = 0
    doc.current_item = ""
    doc.save()
    frappe.db.commit()

    frappe.enqueue(
        "bonito_customizations.bulk_pi_native.create_invoices_background",
        doc_name=doc_name,
        queue="long",
        timeout=3600,
    )

    return {"status": "started", "total": len(valid_items)}


def create_invoices_background(doc_name):
    """Background job: creates a fresh BatchCache and dispatches based on mode."""
    doc = frappe.get_doc("Bulk Purchase Invoice Creation", doc_name)
    cache = BatchCache()

    if doc.mode == MODE_DIRECT:
        _create_invoices_direct(doc, cache)
    else:
        _create_invoices_receipt(doc, cache)


# ── Receipt-based creation ────────────────────────────────────────────────────

def _create_invoices_receipt(doc, cache):
    """Create Purchase Invoices from Purchase Receipts."""
    valid_items = [i for i in doc.items if i.row_status == "Valid"]
    processed = success = failed = skipped = 0
    today = frappe.utils.today()

    for item in valid_items:
        try:
            doc.db_set("current_item", item.purchase_receipt, update_modified=False)

            # Fresh check — not cached, because we're creating PIs in this loop
            existing_pi = _raw_check_pi_exists(item.purchase_receipt)
            if existing_pi:
                item.db_set("row_status", "Skipped", update_modified=False)
                item.db_set("error_message", f"PI {existing_pi} already exists", update_modified=False)
                item.db_set("purchase_invoice", existing_pi, update_modified=False)
                skipped += 1
                processed += 1
                _update_progress(doc, processed, success, failed, skipped)
                continue

            # Fresh accounting period check at creation time
            posting_date = str(item.posting_date) if item.posting_date else today
            if _raw_check_accounting_period_closed(posting_date, "Bonito Designs Pvt Ltd"):
                item.db_set("row_status", "Failed", update_modified=False)
                item.db_set(
                    "error_message",
                    f"Accounting period is closed for posting date {posting_date}",
                    update_modified=False,
                )
                failed += 1
                processed += 1
                _update_progress(doc, processed, success, failed, skipped)
                continue

            pi = make_purchase_invoice(item.purchase_receipt)
            pi_doc = frappe.get_doc(pi)

            pi_doc.bill_no = item.supplier_invoice_no
            pi_doc.bill_date = item.supplier_invoice_date

            # Set posting date from CSV (or default to today)
            pi_doc.posting_date = posting_date
            pi_doc.set_posting_time = 1
            pi_doc.posting_time = "00:00:00"

            # Tax template (cached doc fetch — same template reused across invoices)
            if item.tax_template:
                pi_doc.taxes_and_charges = item.tax_template
                tax_tmpl = cache.get_tax_template_doc(item.tax_template)
                pi_doc.taxes = []
                for tax in tax_tmpl.taxes:
                    pi_doc.append("taxes", {
                        "charge_type": tax.charge_type,
                        "account_head": tax.account_head,
                        "description": tax.description,
                        "rate": tax.rate,
                        "cost_center": tax.cost_center,
                    })

            if item.remarks:
                pi_doc.remarks = item.remarks

            # Let Frappe populate defaults (this fetches supplier's default TDS
            # via fetch_from, which is why we set our TDS *after* this call)
            pi_doc.run_method("set_missing_values")

            # FIX: Set TDS AFTER set_missing_values to prevent supplier's default
            # tax_withholding_category from overwriting the CSV-specified value.
            # set_missing_values triggers fetch_from on the supplier field which
            # pulls the supplier master's default TDS — we override it here.
            if item.tds_category:
                pi_doc.apply_tds = 1
                pi_doc.tax_withholding_category = item.tds_category

            pi_doc.run_method("calculate_taxes_and_totals")
            pi_doc.insert()

            item.db_set("purchase_invoice", pi_doc.name, update_modified=False)
            item.db_set("row_status", "Created", update_modified=False)
            item.db_set("error_message", None, update_modified=False)
            success += 1

        except Exception as e:
            frappe.db.rollback()
            item.db_set("row_status", "Failed", update_modified=False)
            item.db_set("error_message", str(e)[:500], update_modified=False)
            failed += 1

        processed += 1
        _update_progress(doc, processed, success, failed, skipped)
        frappe.db.commit()

    doc.db_set("processing_status", "Completed", update_modified=False)
    doc.db_set("current_item", "", update_modified=False)

    # Auto-submit the bulk document if at least one invoice was created
    if success > 0:
        try:
            doc.reload()
            doc.submit()
        except Exception:
            pass

    frappe.db.commit()


# ── Direct (no receipt) creation ──────────────────────────────────────────────

def _create_invoices_direct(doc, cache):
    """Create Purchase Invoices directly without Purchase Receipts."""
    valid_invoices = [i for i in doc.direct_invoices if i.row_status == "Valid"]
    processed = success = failed = skipped = 0

    for inv in valid_invoices:
        try:
            doc.db_set(
                "current_item",
                f"{inv.supplier} / {inv.supplier_invoice_no}",
                update_modified=False,
            )

            # Fresh duplicate check — NOT cached during creation loop since we're
            # creating new PIs that would make the cache stale
            dup = _raw_check_duplicate_supplier_invoice(inv.supplier, inv.supplier_invoice_no)
            if dup:
                inv.db_set("row_status", "Skipped", update_modified=False)
                inv.db_set(
                    "error_message",
                    f"Purchase Invoice {dup} already exists with this supplier invoice no",
                    update_modified=False,
                )
                inv.db_set("purchase_invoice", dup, update_modified=False)
                skipped += 1
                processed += 1
                _update_progress(doc, processed, success, failed, skipped)
                continue

            # Accounting period (safe to cache — doesn't change mid-batch)
            if cache.is_accounting_period_closed(str(inv.posting_date)):
                inv.db_set("row_status", "Failed", update_modified=False)
                inv.db_set(
                    "error_message",
                    f"Accounting period is closed for {inv.posting_date}",
                    update_modified=False,
                )
                failed += 1
                processed += 1
                _update_progress(doc, processed, success, failed, skipped)
                continue

            # Build PI (uses cached tax template doc)
            pi = _build_direct_purchase_invoice(doc, inv, cache)
            pi.insert()

            inv.db_set("purchase_invoice", pi.name, update_modified=False)
            inv.db_set("row_status", "Created", update_modified=False)
            inv.db_set("error_message", None, update_modified=False)
            success += 1

        except Exception as e:
            frappe.db.rollback()
            inv.db_set("row_status", "Failed", update_modified=False)
            inv.db_set("error_message", str(e)[:500], update_modified=False)
            failed += 1

        processed += 1
        _update_progress(doc, processed, success, failed, skipped)
        frappe.db.commit()

    doc.db_set("processing_status", "Completed", update_modified=False)
    doc.db_set("current_item", "", update_modified=False)

    # Auto-submit the bulk document if at least one invoice was created
    if success > 0:
        try:
            doc.reload()
            doc.submit()
        except Exception:
            pass

    frappe.db.commit()


def _build_direct_purchase_invoice(parent_doc, inv_row, cache):
    """Build a Purchase Invoice document for direct mode."""
    linked_items = [
        i for i in parent_doc.direct_invoice_items if i.invoice_idx == inv_row.idx
    ]

    pi = frappe.new_doc("Purchase Invoice")
    pi.supplier = inv_row.supplier
    pi.bill_no = inv_row.supplier_invoice_no
    pi.bill_date = inv_row.supplier_invoice_date
    pi.posting_date = inv_row.posting_date or frappe.utils.today()
    pi.set_posting_time = 1
    pi.posting_time = "00:00:00"
    pi.credit_to = DEFAULT_CREDIT_TO
    pi.update_stock = 0

    if inv_row.cost_center:
        pi.cost_center = inv_row.cost_center
    if inv_row.department:
        pi.department = inv_row.department
    if inv_row.project:
        pi.project = inv_row.project
    if inv_row.price_list:
        pi.buying_price_list = inv_row.price_list
    if inv_row.remarks:
        pi.remarks = inv_row.remarks

    for item in linked_items:
        item_row = {
            "item_code": item.item_code,
            "qty": item.qty,
            "rate": item.rate,
            "cost_center": inv_row.cost_center or "",
            "department": inv_row.department or "",
            "project": inv_row.project or "",
        }
        # Expense Head overrides the default expense account on the item
        if item.expense_head:
            item_row["expense_account"] = item.expense_head
        pi.append("items", item_row)

    # Apply tax template (cached doc — fetched once, reused for all invoices with same template)
    if inv_row.tax_template:
        pi.taxes_and_charges = inv_row.tax_template
        tax_template = cache.get_tax_template_doc(inv_row.tax_template)
        pi.taxes = []
        for tax in tax_template.taxes:
            pi.append("taxes", {
                "charge_type": tax.charge_type,
                "account_head": tax.account_head,
                "description": tax.description,
                "rate": tax.rate,
                "cost_center": inv_row.cost_center or tax.cost_center,
            })

    # Let Frappe populate defaults (this fetches supplier's default TDS
    # via fetch_from, which is why we set our TDS *after* this call)
    pi.run_method("set_missing_values")

    # FIX: Set TDS AFTER set_missing_values to prevent supplier's default
    # tax_withholding_category from overwriting the CSV-specified value.
    # set_missing_values triggers fetch_from on the supplier field which
    # pulls the supplier master's default TDS — we override it here.
    if inv_row.tds_category:
        pi.apply_tds = 1
        pi.tax_withholding_category = inv_row.tds_category

    pi.run_method("calculate_taxes_and_totals")

    return pi


# ── Progress Helpers ──────────────────────────────────────────────────────────

def _update_progress(doc, processed, success, failed, skipped):
    """Update progress fields via db_set (bypasses ORM, no Version doc created)."""
    doc.db_set("processed_count", processed, update_modified=False)
    doc.db_set("success_count", success, update_modified=False)
    doc.db_set("failed_count", failed, update_modified=False)
    doc.db_set("skipped_count", skipped, update_modified=False)


@frappe.whitelist()
def get_creation_progress(doc_name):
    """Return current progress for frontend polling."""
    doc = frappe.get_doc("Bulk Purchase Invoice Creation", doc_name)

    if doc.mode == MODE_DIRECT:
        items = doc.direct_invoices
    else:
        items = doc.items

    total_valid = len([
        i for i in items
        if i.row_status in ("Valid", "Created", "Skipped", "Failed")
    ])

    return {
        "status": doc.processing_status or "Not Started",
        "processed": doc.processed_count or 0,
        "total": total_valid,
        "success": doc.success_count or 0,
        "failed": doc.failed_count or 0,
        "skipped": doc.skipped_count or 0,
        "current_item": doc.current_item or "",
        "complete": doc.processing_status in ("Completed", "Failed"),
    }


@frappe.whitelist()
def resume_invoice_creation(doc_name):
    """Resume processing for items still in Valid state."""
    doc = frappe.get_doc("Bulk Purchase Invoice Creation", doc_name)

    if doc.mode == MODE_DIRECT:
        pending = len([i for i in doc.direct_invoices if i.row_status == "Valid"])
    else:
        pending = len([i for i in doc.items if i.row_status == "Valid"])

    if pending == 0:
        frappe.throw(_("No pending items to process"))

    doc.db_set("processing_status", "In Progress", update_modified=False)
    frappe.db.commit()

    frappe.enqueue(
        "bonito_customizations.bulk_pi_native.create_invoices_background",
        doc_name=doc_name,
        queue="long",
        timeout=3600,
    )

    return {"status": "resumed", "total": pending}


# ── CSV Templates ─────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_csv_template(mode=None):
    """Return sample CSV content based on mode."""
    if mode == MODE_DIRECT:
        return """Supplier,Supplier Invoice No,Supplier Invoice Date,Posting Date,Cost Center,Project,Price List,Tax Template,TDS,Remarks,Item Code,Expense Head,Accepted Qty,Rate
BESCOM,ELEC-2025-001,2025-01-15,2025-01-20,Main - BDPL,,Standard Buying,Input Tax GST,,Electricity bill Jan,Electricity Charges,Electricity Expenses - BDPL,1,15000
,,,,,,,,,,,Office Supplies,,5,200
Anthropic,CLAUDE-2025-JAN,2025-01-31,2025-02-05,IT - BDPL,,Standard Buying,Input Tax GST - RCM,TDS - 194C - Individual,Claude API Jan,Software Subscription,Software Expenses - BDPL,1,50000
AWS India,AWS-2025-JAN,2025-01-31,,IT - BDPL,,Standard Buying,Input Tax GST - RCM,,AWS hosting Jan,Cloud Hosting Services,,1,75000
,,,,,,,,,,,Data Transfer Charges,,1,5000"""
    else:
        return """Purchase Receipt No,Supplier Invoice No,Supplier Invoice Date,Posting Date,Tax Template,TDS,Remarks
MAT-PRE-2024-00001,INV-001,2024-01-15,2024-01-20,Input Tax GST,TDS - 194C - Individual,Office supplies
MAT-PRE-2024-00002,INV-002,15-01-2024,,Input Tax GST,,Furniture purchase
MAT-PRE-2024-00003,INV-003,2024-01-17,2024-01-20,,,Raw materials"""


# ══════════════════════════════════════════════════════════════════════════════
#  BACKWARD COMPATIBILITY — old client scripts call parse_csv_to_items
# ══════════════════════════════════════════════════════════════════════════════

@frappe.whitelist()
def parse_csv_to_items(file_url=None):
    """
    Backward-compatible: old client script calls this with file_url,
    expects a list of dicts back, and populates child table client-side.
    Only works for receipt-based mode (original flow).
    """
    if not file_url:
        frappe.throw(_("No file URL provided"))

    file_doc = frappe.get_doc("File", {"file_url": file_url})
    content = file_doc.get_content()
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")

    reader = csv.DictReader(StringIO(content))

    # Batch fetch suppliers
    rows = list(reader)
    pr_numbers = set()
    for row in rows:
        pr_no = (row.get("Purchase Receipt No") or "").strip()
        if pr_no:
            pr_numbers.add(pr_no)

    supplier_map = {}
    if pr_numbers:
        pr_list = frappe.get_all(
            "Purchase Receipt",
            filters={"name": ["in", list(pr_numbers)]},
            fields=["name", "supplier"],
        )
        supplier_map = {pr.name: pr.supplier for pr in pr_list}

    # Batch fetch TDS categories from supplier masters
    unique_suppliers = set(supplier_map.values())
    tds_map = {}
    if unique_suppliers:
        supplier_tds = frappe.get_all(
            "Supplier",
            filters={"name": ["in", list(unique_suppliers)]},
            fields=["name", "tax_withholding_category"],
        )
        tds_map = {s.name: s.tax_withholding_category for s in supplier_tds if s.tax_withholding_category}

    items = []
    today = frappe.utils.today()
    for row in rows:
        pr_no = (row.get("Purchase Receipt No") or "").strip()
        if not pr_no:
            continue

        bill_date = (row.get("Supplier Invoice Date") or "").strip()
        supplier = supplier_map.get(pr_no, "")

        # TDS: use CSV value if provided, else auto-fetch from supplier master
        tds_from_csv = (row.get("TDS") or "").strip()
        tds_category = tds_from_csv if tds_from_csv else tds_map.get(supplier, "")

        # Posting Date: use CSV value if provided, else default to today
        posting_date = parse_date(row.get("Posting Date")) or today

        items.append({
            "purchase_receipt": pr_no,
            "supplier": supplier,
            "supplier_invoice_no": (row.get("Supplier Invoice No") or "").strip(),
            "supplier_invoice_date": parse_date(bill_date) if bill_date else None,
            "posting_date": posting_date,
            "tax_template": (row.get("Tax Template") or "").strip() or None,
            "tds_category": tds_category or None,
            "remarks": (row.get("Remarks") or "").strip() or None,
            "row_status": "Pending",
        })

    return items


# ══════════════════════════════════════════════════════════════════════════════
#  Unlink invoices from cancelled bulk pinv documents
# ══════════════════════════════════════════════════════════════════════════════
@frappe.whitelist()
def unlink_invoice_from_cancelled(bulk_doc_name, pinv_name, also_delete=0):
    also_delete = int(also_delete)

    if not frappe.has_permission("Bulk Purchase Invoice Creation", "cancel"):
        frappe.throw(_("You need Cancel permission on Bulk Purchase Invoice Creation"))

    docstatus = frappe.db.get_value("Bulk Purchase Invoice Creation", bulk_doc_name, "docstatus")
    if docstatus is None:
        frappe.throw(_(f"Bulk document {bulk_doc_name} not found"))
    if docstatus != 2:
        frappe.throw(_(f"Bulk document {bulk_doc_name} is not cancelled"))

    if not frappe.db.exists("Purchase Invoice", pinv_name):
        frappe.throw(_(f"Purchase Invoice {pinv_name} not found"))

    # Unlink from BOTH child tables
    total_affected = 0
    for child_table in ["Bulk Purchase Invoice Creation Item", "Bulk PI Direct Invoice"]:
        frappe.db.sql("""
            UPDATE `tab{table}`
            SET purchase_invoice = NULL, row_status = 'Unlinked'
            WHERE parent = %s AND purchase_invoice = %s
        """.format(table=child_table), (bulk_doc_name, pinv_name))
        total_affected += frappe.db.sql("SELECT ROW_COUNT()")[0][0]

    frappe.db.commit()

    result = {"unlinked": True, "rows_affected": total_affected}

    if also_delete:
        pinv = frappe.get_doc("Purchase Invoice", pinv_name)

        if pinv.docstatus == 1:
            pinv.cancel()
            frappe.db.commit()
            result["cancelled"] = True

        # Check for other back-links before deleting
        other_links = []
        for child_table in ["Bulk Purchase Invoice Creation Item", "Bulk PI Direct Invoice"]:
            links = frappe.db.sql("""
                SELECT parent FROM `tab{table}`
                WHERE purchase_invoice = %s AND parent != %s
            """.format(table=child_table), (pinv_name, bulk_doc_name))
            other_links.extend(links)

        if other_links:
            result["deleted"] = False
            result["warning"] = f"PINV is also linked in: {', '.join([r[0] for r in other_links])}"
        else:
            frappe.delete_doc("Purchase Invoice", pinv_name, force=True)
            frappe.db.commit()
            result["deleted"] = True

    return result

@frappe.whitelist()
def get_linked_invoices(bulk_doc_name):
    """Fetch linked PINVs from both receipt-based and direct invoice child tables."""
    if not frappe.has_permission("Bulk Purchase Invoice Creation", "cancel"):
        frappe.throw(_("Insufficient permissions"))

    results = []

    # Check receipt-based child table
    rows = frappe.db.sql("""
        SELECT name, idx, purchase_invoice, purchase_receipt as reference, supplier,
            'items' as source_table
        FROM `tabBulk Purchase Invoice Creation Item`
        WHERE parent = %s
        AND purchase_invoice IS NOT NULL
        AND purchase_invoice != ''
    """, bulk_doc_name, as_dict=True)
    results.extend(rows)

    # Check direct invoice child table
    rows = frappe.db.sql("""
        SELECT name, idx, purchase_invoice, supplier_invoice_no as reference, supplier,
            'direct_invoices' as source_table
        FROM `tabBulk PI Direct Invoice`
        WHERE parent = %s
        AND purchase_invoice IS NOT NULL
        AND purchase_invoice != ''
    """, bulk_doc_name, as_dict=True)
    results.extend(rows)

    return results


def on_submit(doc, method=None):
    """Called on submit via hooks.py doc_events."""
    # Auto-unlink removed invoices if this is an amendment
    if doc.amended_from:
        unlink_removed_invoices_from_parent(doc)


def unlink_removed_invoices_from_parent(doc):
    """
    Compare amended doc's child tables with cancelled parent.
    Any PINV that exists in parent but not in amended doc gets unlinked.
    """
    cancelled_name = doc.amended_from
    
    for child_table in ["Bulk Purchase Invoice Creation Item", "Bulk PI Direct Invoice"]:
        # Get PINVs linked in cancelled parent
        parent_pinvs = frappe.db.sql("""
            SELECT name, purchase_invoice
            FROM `tab{table}`
            WHERE parent = %s
            AND purchase_invoice IS NOT NULL
            AND purchase_invoice != ''
        """.format(table=child_table), cancelled_name, as_dict=True)
        
        if not parent_pinvs:
            continue
        
        # Get PINVs in the current (amended) doc
        current_pinvs = set(
            frappe.db.sql_list("""
                SELECT purchase_invoice
                FROM `tab{table}`
                WHERE parent = %s
                AND purchase_invoice IS NOT NULL
                AND purchase_invoice != ''
            """.format(table=child_table), doc.name)
        )
        
        # Unlink any PINV that was in parent but removed from amendment
        for row in parent_pinvs:
            if row.purchase_invoice not in current_pinvs:
                frappe.db.sql("""
                    UPDATE `tab{table}`
                    SET purchase_invoice = NULL, row_status = 'Unlinked'
                    WHERE name = %s
                """.format(table=child_table), row.name)
                
                frappe.log_error(
                    title="Auto-unlinked PINV from cancelled bulk doc",
                    message=f"Unlinked {row.purchase_invoice} from {cancelled_name} "
                            f"(amended to {doc.name})"
                )
    
    frappe.db.commit()

