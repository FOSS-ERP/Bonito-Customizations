"""
Bulk Payment Entry Creation from Purchase Invoices
====================================================
Creates Payment Entries from CSV with optional Purchase Invoice references.

CSV Columns:
    Party Type, Party, Account Paid From, Paid Amount, Mode of Payment,
    Posting Date, Name, Cheque_or_reference_no, Cheque_or_reference_date,
    Remarks, Proof Files

Three modes of operation:
  1. MANUAL — "Name" column has Purchase Invoice name(s).
     Continuation rows (Party empty, only Name filled) add more PI refs.
  2. AUTO-FIFO — "Name" column is empty.
     Outstanding invoices for the supplier are fetched in FIFO order
     (oldest first) and knocked off automatically until the paid amount
     is exhausted.  The last invoice can be partially allocated.
  3. ADVANCE — "Name" column contains "Advance" (case-insensitive).
     Creates a Payment Entry with NO invoice references — an advance
     payment to the supplier.  No GST deduction applies.

Proof Files:
  - Optional column with pipe-separated filenames: "receipt.pdf|bank_slip.jpg"
  - Upload a ZIP file containing all proof files to the "Proof Files ZIP" field
    on the Bulk Payment Entry Creation form.
  - During creation, matching files are extracted and attached to each PE.

Installation:
    Save as: apps/bonito_customizations/bonito_customizations/bulk_payment_entry_native.py

    Add to hooks.py:
    doc_events = {
        "Bulk Payment Entry Creation": {
            "before_save": "bonito_customizations.bulk_payment_entry_native.before_save",
        }
    }

Author: Bonito Designs Tech Team
"""

import frappe
from frappe import _
import csv
from datetime import datetime
from io import StringIO


# ── Constants ─────────────────────────────────────────────────────────────────

ADVANCE_KEYWORD = "advance"          # case-insensitive match in Name column
ADVANCE_MARKER  = "__ADVANCE__"      # internal marker stored in purchase_invoices field


# ── Helper Functions ──────────────────────────────────────────────────────────

def is_advance_marker(purchase_invoices_str):
    """Check if the purchase_invoices field indicates an advance payment."""
    return (purchase_invoices_str or "").strip() == ADVANCE_MARKER


def parse_date(date_str):
    """Parse date string supporting multiple formats. Returns YYYY-MM-DD or None."""
    if not date_str:
        return None
    date_str = date_str.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def get_file_path(file_url):
    """Resolve file URL to absolute path."""
    if file_url.startswith("http"):
        from urllib.parse import urlparse
        file_url = urlparse(file_url).path

    if file_url.startswith("/private/files/") or file_url.startswith("/files/"):
        import os
        return os.path.join(frappe.get_site_path(), file_url.lstrip("/"))
    return file_url


def get_zip_file_index(zip_file_url):
    """
    Open a ZIP file and return a dict mapping lowercase filename -> actual path in zip.
    Handles files inside subdirectories by indexing on just the basename.
    Returns empty dict if no zip file provided.
    """
    import zipfile, os

    if not zip_file_url:
        return {}

    zip_path = get_file_path(zip_file_url)
    if not os.path.exists(zip_path):
        return {}

    index = {}
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for entry in zf.namelist():
                # Skip directories and __MACOSX entries
                if entry.endswith("/") or "/__MACOSX" in entry or entry.startswith("__MACOSX"):
                    continue
                basename = os.path.basename(entry)
                if basename:
                    index[basename.lower()] = entry
    except zipfile.BadZipFile:
        frappe.log_error(title="Bulk PE: Bad ZIP file", message=zip_file_url)

    return index


def attach_file_from_zip(zip_file_url, zip_entry_path, target_doctype, target_name):
    """
    Extract a single file from ZIP and attach it to a Frappe document.
    Returns the created File doc name, or None on failure.
    """
    import zipfile, os

    zip_path = get_file_path(zip_file_url)
    basename = os.path.basename(zip_entry_path)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            file_content = zf.read(zip_entry_path)

        file_doc = frappe.get_doc({
            "doctype": "File",
            "file_name": basename,
            "attached_to_doctype": target_doctype,
            "attached_to_name": target_name,
            "content": file_content,
            "is_private": 1,
        })
        file_doc.save(ignore_permissions=True)
        return file_doc.name

    except Exception as e:
        frappe.log_error(
            title=f"Bulk PE: File attach error - {basename}",
            message=frappe.get_traceback(),
        )
        return None


