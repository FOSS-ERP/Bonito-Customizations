"""
Bulk Journal Entry Creation
============================
Creates Journal Entries from CSV upload with full validation.

Supports THREE CSV formats, auto-detected by column headers:

1. GENERIC (original) — Multi-line format
   Header row (Entry Type filled) + accounting line rows (Account filled)
   Columns: Entry Type, Posting Date, Slot Payment, Account, Party Type, Party,
            Debit, Credit, Reference Number, Reference Date, Remarks, Mode of Payment

2. SETTLEMENT — Single-row, 1 debit + 2 credits (no Slot Payment)
   Columns: Entry Type, Posting Date, Debit Account, Debit Amount,
            Credit Account, Credit Amount, Charges Account, Charges Party Name,
            Credit Amount Charges, Reference Number, Reference Date, Remarks,
            Mode of Payment
   Logic:
     - Debit line:  Debit Account for Debit Amount (no party)
     - Credit line 1: Credit Account for Credit Amount (no party)
     - Credit line 2: Charges Account for Credit Amount Charges
                      with party_type=Supplier, party=Charges Party Name

3. CUSTOMER CREDIT — Single-row, 1 debit + 1 credit with project-based customer lookup
   Columns: Entry Type, Posting Date, Slot Payment, Debit Account, Debit Amount,
            Customer, Customer Account, Credit Amount, Reference Number,
            Reference Date, Remarks, Mode of Payment
   Logic:
     - Debit line:  Debit Account for Debit Amount (no party)
     - Credit line: Customer Account for Credit Amount
                    with party_type=Customer, party=<resolved from project number>
     - Customer column contains project number → look up Customer by name pattern
       "{project_no} - %"

Installation:
    Save as: apps/bonito_customizations/bonito_customizations/bulk_journal_entry_native.py

    Add to hooks.py:
    doc_events = {
        "Bulk Journal Entry Creation": {
            "before_save": "bonito_customizations.bulk_journal_entry_native.before_save",
        }
    }

    Whitelisted methods (auto-registered via @frappe.whitelist):
    - bulk_journal_entry_native.load_csv
    - bulk_journal_entry_native.validate_items
    - bulk_journal_entry_native.start_creation
    - bulk_journal_entry_native.resume_creation
    - bulk_journal_entry_native.get_creation_progress
    - bulk_journal_entry_native.get_csv_template

Author: Bonito Designs Tech Team
"""

import frappe
from frappe import _
import csv
from datetime import datetime
from io import StringIO


# ── Constants ─────────────────────────────────────────────────────────────────

VALID_ENTRY_TYPES = [
    "Journal Entry",
    "Inter Company Journal Entry",
    "Bank Entry",
    "Cash Entry",
    "Credit Card Entry",
    "Debit Note",
    "Credit Note",
    "Contra Entry",
    "Excise Entry",
    "Write Off Entry",
    "Opening Entry",
    "Depreciation Entry",
    "Exchange Rate Revaluation",
    "Reversal Of ITC",
]

VALID_SLOT_PAYMENTS = [
    "",  # empty is valid
    "Design Fee",
    "1st slot",
    "2nd slot",
    "3rd slot",
    "4th slot",
    "Full slot",
    "Additional Unit",
    "Final Payment",
    "Vendor Payment",
]

VALID_PARTY_TYPES = ["Customer", "Supplier", "Employee", "Shareholder", "Student"]

COMPANY = "Bonito Designs Pvt Ltd"

# CSV format identifiers
FORMAT_GENERIC = "generic"
FORMAT_SETTLEMENT = "settlement"
FORMAT_CUSTOMER_CREDIT = "customer_credit"

# Unique columns that identify each format
SETTLEMENT_MARKER_COLS = {"Charges Account", "Charges Party Name", "Debit Amount Charges"}
CUSTOMER_CREDIT_MARKER_COLS = {"Customer", "Customer Account"}


# ── Helper Functions ──────────────────────────────────────────────────────────

def parse_date(date_str):
    """Parse date string in yyyy-mm-dd format. Returns YYYY-MM-DD or None."""
    if not date_str:
        return None
    date_str = date_str.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(date_str, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def parse_amount(amount_str):
    """Parse amount string to float. Returns 0.0 for empty/invalid."""
    if not amount_str:
        return 0.0
    try:
        return float(str(amount_str).strip().replace(",", ""))
    except (ValueError, TypeError):
        return 0.0


def get_file_path(file_url):
    """Resolve file URL to absolute path."""
    if file_url.startswith("http"):
        from urllib.parse import urlparse
        file_url = urlparse(file_url).path

    if file_url.startswith("/private/files/") or file_url.startswith("/files/"):
        import os
        return os.path.join(frappe.get_site_path(), file_url.lstrip("/"))
    return file_url


def detect_csv_format(headers):
    """
    Detect CSV format based on column headers.
    Returns FORMAT_SETTLEMENT, FORMAT_CUSTOMER_CREDIT, or FORMAT_GENERIC.
    """
    header_set = set(headers)

    if SETTLEMENT_MARKER_COLS.issubset(header_set):
        return FORMAT_SETTLEMENT

    if CUSTOMER_CREDIT_MARKER_COLS.issubset(header_set):
        return FORMAT_CUSTOMER_CREDIT

    return FORMAT_GENERIC


def find_customer_by_project(project_no):
    """
    Look up Customer and Project records from project number.

    Customer and Project are independent doctypes in ERPNext.
    Both follow the naming convention: "{project_no} - {name}".
    We find each independently by matching name starting with "{project_no} - ".
    """
    if not project_no:
        return None, None

    project_no = str(project_no).strip()

    # Find Customer whose name starts with "{project_no} - "
    customer_name = frappe.db.get_value(
        "Customer",
        {"name": ("like", f"{project_no} - %")},
        "name",
    )

    # Find Project whose name starts with "{project_no} - "
    project_name = frappe.db.get_value(
        "Project",
        {"name": ("like", f"{project_no} - %")},
        "name",
    )

    return customer_name, project_name


class BatchCache:
    """Cache for batch lookups to avoid repeated DB calls."""

    def __init__(self):
        self._accounts = {}
        self._parties = {}
        self._modes_of_payment = {}

    def account_exists(self, account_name):
        if account_name not in self._accounts:
            self._accounts[account_name] = frappe.db.exists("Account", account_name)
        return self._accounts[account_name]

    def party_exists(self, party_type, party_name):
        key = f"{party_type}::{party_name}"
        if key not in self._parties:
            self._parties[key] = frappe.db.exists(party_type, party_name)
        return self._parties[key]

    def mode_of_payment_exists(self, mode):
        if not mode:
            return True
        if mode not in self._modes_of_payment:
            self._modes_of_payment[mode] = frappe.db.exists("Mode of Payment", mode)
        return self._modes_of_payment[mode]

    def is_restricted_account(self, account_name):
        """
        Check if account can only be updated via Stock Transactions.
        """
        key = f"restricted::{account_name}"
        if key not in self._accounts:
            account_type = frappe.db.get_value("Account", account_name, "account_type")
            restricted_types = [
                "Stock",
                "Stock Received But Not Billed",
                "Stock Adjustment",
                "Expenses Included In Asset Valuation",
            ]
            self._accounts[key] = account_type in restricted_types
        return self._accounts[key]


# ── Hook: before_save ─────────────────────────────────────────────────────────

def before_save(doc, method=None):
    """Prevent CSV file deletion after document is submitted."""
    if doc.docstatus == 1:  # Submitted
        if doc.get_doc_before_save():
            old_doc = doc.get_doc_before_save()
            if old_doc.csv_file and not doc.csv_file:
                frappe.throw(_("Cannot delete attachment from a submitted document"))
            if old_doc.csv_file and doc.csv_file != old_doc.csv_file:
                frappe.throw(_("Cannot change attachment in a submitted document"))


# ── CSV Loading — Format Detection & Dispatch ─────────────────────────────────

@frappe.whitelist()
def load_csv(doc_name):
    """
    Parse CSV and populate the child tables.
    Auto-detects format (Generic, Settlement, or Customer Credit) from headers.
    """
    doc = frappe.get_doc("Bulk Journal Entry Creation", doc_name)

    if not doc.csv_file:
        frappe.throw(_("Please attach a CSV file first."))

    file_path = get_file_path(doc.csv_file)
    with open(file_path, "r", encoding="utf-8-sig") as f:
        content = f.read()

    reader = csv.DictReader(StringIO(content))
    rows = list(reader)

    if not rows:
        frappe.throw(_("CSV file is empty or has no data rows."))

    headers = [h.strip() for h in reader.fieldnames] if reader.fieldnames else []

    csv_format = detect_csv_format(headers)

    if csv_format == FORMAT_SETTLEMENT:
        result = _load_csv_settlement(rows, headers)
    elif csv_format == FORMAT_CUSTOMER_CREDIT:
        result = _load_csv_customer_credit(rows, headers)
    else:
        result = _load_csv_generic(rows, headers)

    # Clear existing child tables and repopulate
    doc.items = []
    doc.accounting_entries = []

    for item_data in result["items"]:
        doc.append("items", item_data)

    for line_data in result["lines"]:
        doc.append("accounting_entries", line_data)

    doc.processing_status = "Not Started"
    doc.save(ignore_permissions=True)
    frappe.db.commit()

    return {
        "items_count": len(doc.items),
        "lines_count": len(doc.accounting_entries),
        "format": csv_format,
    }


# ── CSV Loader: Generic (original multi-line format) ─────────────────────────

def _load_csv_generic(rows, headers):
    """Parse the original multi-line CSV format."""
    required_headers = ["Entry Type", "Account"]
    missing = [h for h in required_headers if h not in headers]
    if missing:
        frappe.throw(_("Missing required CSV columns for Generic format: {0}. Found: {1}").format(
            ", ".join(missing), ", ".join(headers)
        ))

    items = []
    lines = []
    group_idx = 0
    current_header = None

    for csv_row_num, row in enumerate(rows, 2):
        n = {}
        for k, v in row.items():
            n[k.strip()] = (v or "").strip()

        entry_type = n.get("Entry Type", "")
        account = n.get("Account", "")

        if entry_type:
            group_idx += 1
            current_header = {
                "je_group": group_idx,
                "entry_type": entry_type,
                "posting_date": parse_date(n.get("Posting Date", "")),
                "slot_payment": n.get("Slot Payment", ""),
                "reference_number": n.get("Reference Number", ""),
                "reference_date": parse_date(n.get("Reference Date", "")),
                "remarks": n.get("Remarks", ""),
                "mode_of_payment": n.get("Mode of Payment", ""),
                "row_status": "Pending",
                "csv_row_number": csv_row_num,
            }
            items.append(current_header)

            if account:
                debit = parse_amount(n.get("Debit", ""))
                credit = parse_amount(n.get("Credit", ""))
                lines.append({
                    "je_group": group_idx,
                    "account": account,
                    "party_type": n.get("Party Type", ""),
                    "party": n.get("Party", ""),
                    "debit_amount": debit,
                    "credit_amount": credit,
                    "csv_row_number": csv_row_num,
                })

        elif account and current_header:
            debit = parse_amount(n.get("Debit", ""))
            credit = parse_amount(n.get("Credit", ""))
            lines.append({
                "je_group": group_idx,
                "account": account,
                "party_type": n.get("Party Type", ""),
                "party": n.get("Party", ""),
                "debit_amount": debit,
                "credit_amount": credit,
                "csv_row_number": csv_row_num,
            })

    if group_idx == 0:
        frappe.throw(_("No valid Journal Entry groups found in CSV. "
                       "Ensure at least one row has 'Entry Type' filled."))

    return {"items": items, "lines": lines}


# ── CSV Loader: Settlement ────────────────────────────────────────────────────

def _load_csv_settlement(rows, headers):
    """
    Parse Settlement CSV — single-row format.
    Each row → 1 item + 3 accounting lines (1 credit, 2 debits).

    Credit Account receives the gross amount (e.g. Cardswipe/Easebuzz receivable).
    Debit Account receives the net settlement (e.g. HDFC bank).
    Charges Account is debited for PG charges with Supplier party.
    Balance: Credit Amount == Debit Amount + Debit Amount Charges.
    """
    required = ["Entry Type", "Credit Account", "Credit Amount",
                "Debit Account", "Debit Amount",
                "Charges Account", "Charges Party Name", "Debit Amount Charges"]
    missing = [h for h in required if h not in headers]
    if missing:
        frappe.throw(_("Missing required columns for Settlement format: {0}").format(
            ", ".join(missing)
        ))

    items = []
    lines = []
    group_idx = 0

    for csv_row_num, row in enumerate(rows, 2):
        n = {}
        for k, v in row.items():
            n[k.strip()] = (v or "").strip()

        entry_type = n.get("Entry Type", "")
        if not entry_type:
            continue  # skip empty rows

        group_idx += 1

        credit_account = n.get("Credit Account", "")
        credit_amount = parse_amount(n.get("Credit Amount", ""))
        debit_account = n.get("Debit Account", "")
        debit_amount = parse_amount(n.get("Debit Amount", ""))
        charges_account = n.get("Charges Account", "")
        charges_party = n.get("Charges Party Name", "")
        charges_amount = parse_amount(n.get("Debit Amount Charges", ""))

        # Item (header) — no Slot Payment for settlements
        items.append({
            "je_group": group_idx,
            "entry_type": entry_type,
            "posting_date": parse_date(n.get("Posting Date", "")),
            "slot_payment": "",
            "reference_number": n.get("Reference Number", ""),
            "reference_date": parse_date(n.get("Reference Date", "")),
            "remarks": n.get("Remarks", ""),
            "mode_of_payment": n.get("Mode of Payment", ""),
            "row_status": "Pending",
            "csv_row_number": csv_row_num,
        })

        # Line 1: Credit — settlement receivable account (no party)
        lines.append({
            "je_group": group_idx,
            "account": credit_account,
            "party_type": "",
            "party": "",
            "debit_amount": 0,
            "credit_amount": credit_amount,
            "csv_row_number": csv_row_num,
        })

        # Line 2: Debit — bank account (net settlement received)
        lines.append({
            "je_group": group_idx,
            "account": debit_account,
            "party_type": "",
            "party": "",
            "debit_amount": debit_amount,
            "credit_amount": 0,
            "csv_row_number": csv_row_num,
        })

        # Line 3: Debit — charges account with Supplier party
        lines.append({
            "je_group": group_idx,
            "account": charges_account,
            "party_type": "Supplier",
            "party": charges_party,
            "debit_amount": charges_amount,
            "credit_amount": 0,
            "csv_row_number": csv_row_num,
        })

    if group_idx == 0:
        frappe.throw(_("No valid Settlement entries found in CSV."))

    return {"items": items, "lines": lines}


# ── CSV Loader: Customer Credit ───────────────────────────────────────────────

def _load_csv_customer_credit(rows, headers):
    """
    Parse Customer Credit CSV — single-row format.
    Each row → 1 item + 2 accounting lines (1 debit, 1 credit).

    Customer column contains project number → resolved to actual Customer
    during validation via find_customer_by_project().
    """
    required = ["Entry Type", "Debit Account", "Debit Amount",
                "Customer", "Customer Account", "Credit Amount"]
    missing = [h for h in required if h not in headers]
    if missing:
        frappe.throw(_("Missing required columns for Customer Credit format: {0}").format(
            ", ".join(missing)
        ))

    items = []
    lines = []
    group_idx = 0

    for csv_row_num, row in enumerate(rows, 2):
        n = {}
        for k, v in row.items():
            n[k.strip()] = (v or "").strip()

        entry_type = n.get("Entry Type", "")
        if not entry_type:
            continue  # skip empty rows

        group_idx += 1

        debit_account = n.get("Debit Account", "")
        debit_amount = parse_amount(n.get("Debit Amount", ""))
        customer_ref = n.get("Customer", "")  # project number
        customer_account = n.get("Customer Account", "")
        credit_amount = parse_amount(n.get("Credit Amount", ""))
        slot_payment = n.get("Slot Payment", "")

        items.append({
            "je_group": group_idx,
            "entry_type": entry_type,
            "posting_date": parse_date(n.get("Posting Date", "")),
            "slot_payment": slot_payment,
            "reference_number": n.get("Reference Number", ""),
            "reference_date": parse_date(n.get("Reference Date", "")),
            "remarks": n.get("Remarks", ""),
            "mode_of_payment": n.get("Mode of Payment", ""),
            "row_status": "Pending",
            "csv_row_number": csv_row_num,
        })

        # Line 1: Debit (no party)
        lines.append({
            "je_group": group_idx,
            "account": debit_account,
            "party_type": "",
            "party": "",
            "debit_amount": debit_amount,
            "credit_amount": 0,
            "csv_row_number": csv_row_num,
        })

        # Line 2: Credit to customer account
        # Resolve project number → Customer name at load time
        resolved_customer = customer_ref
        if customer_ref:
            customer_name, _project_name = find_customer_by_project(customer_ref)
            if customer_name:
                resolved_customer = customer_name
            # If not found, keep the raw value — validation will catch it

        lines.append({
            "je_group": group_idx,
            "account": customer_account,
            "party_type": "Customer",
            "party": resolved_customer,
            "debit_amount": 0,
            "credit_amount": credit_amount,
            "csv_row_number": csv_row_num,
        })

    if group_idx == 0:
        frappe.throw(_("No valid Customer Credit entries found in CSV."))

    return {"items": items, "lines": lines}


# ── Validation ────────────────────────────────────────────────────────────────

@frappe.whitelist()
def validate_items(doc_name):
    """
    Validate all Journal Entry groups.

    Checks:
    1. Entry Type is valid
    2. Posting Date is present and valid
    3. Slot Payment (if provided) is valid
    4. At least 2 accounting lines per JE
    5. Each account exists and is not restricted (stock accounts)
    6. Party Type is valid (if provided)
    7. Party exists (if Party Type + Party provided)
       — For Customer Credit format, resolves project number → Customer
    8. Each line has either Debit or Credit > 0 (not both)
    9. CRITICAL: Total Debit == Total Credit for the group
    10. Mode of Payment exists (if provided)
    11. Reference Number present (warning if missing)
    """
    doc = frappe.get_doc("Bulk Journal Entry Creation", doc_name)
    cache = BatchCache()

    # Build a map of accounting entries by je_group
    lines_by_group = {}
    for entry in doc.accounting_entries:
        grp = entry.je_group
        if grp not in lines_by_group:
            lines_by_group[grp] = []
        lines_by_group[grp].append(entry)

    results = {"valid": 0, "invalid": 0, "details": []}

    for item in doc.items:
        errors = []
        warnings = []
        group = item.je_group
        lines = lines_by_group.get(group, [])

        # 1. Entry Type validation
        if not item.entry_type:
            errors.append("Entry Type is required")
        elif item.entry_type not in VALID_ENTRY_TYPES:
            errors.append(f"Invalid Entry Type: '{item.entry_type}'. "
                          f"Must be one of: {', '.join(VALID_ENTRY_TYPES)}")

        # 2. Posting Date
        if not item.posting_date:
            errors.append("Posting Date is required (format: yyyy-mm-dd)")

        # 3. Slot Payment (optional but must be valid if provided)
        if item.slot_payment and item.slot_payment not in VALID_SLOT_PAYMENTS:
            errors.append(f"Invalid Slot Payment: '{item.slot_payment}'. "
                          f"Must be one of: {', '.join([s for s in VALID_SLOT_PAYMENTS if s])}")

        # 4. At least 2 accounting lines
        if len(lines) < 2:
            errors.append(f"At least 2 accounting lines required, found {len(lines)}")

        # 5-8. Validate each accounting line
        total_debit = 0.0
        total_credit = 0.0
        line_errors = []

        for line in lines:
            le = []  # line-level errors

            # 5. Account exists
            if not line.account:
                le.append(f"Row {line.csv_row_number}: Account is required")
            elif not cache.account_exists(line.account):
                le.append(f"Row {line.csv_row_number}: Account '{line.account}' does not exist")
            elif cache.is_restricted_account(line.account):
                le.append(f"Row {line.csv_row_number}: Account '{line.account}' can only be "
                          f"updated via Stock Transactions (not allowed in Journal Entry)")

            # 6. Party Type validation
            if line.party_type:
                if line.party_type not in VALID_PARTY_TYPES:
                    le.append(f"Row {line.csv_row_number}: Invalid Party Type '{line.party_type}'. "
                              f"Must be one of: {', '.join(VALID_PARTY_TYPES)}")
                else:
                    # 7. Party exists
                    if line.party:
                        if not cache.party_exists(line.party_type, line.party):
                            le.append(f"Row {line.csv_row_number}: {line.party_type} "
                                      f"'{line.party}' does not exist")
                    elif line.party_type:
                        warnings.append(f"Row {line.csv_row_number}: Party Type is set "
                                        f"but Party is empty")

            # Also check: if party is provided, party_type should be too
            if line.party and not line.party_type:
                le.append(f"Row {line.csv_row_number}: Party '{line.party}' provided "
                          f"but Party Type is missing")

            # 8. Debit/Credit validation
            debit = line.debit_amount or 0.0
            credit = line.credit_amount or 0.0

            if debit > 0 and credit > 0:
                le.append(f"Row {line.csv_row_number}: Cannot have both Debit ({debit}) "
                          f"and Credit ({credit}) on the same line")
            elif debit == 0 and credit == 0:
                le.append(f"Row {line.csv_row_number}: Either Debit or Credit must be > 0")

            total_debit += debit
            total_credit += credit
            line_errors.extend(le)

        errors.extend(line_errors)

        # 9. CRITICAL: Debit must equal Credit
        if lines and abs(total_debit - total_credit) > 0.01:
            errors.append(
                f"BALANCE ERROR: Total Debit ({total_debit:,.2f}) != "
                f"Total Credit ({total_credit:,.2f}). "
                f"Difference: {abs(total_debit - total_credit):,.2f}"
            )

        # 10. Mode of Payment
        if item.mode_of_payment and not cache.mode_of_payment_exists(item.mode_of_payment):
            errors.append(f"Mode of Payment '{item.mode_of_payment}' does not exist")

        # 11. Reference Number (warning only)
        if not item.reference_number:
            warnings.append("Reference Number is empty")

        # Update row status
        if errors:
            item.db_set("row_status", "Invalid", update_modified=False)
            item.db_set("error_message", "; ".join(errors), update_modified=False)
            results["invalid"] += 1
        else:
            item.db_set("row_status", "Valid", update_modified=False)
            item.db_set("error_message",
                         "; ".join(warnings) if warnings else None,
                         update_modified=False)
            results["valid"] += 1

        results["details"].append({
            "row": item.csv_row_number,
            "group": group,
            "entry_type": item.entry_type,
            "posting_date": str(item.posting_date) if item.posting_date else "",
            "total_debit": total_debit,
            "total_credit": total_credit,
            "lines": len(lines),
            "status": "Invalid" if errors else "Valid",
            "errors": errors,
            "warnings": warnings,
        })

    frappe.db.commit()
    return results


def _detect_format_from_data(doc):
    """
    Detect the CSV format from the loaded data pattern.
    Settlement: every group has exactly 3 lines, third line has party_type=Supplier.
    Customer Credit: every group has exactly 2 lines, second line has party_type=Customer.
    Otherwise: Generic.
    """
    lines_by_group = {}
    for entry in doc.accounting_entries:
        grp = entry.je_group
        if grp not in lines_by_group:
            lines_by_group[grp] = []
        lines_by_group[grp].append(entry)

    if not lines_by_group:
        return FORMAT_GENERIC

    is_settlement = True
    is_customer_credit = True

    for grp, group_lines in lines_by_group.items():
        if len(group_lines) != 3:
            is_settlement = False
        else:
            if not (group_lines[2].party_type == "Supplier" and group_lines[2].party):
                is_settlement = False

        if len(group_lines) != 2:
            is_customer_credit = False
        else:
            if group_lines[1].party_type != "Customer":
                is_customer_credit = False

    if is_settlement:
        return FORMAT_SETTLEMENT
    if is_customer_credit:
        return FORMAT_CUSTOMER_CREDIT
    return FORMAT_GENERIC


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
    doc = frappe.get_doc("Bulk Journal Entry Creation", doc_name)
    valid_count = len([i for i in doc.items
                       if i.row_status in ("Valid", "Created", "Failed", "Skipped")])

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


# ── Journal Entry Creation ────────────────────────────────────────────────────

@frappe.whitelist()
def start_creation(doc_name):
    """Start background creation of Journal Entries."""
    doc = frappe.get_doc("Bulk Journal Entry Creation", doc_name)
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
        "bonito_customizations.bulk_journal_entry_native.create_entries_background",
        doc_name=doc_name,
        queue="long",
        timeout=3600,
    )

    return {"status": "started", "total": len(valid_items)}