def is_accounting_period_closed(posting_date, company=None):
    """Check if the posting date falls in a closed accounting period."""
    if not posting_date:
        return False
    if not company:
        company = frappe.defaults.get_defaults().get("company", "Bonito Designs Pvt Ltd")

    closed_period = frappe.db.exists(
        "Accounting Period",
        {
            "company": company,
            "period_start_date": ("<=", posting_date),
            "period_end_date": (">=", posting_date),
            "closed": 1,
        },
    )
    return bool(closed_period)


def get_outstanding_invoices_fifo(party_type, party, company=None):
    """
    Fetch outstanding Purchase Invoices for a party in FIFO order.
    Returns list of dicts with name, outstanding_amount, grand_total, posting_date.
    Only includes submitted invoices with outstanding_amount > 0.
    Ordered by posting_date ASC (oldest first) = FIFO.
    """
    if not company:
        company = frappe.defaults.get_defaults().get("company", "Bonito Designs Pvt Ltd")

    filters = {
        "docstatus": 1,
        "outstanding_amount": (">", 0),
        "company": company,
    }
    if party_type == "Supplier":
        filters["supplier"] = party
    else:
        filters["customer"] = party

    invoices = frappe.get_all(
        "Purchase Invoice",
        filters=filters,
        fields=["name", "outstanding_amount", "grand_total", "posting_date", "supplier"],
        order_by="posting_date asc, creation asc",
    )

    return invoices


class BatchCache:
    """Cache for batch lookups to avoid repeated DB calls."""

    def __init__(self):
        self._supplier_cache = {}
        self._customer_cache = {}
        self._pinv_cache = {}
        self._fifo_cache = {}
        self._accounting_period_cache = {}
        self._mode_of_payment_cache = {}

    def party_exists(self, party_type, party_name):
        """Check if party exists."""
        if party_type == "Supplier":
            if party_name not in self._supplier_cache:
                self._supplier_cache[party_name] = frappe.db.exists("Supplier", party_name)
            return self._supplier_cache[party_name]
        elif party_type == "Customer":
            if party_name not in self._customer_cache:
                self._customer_cache[party_name] = frappe.db.exists("Customer", party_name)
            return self._customer_cache[party_name]
        return frappe.db.exists(party_type, party_name)

    def purchase_invoice_exists(self, pi_name):
        """Check if Purchase Invoice exists and is submitted."""
        if pi_name not in self._pinv_cache:
            self._pinv_cache[pi_name] = frappe.db.get_value(
                "Purchase Invoice", pi_name,
                ["name", "docstatus", "supplier", "outstanding_amount", "grand_total"],
                as_dict=True,
            )
        return self._pinv_cache[pi_name]

    def get_fifo_invoices(self, party_type, party, company):
        """Get outstanding invoices in FIFO order (cached per party)."""
        key = f"{party_type}:{party}"
        if key not in self._fifo_cache:
            self._fifo_cache[key] = get_outstanding_invoices_fifo(party_type, party, company)
        return self._fifo_cache[key]

    def is_period_closed(self, posting_date, company):
        """Check accounting period (cached per date)."""
        if posting_date not in self._accounting_period_cache:
            self._accounting_period_cache[posting_date] = is_accounting_period_closed(
                posting_date, company
            )
        return self._accounting_period_cache[posting_date]

    def mode_of_payment_exists(self, mop):
        """Check if Mode of Payment exists."""
        if mop not in self._mode_of_payment_cache:
            self._mode_of_payment_cache[mop] = frappe.db.exists("Mode of Payment", mop)
        return self._mode_of_payment_cache[mop]


# ── before_save Hook ──────────────────────────────────────────────────────────

def before_save(doc, method=None):
    """Prevent attachment changes on submitted documents."""
    if doc.docstatus == 1:
        old_doc = doc.get_doc_before_save()
        if old_doc and old_doc.csv_file != doc.csv_file:
            frappe.throw(_("Cannot change attachment on a submitted document"))


# ── CSV Parsing ───────────────────────────────────────────────────────────────

@frappe.whitelist()
def parse_csv_to_items(doc_name):
    """Parse CSV file and populate child table items. Called on csv_file change."""
    doc = frappe.get_doc("Bulk Payment Entry Creation", doc_name)

    if not doc.csv_file:
        frappe.throw(_("Please attach a CSV file first"))

    file_path = get_file_path(doc.csv_file)

    with open(file_path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    reader = csv.DictReader(StringIO(content))
    rows = list(reader)

    if not rows:
        frappe.throw(_("CSV file is empty or has no data rows"))

    # Validate required headers (Name is optional for auto-FIFO / advance)
    required_headers = ["Party Type", "Party", "Account Paid From", "Paid Amount"]
    headers = list(rows[0].keys())
    header_map = {h.strip(): h for h in headers}
    missing = [
        h for h in required_headers
        if h not in header_map and h.lower() not in {k.lower() for k in header_map}
    ]
    if missing:
        frappe.throw(
            _("Missing required columns: {0}. Found: {1}").format(
                ", ".join(missing), ", ".join(headers)
            )
        )

    # Clear existing items
    doc.items = []

    # ── Multi-row grouping ────────────────────────────────────────────────
    # A "header" row has Party filled — starts a new payment entry.
    # A "continuation" row has Party empty but Name filled — adds another
    # Purchase Invoice reference to the current payment entry.
    # If Name is empty on the header row, auto-FIFO mode is used.
    # If Name is "Advance" (case-insensitive), advance payment mode is used.
    # ──────────────────────────────────────────────────────────────────────

    pending_entry = None

    def flush_entry(entry):
        if entry:
            doc.append("items", entry)

    for idx, row in enumerate(rows, 1):
        normalized = {}
        for k, v in row.items():
            normalized[k.strip()] = (v or "").strip()

        party = normalized.get("Party", "")
        pi_name = normalized.get("Name", "")

        if party:
            # ── Header row ────────────────────────────────────────────
            flush_entry(pending_entry)

            party_type = normalized.get("Party Type", "Supplier")
            account_paid_from = normalized.get("Account Paid From", "")
            paid_amount_str = normalized.get("Paid Amount", "0")
            mode_of_payment = normalized.get("Mode of Payment", "") or normalized.get("Mode_of_Payment", "")
            posting_date_str = normalized.get("Posting Date", "") or normalized.get("Posting_Date", "")
            cheque_no = (
                normalized.get("Cheque_or_reference_no", "")
                or normalized.get("Cheque/Reference No", "")
            )
            cheque_date = (
                normalized.get("Cheque_or_reference_date", "")
                or normalized.get("Cheque/Reference Date", "")
            )
            remarks = normalized.get("Remarks", "") or normalized.get("remarks", "")
            proof_files = (
                normalized.get("Proof Files", "")
                or normalized.get("Proof_Files", "")
                or normalized.get("proof_files", "")
            )

            try:
                paid_amount = float(paid_amount_str.replace(",", ""))
            except (ValueError, TypeError):
                paid_amount = 0

            # ── Detect Advance mode ───────────────────────────────────
            if pi_name.strip().lower() == ADVANCE_KEYWORD:
                pi_field = ADVANCE_MARKER
            else:
                pi_list = [pi_name] if pi_name else []
                pi_field = "|".join(pi_list)

            pending_entry = {
                "party_type": party_type or "Supplier",
                "party": party,
                "account_paid_from": account_paid_from,
                "paid_amount": paid_amount,
                "mode_of_payment": mode_of_payment,
                "posting_date": parse_date(posting_date_str),
                "purchase_invoices": pi_field,
                "cheque_reference_no": cheque_no,
                "cheque_reference_date": parse_date(cheque_date),
                "remarks": remarks,
                "proof_filenames": proof_files,
                "row_status": "Pending",
                "csv_row_number": idx,
            }

        elif pi_name and pending_entry:
            # ── Continuation row ──────────────────────────────────────
            # Continuation rows are not applicable for advance payments
            if not is_advance_marker(pending_entry["purchase_invoices"]):
                existing = pending_entry["purchase_invoices"]
                if existing:
                    pending_entry["purchase_invoices"] = existing + "|" + pi_name
                else:
                    pending_entry["purchase_invoices"] = pi_name

        # Rows with neither Party nor Name are ignored

    flush_entry(pending_entry)

    doc.save()
    frappe.db.commit()

    return {"items_count": len(doc.items)}


# ── Validation ────────────────────────────────────────────────────────────────

@frappe.whitelist()
def validate_items(doc_name):
    """Validate all items. Returns detailed results for modal display."""
    doc = frappe.get_doc("Bulk Payment Entry Creation", doc_name)
    cache = BatchCache()
    company = frappe.defaults.get_defaults().get("company", "Bonito Designs Pvt Ltd")

    results = {"valid": 0, "invalid": 0, "details": []}

    # Build zip file index for proof file validation
    zip_index = get_zip_file_index(doc.proof_files_zip) if doc.proof_files_zip else {}

    for item in doc.items:
        errors = []
        warnings = []

        party_type = item.party_type or "Supplier"
        is_advance = is_advance_marker(item.purchase_invoices)

        # 1. Validate Party exists
        if not item.party:
            errors.append("Party is required")
        elif not cache.party_exists(party_type, item.party):
            errors.append(f"{party_type} '{item.party}' does not exist")

        # 2. Validate Account Paid From
        if not item.account_paid_from:
            errors.append("Account Paid From is required")
        elif not frappe.db.exists("Account", item.account_paid_from):
            errors.append(f"Account '{item.account_paid_from}' does not exist")

        # 3. Validate Paid Amount
        if not item.paid_amount or item.paid_amount <= 0:
            errors.append("Paid Amount must be greater than 0")

        # 4. Validate Mode of Payment (optional but must exist if given)
        if item.mode_of_payment and not cache.mode_of_payment_exists(item.mode_of_payment):
            errors.append(f"Mode of Payment '{item.mode_of_payment}' does not exist")

        # 5. Validate Posting Date & Accounting Period
        if item.posting_date:
            if cache.is_period_closed(str(item.posting_date), company):
                errors.append(
                    f"Accounting period is closed for posting date {item.posting_date}"
                )
        # posting_date is optional — defaults to today during creation

        # 6. Validate Purchase Invoice references, Auto-FIFO, or Advance
        if is_advance:
            # ── Advance mode: no invoice validation needed ────────────
            warnings.append("Advance payment — no invoice references will be linked")

        else:
            pi_names = [n.strip() for n in (item.purchase_invoices or "").split("|") if n.strip()]

            if pi_names:
                # ── Manual mode: validate specified invoices ──────────
                total_outstanding = 0
                all_zero_outstanding = True
                for pi_name in pi_names:
                    pi_info = cache.purchase_invoice_exists(pi_name)
                    if not pi_info:
                        errors.append(f"Purchase Invoice '{pi_name}' does not exist")
                    elif pi_info.get("docstatus") != 1:
                        errors.append(f"Purchase Invoice '{pi_name}' is not submitted")
                    else:
                        if party_type == "Supplier" and pi_info.get("supplier") != item.party:
                            errors.append(
                                f"Purchase Invoice '{pi_name}' belongs to supplier "
                                f"'{pi_info.get('supplier')}', not '{item.party}'"
                            )
                        outstanding = pi_info.get("outstanding_amount", 0)
                        if outstanding <= 0:
                            warnings.append(
                                f"Purchase Invoice '{pi_name}' has no outstanding amount"
                            )
                        else:
                            all_zero_outstanding = False
                        total_outstanding += outstanding

                if not errors and all_zero_outstanding:
                    errors.append(
                        "All referenced Purchase Invoices have zero outstanding amount"
                    )

                if total_outstanding > 0 and item.paid_amount > total_outstanding:
                    errors.append(
                        f"Paid amount ({item.paid_amount}) exceeds total outstanding "
                        f"({total_outstanding})"
                    )
            else:
                # ── Auto-FIFO mode: check if supplier has outstanding invoices ──
                if not errors and item.party:
                    fifo_invoices = cache.get_fifo_invoices(party_type, item.party, company)
                    if not fifo_invoices:
                        errors.append(
                            f"No outstanding Purchase Invoices found for {party_type} "
                            f"'{item.party}'"
                        )
                    else:
                        total_outstanding = sum(inv.get("outstanding_amount", 0) for inv in fifo_invoices)
                        if item.paid_amount > total_outstanding:
                            errors.append(
                                f"Paid amount ({item.paid_amount}) exceeds total outstanding "
                                f"({total_outstanding}) across {len(fifo_invoices)} invoices"
                            )
                        else:
                            # Show which invoices will be auto-allocated
                            remaining = item.paid_amount
                            auto_pis = []
                            for inv in fifo_invoices:
                                if remaining <= 0:
                                    break
                                alloc = min(remaining, inv["outstanding_amount"])
                                auto_pis.append(f"{inv['name']} ({alloc})")
                                remaining -= alloc
                            warnings.append(
                                f"Auto-FIFO will allocate to: {', '.join(auto_pis)}"
                            )

        # 7. Validate Cheque/Reference No
        if not item.cheque_reference_no:
            warnings.append("Cheque/Reference No is empty")

        # 8. Validate Proof Files (if specified, must exist in ZIP)
        if item.proof_filenames:
            filenames = [f.strip() for f in item.proof_filenames.split("|") if f.strip()]
            if filenames and not doc.proof_files_zip:
                errors.append(
                    "Proof files specified but no ZIP file uploaded in 'Proof Files ZIP' field"
                )
            elif filenames and zip_index:
                for fname in filenames:
                    if fname.lower() not in zip_index:
                        errors.append(f"Proof file '{fname}' not found in ZIP")

        # Update row status
        if errors:
            item.db_set("row_status", "Invalid", update_modified=False)
            item.db_set("error_message", "; ".join(errors), update_modified=False)
            results["invalid"] += 1
        else:
            item.db_set("row_status", "Valid", update_modified=False)
            item.db_set(
                "error_message",
                "; ".join(warnings) if warnings else None,
                update_modified=False,
            )
            results["valid"] += 1

        results["details"].append({
            "row": item.csv_row_number,
            "party": item.party,
            "amount": item.paid_amount,
            "invoices": (
                "(Advance)" if is_advance
                else item.purchase_invoices or "(Auto-FIFO)"
            ),
            "status": "Invalid" if errors else "Valid",
            "errors": errors,
            "warnings": warnings,
        })

    frappe.db.commit()
    return results


# ── Progress Tracking ─────────────────────────────────────────────────────────

def _update_progress(doc, processed, success, failed, skipped):
    """Update progress fields on parent document."""
    doc.db_set("processed_count", processed, update_modified=False)
    doc.db_set("success_count", success, update_modified=False)
    doc.db_set("failed_count", failed, update_modified=False)
    doc.db_set("skipped_count", skipped, update_modified=False)


@frappe.whitelist()
def get_creation_progress(doc_name):
    """Poll progress of background creation job."""
    doc = frappe.get_doc("Bulk Payment Entry Creation", doc_name)
    valid_count = len(
        [i for i in doc.items if i.row_status in ("Valid", "Created", "Failed", "Skipped")]
    )

    return {
        "status": doc.processing_status,
        "total": valid_count,
        "processed": doc.processed_count or 0,
        "success": doc.success_count or 0,
        "failed": doc.failed_count or 0,
        "skipped": doc.skipped_count or 0,
        "current_item": doc.current_item or "",
        "complete": doc.processing_status in ("Completed", "Failed"),
    }


# ── Payment Entry Creation ────────────────────────────────────────────────────

@frappe.whitelist()
def start_creation(doc_name):
    """Start background creation of Payment Entries."""
    doc = frappe.get_doc("Bulk Payment Entry Creation", doc_name)
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
        "bonito_customizations.bulk_payment_entry_native.create_entries_background",
        doc_name=doc_name,
        queue="long",
        timeout=3600,
    )

    return {"status": "started", "total": len(valid_items)}