@frappe.whitelist()
def resume_creation(doc_name):
    """Resume creation from where it left off."""
    doc = frappe.get_doc("Bulk Journal Entry Creation", doc_name)

    pending = len([i for i in doc.items if i.row_status == "Valid"])
    if pending == 0:
        frappe.throw(_("No pending items to process"))

    doc.db_set("processing_status", "In Progress", update_modified=False)
    frappe.db.commit()

    frappe.enqueue(
        "bonito_customizations.bulk_journal_entry_native.create_entries_background",
        doc_name=doc_name,
        queue="long",
        timeout=3600,
    )

    return {"status": "resumed", "total": pending}


def create_entries_background(doc_name):
    """Background job: Create Journal Entries one by one."""
    doc = frappe.get_doc("Bulk Journal Entry Creation", doc_name)
    company = COMPANY

    # Build lines map
    lines_by_group = {}
    for entry in doc.accounting_entries:
        grp = entry.je_group
        if grp not in lines_by_group:
            lines_by_group[grp] = []
        lines_by_group[grp].append(entry)

    valid_items = [i for i in doc.items if i.row_status == "Valid"]
    processed = success = failed = skipped = 0

    for item in valid_items:
        try:
            lines = lines_by_group.get(item.je_group, [])
            doc.db_set(
                "current_item",
                f"Group {item.je_group}: {item.entry_type} ({item.posting_date})",
                update_modified=False,
            )

            je = _build_journal_entry(item, lines, company)

            if not je:
                item.db_set("row_status", "Skipped", update_modified=False)
                item.db_set("error_message",
                            "Could not build journal entry", update_modified=False)
                skipped += 1
                processed += 1
                _update_progress(doc, processed, success, failed, skipped)
                frappe.db.commit()
                continue

            je.flags.ignore_permissions = True

            # ERPNext's JE save() → validate() → set_missing_values() can
            # auto-add default stock/WIP accounts to the accounts table.
            # We let set_missing_values run fully (preserving all ERPNext
            # validations like title, pay_to_recd_from, exchange rates etc.)
            # but strip any extra account rows it injects afterwards.
            if hasattr(je, 'set_missing_values'):
                _original_smv = je.set_missing_values
                def _patched_smv(*args, **kwargs):
                    num_before = len(je.accounts)
                    _original_smv(*args, **kwargs)
                    if len(je.accounts) > num_before:
                        je.accounts = je.accounts[:num_before]
                je.set_missing_values = _patched_smv

            je.save()
            frappe.db.commit()

            # Mark success
            item.db_set("row_status", "Created", update_modified=False)
            item.db_set("journal_entry", je.name, update_modified=False)
            item.db_set("error_message", None, update_modified=False)
            success += 1

        except Exception as e:
            frappe.db.rollback()
            error_msg = str(e)[:500]
            frappe.log_error(
                title=f"Bulk JE Creation Error - Group {item.je_group}",
                message=frappe.get_traceback()
            )
            item.db_set("row_status", "Failed", update_modified=False)
            item.db_set("error_message", error_msg, update_modified=False)
            failed += 1

        processed += 1
        _update_progress(doc, processed, success, failed, skipped)
        frappe.db.commit()

    # Final update
    doc.db_set("processing_status", "Completed", update_modified=False)
    doc.db_set("current_item", "", update_modified=False)
    doc.db_set("processed_count", len(valid_items), update_modified=False)
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