@frappe.whitelist()
def resume_creation(doc_name):
    """Resume creation from where it left off."""
    doc = frappe.get_doc("Bulk Payment Entry Creation", doc_name)

    pending = len([i for i in doc.items if i.row_status == "Valid"])
    if pending == 0:
        frappe.throw(_("No pending items to process"))

    doc.db_set("processing_status", "In Progress", update_modified=False)
    frappe.db.commit()

    frappe.enqueue(
        "bonito_customizations.bulk_payment_entry_native.create_entries_background",
        doc_name=doc_name,
        queue="long",
        timeout=3600,
    )

    return {"status": "resumed", "total": pending}


def create_entries_background(doc_name):
    """Background job: Create Payment Entries."""
    doc = frappe.get_doc("Bulk Payment Entry Creation", doc_name)
    cache = BatchCache()
    company = frappe.defaults.get_defaults().get("company", "Bonito Designs Pvt Ltd")

    valid_items = [i for i in doc.items if i.row_status == "Valid"]
    total = len(valid_items)
    processed = success = failed = skipped = 0

    for idx, item in enumerate(valid_items, 1):
        try:
            is_advance = is_advance_marker(item.purchase_invoices)

            doc.db_set(
                "current_item",
                f"{item.party} / {'Advance' if is_advance else (item.purchase_invoices or 'Auto-FIFO')}",
                update_modified=False,
            )

            pe = _build_payment_entry(item, company, cache)

            if not pe:
                item.db_set("row_status", "Skipped", update_modified=False)
                item.db_set(
                    "error_message", "Could not build payment entry", update_modified=False
                )
                skipped += 1
                processed += 1
                _update_progress(doc, processed, success, failed, skipped)
                frappe.db.commit()
                continue

            pe.run_method("set_missing_values")
            pe.flags.ignore_permissions = True
            pe.save()
            frappe.db.commit()

            # ── Reference retention fix (skip for advance payments) ──
            if not is_advance:
                _reapply_references(pe, item, cache, company)
                frappe.db.commit()
            # ─────────────────────────────────────────────────────────

            # Store the auto-resolved invoice names back on the child row
            if not is_advance and not item.purchase_invoices and pe.references:
                resolved = "|".join(ref.reference_name for ref in pe.references)
                item.db_set("purchase_invoices", resolved, update_modified=False)

            # ── Attach proof files from ZIP ──────────────────────────
            if item.proof_filenames and doc.proof_files_zip:
                _attach_proof_files(doc.proof_files_zip, item.proof_filenames, pe.name)
            # ─────────────────────────────────────────────────────────

            item.db_set("payment_entry", pe.name, update_modified=False)
            item.db_set("row_status", "Created", update_modified=False)
            item.db_set("error_message", None, update_modified=False)
            success += 1

        except Exception as e:
            frappe.db.rollback()
            item.db_set("row_status", "Failed", update_modified=False)
            item.db_set("error_message", str(e)[:500], update_modified=False)
            failed += 1
            frappe.log_error(
                title=f"Bulk PE Creation Error - {item.party}",
                message=frappe.get_traceback(),
            )

        processed += 1
        _update_progress(doc, processed, success, failed, skipped)
        frappe.db.commit()

    # Final update
    doc.db_set("processing_status", "Completed", update_modified=False)
    doc.db_set("current_item", "", update_modified=False)
    doc.db_set("processed_count", total, update_modified=False)
    doc.db_set("success_count", success, update_modified=False)
    doc.db_set("failed_count", failed, update_modified=False)
    doc.db_set("skipped_count", skipped, update_modified=False)

    # Auto-submit the bulk doc if at least one created
    if success > 0:
        try:
            doc.reload()
            doc.submit()
            frappe.db.commit()
        except Exception:
            frappe.db.commit()
    else:
        frappe.db.commit()