def _build_journal_entry(item, lines, company):
    """
    Build a Journal Entry doc from header item and accounting lines.

    Returns a frappe.Document (unsaved) or None on error.
    """
    if not lines:
        return None

    # Final balance check (belt and suspenders)
    total_debit = sum((l.debit_amount or 0) for l in lines)
    total_credit = sum((l.credit_amount or 0) for l in lines)
    if abs(total_debit - total_credit) > 0.01:
        raise ValueError(
            f"Debit ({total_debit:,.2f}) != Credit ({total_credit:,.2f})"
        )

    je = frappe.new_doc("Journal Entry")
    je.voucher_type = item.entry_type or "Journal Entry"
    je.company = company
    je.posting_date = item.posting_date
    je.multi_currency = 0

    # Cheque/Reference
    if item.reference_number:
        je.cheque_no = item.reference_number
    if item.reference_date:
        je.cheque_date = item.reference_date

    # Remarks — set user_remark and also set remark directly
    if item.remarks:
        je.user_remark = item.remarks
        je.remark = item.remarks

    # Mode of Payment
    if item.mode_of_payment:
        je.mode_of_payment = item.mode_of_payment

    # Custom field: slot_payment
    if item.slot_payment:
        je.slot_payment = item.slot_payment

    # Accounting entries
    for line in lines:
        row = {
            "account": line.account,
            "debit_in_account_currency": line.debit_amount or 0,
            "credit_in_account_currency": line.credit_amount or 0,
            "debit": line.debit_amount or 0,
            "credit": line.credit_amount or 0,
        }
        if line.party_type and line.party:
            row["party_type"] = line.party_type
            row["party"] = line.party

        # Set cost center if available
        default_cc = frappe.db.get_value(
            "Company", company, "cost_center"
        )
        if default_cc:
            row["cost_center"] = default_cc

        je.append("accounts", row)

    # Set total debit/credit on the parent
    je.total_debit = total_debit
    je.total_credit = total_credit

    return je