def _get_party_account(party_type, party, company):
    """
    Get the payable/receivable account for a party.
    Checks the party's default account for the company first (Party Account child table),
    then falls back to the company default.
    """
    party_account = None

    if party:
        # Check party-specific account from the Party Account child table
        party_account = frappe.db.get_value(
            "Party Account",
            {"parenttype": party_type, "parent": party, "company": company},
            "account",
        )

    if not party_account:
        # Fallback to company default
        if party_type == "Supplier":
            party_account = frappe.get_value("Company", company, "default_payable_account")
        else:
            party_account = frappe.get_value("Company", company, "default_receivable_account")

    return party_account


def _build_payment_entry(item, company, cache):
    """Build a Payment Entry doc from a child table row."""
    party_type = item.party_type or "Supplier"
    is_advance = is_advance_marker(item.purchase_invoices)
    pi_names = []
    if not is_advance:
        pi_names = [n.strip() for n in (item.purchase_invoices or "").split("|") if n.strip()]

    # Determine paid_to account (the party's payable/receivable account)
    # Priority: supplier/customer-specific account > company default
    paid_to = _get_party_account(party_type, item.party, company)

    paid_from_currency = (
        frappe.get_value("Account", item.account_paid_from, "account_currency") or "INR"
    )
    paid_to_currency = frappe.get_value("Account", paid_to, "account_currency") or "INR"

    posting_date = str(item.posting_date) if item.posting_date else frappe.utils.today()

    pe = frappe.new_doc("Payment Entry")
    pe.payment_type = "Pay"
    pe.posting_date = posting_date
    pe.company = company
    pe.mode_of_payment = item.mode_of_payment or "Bank Draft"
    pe.party_type = party_type
    pe.party = item.party

    pe.party_account = paid_to
    pe.party_account_currency = paid_to_currency

    pe.paid_from = item.account_paid_from
    pe.paid_from_account_currency = paid_from_currency
    pe.paid_to = paid_to
    pe.paid_to_account_currency = paid_to_currency

    pe.paid_amount = item.paid_amount
    pe.received_amount = item.paid_amount
    pe.target_exchange_rate = 1
    pe.source_exchange_rate = 1
    pe.reference_no = item.cheque_reference_no or "Bulk Payment"
    pe.reference_date = item.cheque_reference_date or posting_date

    # Custom remarks
    if item.remarks:
        pe.custom_remarks = 1
        pe.remarks = item.remarks
    elif is_advance:
        # Default remark for advance payments
        pe.custom_remarks = 1
        pe.remarks = f"Advance payment to {item.party}"

    # ── Resolve invoice references ────────────────────────────────────────
    if is_advance:
        # Advance mode: no references — payment entry with no invoice linkage
        pass
    elif pi_names:
        # Manual mode: use specified invoices
        _add_manual_references(pe, pi_names, item.paid_amount, cache)
    else:
        # Auto-FIFO mode: fetch outstanding invoices and allocate
        _add_fifo_references(pe, party_type, item.party, item.paid_amount, company, cache)

    return pe


def _add_manual_references(pe, pi_names, paid_amount, cache):
    """Add manually specified Purchase Invoice references."""
    remaining = paid_amount
    for pi_name in pi_names:
        pi_info = cache.purchase_invoice_exists(pi_name)
        if not pi_info:
            continue

        outstanding = pi_info.get("outstanding_amount", 0)
        if outstanding <= 0:
            allocated = 0
        else:
            allocated = min(remaining, outstanding)
            remaining -= allocated

        pe.append("references", {
            "reference_doctype": "Purchase Invoice",
            "reference_name": pi_name,
            "total_amount": pi_info.get("grand_total", 0),
            "outstanding_amount": outstanding,
            "allocated_amount": allocated,
        })


def _add_fifo_references(pe, party_type, party, paid_amount, company, cache):
    """
    Auto-FIFO: fetch outstanding invoices for the party, ordered oldest first,
    and allocate paid_amount across them.  The last invoice may be partial.
    """
    fifo_invoices = get_outstanding_invoices_fifo(party_type, party, company)
    # Don't use cache here — we want fresh data at creation time

    remaining = paid_amount
    for inv in fifo_invoices:
        if remaining <= 0:
            break

        outstanding = inv.get("outstanding_amount", 0)
        if outstanding <= 0:
            continue

        allocated = min(remaining, outstanding)
        remaining -= allocated

        pe.append("references", {
            "reference_doctype": "Purchase Invoice",
            "reference_name": inv["name"],
            "total_amount": inv.get("grand_total", 0),
            "outstanding_amount": outstanding,
            "allocated_amount": allocated,
        })