# ── CSV Template ──────────────────────────────────────────────────────────────

@frappe.whitelist()
def get_csv_template(template_type=None):
    """
    Return a sample CSV template string.
    template_type: 'generic' (default), 'settlement', or 'customer_credit'
    """
    if template_type == "settlement":
        return _get_settlement_template()
    elif template_type == "customer_credit":
        return _get_customer_credit_template()
    else:
        return _get_generic_template()


def _get_generic_template():
    """Original multi-line template."""
    return (
        "Entry Type,Posting Date,Slot Payment,Account,Party Type,Party,"
        "Debit,Credit,Reference Number,Reference Date,Remarks,Mode of Payment\n"
        "Bank Entry,2025-03-01,Design Fee,Bank Account - BDPL,,,0,50000,"
        "ADV-001,2025-03-01,Advance received from customer,Bank Transfer\n"
        ",,,Debtors - BDPL,Customer,John Doe,50000,0,,,,\n"
        "Journal Entry,2025-03-02,,Creditors - BDPL,Supplier,ABC Suppliers,"
        "25000,0,REF-002,2025-03-02,Refund to supplier,\n"
        ",,,Bank Account - BDPL,,,0,25000,,,,\n"
        "Credit Note,2025-03-03,1st slot,Sales - BDPL,,,10000,0,"
        "CN-001,2025-03-03,Credit note for returned goods,\n"
        ",,,Debtors - BDPL,Customer,Jane Smith,0,10000,,,,\n"
    )