def _reapply_references(pe, item, cache, company):
    """
    Re-apply Purchase Invoice references after initial save.
    ERPNext Payment Entry can clear references on first save.
    """
    pe.reload()

    existing_refs = {ref.reference_name for ref in pe.references}

    # Determine what should be there
    pi_names = [n.strip() for n in (item.purchase_invoices or "").split("|") if n.strip()]

    if not pi_names:
        # Auto-FIFO: re-derive from what we would allocate
        party_type = item.party_type or "Supplier"
        fifo_invoices = get_outstanding_invoices_fifo(party_type, item.party, company)
        remaining = item.paid_amount
        pi_names = []
        for inv in fifo_invoices:
            if remaining <= 0:
                break
            outstanding = inv.get("outstanding_amount", 0)
            if outstanding <= 0:
                continue
            pi_names.append(inv["name"])
            remaining -= min(remaining, outstanding)

    missing = [pi for pi in pi_names if pi not in existing_refs]

    if not missing:
        return  # All intact

    # Re-add missing references
    remaining = item.paid_amount
    for ref in pe.references:
        remaining -= ref.allocated_amount

    for pi_name in missing:
        pi_info = cache.purchase_invoice_exists(pi_name)
        if not pi_info:
            # Try fresh lookup for FIFO-resolved invoices
            pi_info = frappe.db.get_value(
                "Purchase Invoice", pi_name,
                ["name", "docstatus", "supplier", "outstanding_amount", "grand_total"],
                as_dict=True,
            )
        if not pi_info:
            continue

        outstanding = pi_info.get("outstanding_amount", 0)
        allocated = min(remaining, outstanding) if outstanding > 0 else 0
        remaining -= allocated

        pe.append("references", {
            "reference_doctype": "Purchase Invoice",
            "reference_name": pi_name,
            "total_amount": pi_info.get("grand_total", 0),
            "outstanding_amount": outstanding,
            "allocated_amount": allocated,
        })

    pe.flags.ignore_permissions = True
    pe.save()


def _attach_proof_files(zip_file_url, proof_filenames_str, pe_name):
    """
    Extract proof files from ZIP and attach them to a Payment Entry.
    proof_filenames_str is pipe-separated: "receipt.pdf|slip.jpg"
    """
    filenames = [f.strip() for f in proof_filenames_str.split("|") if f.strip()]
    if not filenames:
        return

    zip_index = get_zip_file_index(zip_file_url)
    if not zip_index:
        return

    for fname in filenames:
        zip_entry = zip_index.get(fname.lower())
        if zip_entry:
            attach_file_from_zip(zip_file_url, zip_entry, "Payment Entry", pe_name)


# ── CSV Template ──────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_csv_template():
    """Return sample CSV content for download."""
    return (
        "Party Type,Party,Account Paid From,Paid Amount,Mode of Payment,"
        "Posting Date,Name,Cheque_or_reference_no,Cheque_or_reference_date,"
        "Remarks,Proof Files\n"
        "Supplier,BESCOM,Bank Account - BDPL,15000,Bank Draft,"
        "2025-02-01,PINV-00001,CHQ-12345,2025-02-01,"
        "Electricity bill payment,bescom_receipt.pdf\n"
        "Supplier,AWS India,Bank Account - BDPL,75000,NEFT,"
        "2025-02-05,PINV-00002,NEFT-98765,2025-02-05,"
        "Cloud hosting Jan + Feb,aws_invoice.pdf|bank_slip.jpg\n"
        ",,,,,,PINV-00003,,,,\n"
        "Supplier,Vendor XYZ,Bank Account - BDPL,50000,RTGS,"
        "2025-02-10,,TXN-11111,2025-02-10,"
        "Auto-FIFO allocation,vendor_xyz_proof.pdf\n"
        "Supplier,Material Co,Bank Account - BDPL,100000,NEFT,"
        "2025-02-15,Advance,NEFT-55555,2025-02-15,"
        "Advance payment for March order,material_co_receipt.pdf"
    )