def _get_settlement_template():
    """Settlement single-row template.
    Credit Amount = Debit Amount + Debit Amount Charges (must balance).
    """
    return (
        "Entry Type,Posting Date,Credit Account,Credit Amount,"
        "Debit Account,Debit Amount,Charges Account,Charges Party Name,"
        "Debit Amount Charges,Reference Number,Reference Date,Remarks,Mode of Payment\n"
        "Bank Entry,2025-03-01,Cardswipe-BDPL,975000,HDFC - BDPL,965800,"
        "PG Charges - BDPL,HDFC Bank Ltd,9200,REF-001,2025-03-01,Card swipe settlement March,\n"
        "Bank Entry,2025-03-01,Easebuzz-BDPL,100000,HDFC - BDPL,91000,"
        "PG Charges - BDPL,Easebuzz Ltd,9000,REF-002,2025-03-01,Easebuzz settlement March,\n"
    )


def _get_customer_credit_template():
    """Customer Credit single-row template."""
    return (
        "Entry Type,Posting Date,Slot Payment,Debit Account,Debit Amount,"
        "Customer,Customer Account,Credit Amount,Reference Number,Reference Date,"
        "Remarks,Mode of Payment\n"
        "Journal Entry,2025-03-01,,Cardswipe-BDPL,10000,12797,"
        "Bonito Debtors - BDPL,10000,,,Customer credit entry,\n"
        "Journal Entry,2025-03-01,Design Fee,Easebuzz-BDPL,25000,13456,"
        "Bonito Debtors - BDPL,25000,REF-003,2025-03-01,Design fee credit,\n"
    )

