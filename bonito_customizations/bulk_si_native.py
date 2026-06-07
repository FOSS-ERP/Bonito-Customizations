"""
Bulk Sales Invoice Creation from CSV
======================================
Creates Sales Invoices or Credit Notes based on milestone billing CSV uploads.

Supports two operation types:
1. Sales Invoice - creates new SIs from milestone billing data
2. Credit Note - creates return SIs against existing Sales Invoices,
   consuming the requested amount in reverse chronological order
   across multiple SIs if needed.

Sales Invoice CSV Format:
- Project No
- Milestone
- Date
- Modular Price
- VRM Price
- Sales Tax Template
- Remarks

Credit Note CSV Format:
- Project No           (required - used to find customer and their SIs)
- Amount               (required - total amount to reverse, must be > 0)
- Posting Date         (optional - defaults to today)
- Remarks             (optional - reason for reversal)

Credit Note Logic:
    Given a project number and amount, the system:
    1. Finds the customer from the project number
    2. Finds all submitted, non-cancelled Sales Invoices for that customer
       that don't already have credit notes against them
    3. Orders them newest-first (reverse chronological)
    4. Creates credit notes against each SI, consuming the amount:
       - If SI total <= remaining amount: full credit note (negative qty)
       - If SI total > remaining amount: partial credit note (prorated qty)
       - Stops when the entire amount is consumed

Milestone to Item Code Mapping:
- Package completion     -> Production Completion Fees
- Checklist signed       -> Performance Guarantee Services
- IDM                    -> Design Finalization Fees
- Client 3D             -> Production Initiation Fees
- Agreement signed       -> Design Initiation Fees

Installation:
    Save as: apps/bonito_customizations/bonito_customizations/bulk_si_native.py

    Add to hooks.py:
    doc_events = {
        "Bulk Sales Invoice Creation": {
            "before_save": "bonito_customizations.bulk_si_native.before_save",
        }
    }

Author: Bonito Designs Tech Team
"""

import frappe
from frappe import _
import csv
from datetime import datetime
from io import StringIO


# ── Milestone → Item Code Mapping ────────────────────────────────────────────

MILESTONE_ITEM_MAP = {
    "package completion": "Production Completion Fees",
    "checklist signed": "Performance Guarantee Services",
    "idm": "Design Finalization Fees",
    "client 3d": "Production Initiation Fees",
    "agreement signed": "Design Initiation Fees",
}

VRM_ITEM_CODE = "VRM Service"


# ── Helper Functions ─────────────────────────────────────────────────────────

def parse_date(date_str):
    """Parse date string supporting multiple formats. Returns (date, time_str) tuple."""
    date_str = date_str.strip()
    formats = [
        ("%Y-%m-%d %H:%M:%S", True),
        ("%Y-%m-%d %H:%M", True),
        ("%Y-%m-%d", False),
        ("%d-%m-%Y %H:%M:%S", True),
        ("%d-%m-%Y %H:%M", True),
        ("%d-%m-%Y", False),
        ("%d/%m/%Y %H:%M:%S", True),
        ("%d/%m/%Y %H:%M", True),
        ("%d/%m/%Y", False),
        ("%Y/%m/%d", False),
        ("%m/%d/%Y", False),
    ]
    for fmt, has_time in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            date_val = dt.strftime("%Y-%m-%d")
            time_val = dt.strftime("%H:%M:%S") if has_time else "00:00:00"
            return date_val, time_val
        except ValueError:
            continue
    return None, None


def parse_amount(val):
    """Parse an amount string, handling commas and blanks."""
    if not val:
        return 0.0
    val = str(val).strip().replace(",", "")
    try:
        return float(val)
    except ValueError:
        return 0.0


def find_customer_by_project(project_no):
    """
    Find a customer whose name starts with '<project_no> - '.
    The project_no must appear at the very beginning followed by ' - '.
    Returns customer name (ID) or None.
    """
    if not project_no:
        return None

    prefix = f"{project_no.strip()} - "

    customers = frappe.get_all(
        "Customer",
        filters=[["name", "like", f"{prefix}%"]],
        fields=["name", "customer_name"],
        limit_page_length=2,
    )

    if len(customers) == 1:
        return customers[0].name
    elif len(customers) > 1:
        return customers[0].name
    return None


def find_project_by_project_no(project_no):
    """
    Find a project whose name starts with '<project_no> - '.
    The project_no must appear at the very beginning followed by ' - '.
    Returns project name (ID) or None.
    """
    if not project_no:
        return None

    prefix = f"{project_no.strip()} - "

    projects = frappe.get_all(
        "Project",
        filters=[["name", "like", f"{prefix}%"]],
        fields=["name", "cost_center"],
        limit_page_length=2,
    )

    if len(projects) == 1:
        return projects[0].name
    elif len(projects) > 1:
        return projects[0].name
    return None


def get_milestone_item_code(milestone):
    """Map a milestone string to its ERPNext item code."""
    if not milestone:
        return None
    return MILESTONE_ITEM_MAP.get(milestone.strip().lower())


def check_existing_sales_invoice(customer, item_code):
    """
    Check if a Sales Invoice (non-cancelled) already exists for this
    customer + item code combination.
    Returns the SI name or None.
    """
    existing = frappe.db.sql(
        """
        SELECT si.name
        FROM `tabSales Invoice` si
        INNER JOIN `tabSales Invoice Item` sii ON sii.parent = si.name
        WHERE si.customer = %s
          AND sii.item_code = %s
          AND si.docstatus != 2
        LIMIT 1
        """,
        (customer, item_code),
        as_dict=True,
    )
    return existing[0].name if existing else None


def is_accounting_period_closed(posting_date, company=None):
    """
    Check if the accounting period for the given posting date is closed.
    Returns (is_closed: bool, period_name: str or None).
    """
    if not company:
        company = frappe.defaults.get_defaults().get("company")

    closed_periods = frappe.db.sql(
        """
        SELECT ap.name
        FROM `tabAccounting Period` ap
        INNER JOIN `tabClosed Document` cd ON cd.parent = ap.name
        WHERE ap.start_date <= %s
          AND ap.end_date >= %s
          AND ap.company = %s
          AND cd.document_type = 'Sales Invoice'
          AND cd.closed = 1
        LIMIT 1
        """,
        (posting_date, posting_date, company),
        as_dict=True,
    )
    if closed_periods:
        return True, closed_periods[0].name
    return False, None


def get_reversible_sales_invoices(customer):
    """
    Find all submitted Sales Invoices for a customer that do NOT already
    have a non-cancelled credit note against them.
    Returns list of dicts sorted newest-first (reverse chronological by posting_date).

    Each dict: {name, posting_date, total, outstanding_amount}
    """
    # All submitted SIs for this customer
    all_sis = frappe.db.sql(
        """
        SELECT si.name, si.posting_date, si.total, si.outstanding_amount
        FROM `tabSales Invoice` si
        WHERE si.customer = %s
          AND si.docstatus = 1
          AND si.is_return = 0
        ORDER BY si.posting_date DESC, si.creation DESC
        """,
        (customer,),
        as_dict=True,
    )

    if not all_sis:
        return []

    # Filter out SIs that already have a non-cancelled credit note
    reversible = []
    for si in all_sis:
        existing_cn = frappe.db.sql(
            """
            SELECT name FROM `tabSales Invoice`
            WHERE return_against = %s
              AND is_return = 1
              AND docstatus != 2
            LIMIT 1
            """,
            (si.name,),
            as_dict=True,
        )
        if not existing_cn:
            reversible.append(si)

    return reversible


def compute_cn_allocation(reversible_sis, amount):
    """
    Given a list of reversible SIs (newest-first) and a target amount,
    compute how much to reverse against each SI.

    Returns list of dicts:
        [{si_name, si_total, reverse_amount, is_partial}, ...]

    The reverse_amount for each SI is at most the SI's total.
    For a partial reversal (last SI in the chain), reverse_amount < si_total.
    """
    allocation = []
    remaining = amount

    for si in reversible_sis:
        if remaining <= 0:
            break

        si_total = si.total
        if si_total <= 0:
            continue

        if remaining >= si_total:
            # Full reversal of this SI
            allocation.append({
                "si_name": si.name,
                "si_total": si_total,
                "si_posting_date": si.posting_date,
                "reverse_amount": si_total,
                "is_partial": False,
            })
            remaining -= si_total
        else:
            # Partial reversal — this is the last SI we touch
            allocation.append({
                "si_name": si.name,
                "si_total": si_total,
                "si_posting_date": si.posting_date,
                "reverse_amount": remaining,
                "is_partial": True,
            })
            remaining = 0

    return allocation, remaining


# ── CSV Parsing ──────────────────────────────────────────────────────────────

@frappe.whitelist()
def parse_csv_to_items(file_url, operation_type="Sales Invoice"):
    """
    Parse an uploaded CSV file and return structured items for the child table.
    Called when CSV is attached to load data into the grid.
    Routes to the appropriate parser based on operation_type.
    """
    if not file_url:
        frappe.throw(_("No file URL provided"))

    file_doc = frappe.get_doc("File", {"file_url": file_url})
    content = file_doc.get_content()

    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")

    if operation_type == "Credit Note":
        return _parse_credit_note_csv(content)
    else:
        return _parse_sales_invoice_csv(content)


def _parse_sales_invoice_csv(content):
    """Parse CSV for Sales Invoice creation (original format)."""
    reader = csv.DictReader(StringIO(content))

    items = []
    for row_num, row in enumerate(reader, start=2):
        project_no = (row.get("Project No") or "").strip()
        milestone = (row.get("Milestone") or "").strip()
        date_str = (row.get("Date") or "").strip()
        modular_amt = parse_amount(row.get("Modular Price"))
        vrm_amt = parse_amount(row.get("VRM Price"))
        tax_template = (row.get("Sales Tax Template") or "").strip()
        remarks = (row.get("Remarks") or "").strip()

        if not project_no and not milestone:
            continue

        customer = find_customer_by_project(project_no)
        project = find_project_by_project_no(project_no)
        item_code = get_milestone_item_code(milestone)
        posting_date, posting_time = parse_date(date_str) if date_str else (None, None)

        base_item = {
            "project_no": project_no,
            "milestone": milestone,
            "customer": customer,
            "project": project,
            "item_code": item_code,
            "posting_date": posting_date,
            "posting_time": posting_time or "00:00:00",
            "tax_template": tax_template,
            "remarks": remarks,
            "row_status": "Pending",
            "return_against": None,
        }

        if modular_amt:
            items.append(
                {
                    **base_item,
                    "invoice_type": "Modular",
                    "modular_amount": modular_amt,
                    "vrm_amount": 0,
                }
            )

        if vrm_amt:
            items.append(
                {
                    **base_item,
                    "item_code": VRM_ITEM_CODE,
                    "invoice_type": "VRM",
                    "modular_amount": 0,
                    "vrm_amount": vrm_amt,
                }
            )

        if not modular_amt and not vrm_amt:
            items.append(
                {
                    **base_item,
                    "invoice_type": "",
                    "modular_amount": 0,
                    "vrm_amount": 0,
                }
            )

    return items


def _parse_credit_note_csv(content):
    """
    Parse CSV for Credit Note creation.
    New format: Project No, Amount, Posting Date (optional), Remarks (optional)

    Each CSV row is expanded into one or more child table rows — one per SI
    that will be reversed to consume the requested amount.
    """
    reader = csv.DictReader(StringIO(content))

    items = []
    for row_num, row in enumerate(reader, start=2):
        project_no = (row.get("Project No") or "").strip()
        amount = parse_amount(row.get("Amount"))
        date_str = (row.get("Posting Date") or "").strip()
        remarks = (row.get("Remarks") or "").strip()

        if not project_no:
            continue

        # Parse posting date
        if date_str:
            posting_date, posting_time = parse_date(date_str)
        else:
            posting_date = frappe.utils.today()
            posting_time = "00:00:00"

        # Resolve customer
        customer = find_customer_by_project(project_no)
        project = find_project_by_project_no(project_no)

        # If we can't resolve the customer, still add a single row so
        # validation can flag it with a clear error
        if not customer or amount <= 0:
            items.append({
                "project_no": project_no,
                "milestone": "",
                "customer": customer,
                "project": project,
                "item_code": None,
                "invoice_type": "",
                "posting_date": posting_date,
                "posting_time": posting_time or "00:00:00",
                "modular_amount": 0,
                "vrm_amount": 0,
                "tax_template": "",
                "remarks": remarks,
                "row_status": "Pending",
                "return_against": None,
                "cn_request_amount": amount,
                "cn_reverse_amount": 0,
                "cn_is_partial": False,
                "csv_row_number": row_num,
            })
            continue

        # Find reversible SIs and compute allocation
        reversible_sis = get_reversible_sales_invoices(customer)
        allocation, shortfall = compute_cn_allocation(reversible_sis, amount)

        if not allocation:
            # No SIs to reverse — add a placeholder row for validation error
            items.append({
                "project_no": project_no,
                "milestone": "",
                "customer": customer,
                "project": project,
                "item_code": None,
                "invoice_type": "",
                "posting_date": posting_date,
                "posting_time": posting_time or "00:00:00",
                "modular_amount": 0,
                "vrm_amount": 0,
                "tax_template": "",
                "remarks": remarks,
                "row_status": "Pending",
                "return_against": None,
                "cn_request_amount": amount,
                "cn_reverse_amount": 0,
                "cn_is_partial": False,
                "csv_row_number": row_num,
            })
            continue

        # Create one child row per SI in the allocation
        for alloc in allocation:
            si_name = alloc["si_name"]

            # Look up SI details for the child row
            si_doc = frappe.get_doc("Sales Invoice", si_name)
            item_code = None
            invoice_type = ""
            modular_amount = 0
            vrm_amount = 0
            tax_template = si_doc.taxes_and_charges or ""

            if si_doc.items:
                first_item = si_doc.items[0]
                item_code = first_item.item_code
                if item_code == VRM_ITEM_CODE:
                    invoice_type = "VRM"
                    vrm_amount = alloc["reverse_amount"]
                else:
                    invoice_type = "Modular"
                    modular_amount = alloc["reverse_amount"]

            partial_label = " (partial)" if alloc["is_partial"] else ""
            row_remarks = remarks or f"Return against {si_name}{partial_label}"
            if alloc["is_partial"]:
                row_remarks = f"{row_remarks} — ₹{alloc['reverse_amount']:,.2f} of ₹{alloc['si_total']:,.2f}"

            items.append({
                "project_no": project_no,
                "milestone": "",
                "customer": customer,
                "project": si_doc.project or project,
                "item_code": item_code,
                "invoice_type": invoice_type,
                "posting_date": posting_date,
                "posting_time": posting_time or "00:00:00",
                "modular_amount": modular_amount,
                "vrm_amount": vrm_amount,
                "tax_template": tax_template,
                "remarks": row_remarks,
                "row_status": "Pending",
                "return_against": si_name,
                "cn_request_amount": amount,
                "cn_reverse_amount": alloc["reverse_amount"],
                "cn_is_partial": alloc["is_partial"],
                "csv_row_number": row_num,
            })

        # If there's a shortfall, mark the last row so validation can flag it
        if shortfall > 0 and items:
            last = items[-1]
            last["cn_shortfall"] = shortfall

    return items


# ── Validation ───────────────────────────────────────────────────────────────

@frappe.whitelist()
def validate_items(doc_name):
    """
    Validate all items in the Bulk Sales Invoice Creation document.
    Routes to the appropriate validator based on operation_type.
    Updates row_status and error_message for each row.
    """
    doc = frappe.get_doc("Bulk Sales Invoice Creation", doc_name)

    if doc.operation_type == "Credit Note":
        return _validate_credit_note_items(doc)
    else:
        return _validate_sales_invoice_items(doc)


def _validate_sales_invoice_items(doc):
    """Original validation logic for Sales Invoice creation."""
    valid_count = 0
    invalid_count = 0

    for item in doc.items:
        errors = []
        is_vrm = (item.invoice_type == "VRM")
        is_modular = (item.invoice_type == "Modular")

        if not item.project_no:
            errors.append("Project No is missing")

        if not item.milestone:
            errors.append("Milestone is missing")
        elif not get_milestone_item_code(item.milestone):
            errors.append(
                f"Unknown milestone '{item.milestone}'. "
                f"Valid: {', '.join(MILESTONE_ITEM_MAP.keys())}"
            )

        if item.project_no and not item.customer:
            customer = find_customer_by_project(item.project_no)
            if customer:
                item.customer = customer
            else:
                errors.append(
                    f"No customer found with name starting with '{item.project_no} - '"
                )

        if item.project_no and not item.project:
            project = find_project_by_project_no(item.project_no)
            if project:
                item.project = project
            else:
                errors.append(
                    f"No project found with name starting with '{item.project_no} - '"
                )

        if item.project:
            project_cc = frappe.get_value("Project", item.project, "cost_center")
            if not project_cc:
                errors.append(
                    f"Project '{item.project}' has no cost center assigned"
                )

        if not item.posting_date:
            errors.append("Date is missing or invalid")

        if is_modular and not item.modular_amount:
            errors.append("Modular Price is zero/empty")
        elif is_vrm and not item.vrm_amount:
            errors.append("VRM Price is zero/empty")
        elif not is_modular and not is_vrm:
            errors.append("Both Modular and VRM amounts are zero/empty")

        if item.customer and item.milestone:
            if is_modular:
                milestone_item = get_milestone_item_code(item.milestone)
                if milestone_item:
                    existing_si = check_existing_sales_invoice(
                        item.customer, milestone_item
                    )
                    if existing_si:
                        errors.append(
                            f"Sales Invoice '{existing_si}' already exists for "
                            f"this customer and milestone item"
                        )
                        item.sales_invoice = existing_si
            elif is_vrm:
                existing_vrm_si = check_existing_sales_invoice(
                    item.customer, VRM_ITEM_CODE
                )
                if existing_vrm_si:
                    errors.append(
                        f"Sales Invoice '{existing_vrm_si}' already exists for "
                        f"this customer and VRM item"
                    )
                    item.sales_invoice = existing_vrm_si

        if item.posting_date:
            is_closed, period_name = is_accounting_period_closed(item.posting_date)
            if is_closed:
                errors.append(
                    f"Accounting period '{period_name}' is closed for date {item.posting_date}"
                )

        if item.tax_template:
            if not frappe.db.exists(
                "Sales Taxes and Charges Template", item.tax_template
            ):
                errors.append(f"Tax template '{item.tax_template}' not found")

        if is_modular and item.milestone:
            milestone_item = get_milestone_item_code(item.milestone)
            if milestone_item and not frappe.db.exists("Item", milestone_item):
                errors.append(f"Item '{milestone_item}' does not exist in ERPNext")

        if is_vrm:
            if not frappe.db.exists("Item", VRM_ITEM_CODE):
                errors.append(f"Item '{VRM_ITEM_CODE}' does not exist in ERPNext")

        if errors:
            item.row_status = "Failed"
            item.error_message = "; ".join(errors)
            invalid_count += 1
        else:
            item.row_status = "Valid"
            item.error_message = None
            valid_count += 1

    doc.save()

    return {
        "total": len(doc.items),
        "valid": valid_count,
        "invalid": invalid_count,
    }


def _validate_credit_note_items(doc):
    """
    Validation logic for the new Credit Note flow.

    Each child row represents a single CN to create against a specific SI.
    Multiple rows may originate from the same CSV row (one project_no + amount
    expanding into several SI reversals).

    Validations per row:
    1. Project No is required
    2. Customer must be resolvable from Project No
    3. return_against (source SI) must exist and be submitted
    4. Source SI must not already have a credit note
    5. Reverse amount must be > 0 and <= SI total
    6. Posting date is required and accounting period must be open
    7. Shortfall check: if the requested amount exceeds all available SIs
    """
    valid_count = 0
    invalid_count = 0

    for item in doc.items:
        errors = []

        # ── 1. Project No ────────────────────────────────────────────
        if not item.project_no:
            errors.append("Project No is missing")

        # ── 2. Customer resolution ───────────────────────────────────
        if item.project_no and not item.customer:
            customer = find_customer_by_project(item.project_no)
            if customer:
                item.customer = customer
            else:
                errors.append(
                    f"No customer found with name starting with '{item.project_no} - '"
                )

        # ── 3. Amount checks ─────────────────────────────────────────
        cn_request_amount = item.cn_request_amount or 0
        cn_reverse_amount = item.cn_reverse_amount or 0

        if cn_request_amount <= 0 and not item.return_against:
            errors.append("Amount must be greater than zero")

        if cn_reverse_amount <= 0 and item.return_against:
            errors.append("Computed reverse amount is zero — nothing to reverse")

        # ── 4. Shortfall check ────────────────────────────────────────
        cn_shortfall = item.cn_shortfall if hasattr(item, "cn_shortfall") and item.cn_shortfall else 0
        if cn_shortfall > 0:
            errors.append(
                f"Requested amount exceeds available Sales Invoices by "
                f"₹{cn_shortfall:,.2f}. Reduce the amount or ensure all SIs are booked."
            )

        # ── 5. Source SI validation ──────────────────────────────────
        if not item.return_against:
            if item.customer and cn_request_amount > 0:
                errors.append(
                    "No reversible Sales Invoices found for this customer. "
                    "All SIs may already have credit notes or none exist."
                )
        else:
            if not frappe.db.exists("Sales Invoice", item.return_against):
                errors.append(f"Sales Invoice '{item.return_against}' does not exist")
            else:
                si_doc = frappe.get_doc("Sales Invoice", item.return_against)

                if si_doc.docstatus == 0:
                    errors.append(
                        f"Sales Invoice '{item.return_against}' is in Draft — "
                        f"cannot create credit note against a draft"
                    )
                elif si_doc.docstatus == 2:
                    errors.append(
                        f"Sales Invoice '{item.return_against}' is already cancelled"
                    )

                # Check for existing credit note (may have been created since CSV load)
                existing_cn = frappe.db.sql(
                    """
                    SELECT name FROM `tabSales Invoice`
                    WHERE return_against = %s
                      AND is_return = 1
                      AND docstatus != 2
                    LIMIT 1
                    """,
                    (item.return_against,),
                    as_dict=True,
                )
                if existing_cn:
                    errors.append(
                        f"Credit note '{existing_cn[0].name}' already exists "
                        f"against '{item.return_against}'"
                    )

                # Partial amount check: reverse_amount must not exceed SI total
                if si_doc.docstatus == 1 and cn_reverse_amount > si_doc.total:
                    errors.append(
                        f"Reverse amount ₹{cn_reverse_amount:,.2f} exceeds "
                        f"SI total ₹{si_doc.total:,.2f} for '{item.return_against}'"
                    )

                # Refresh child row fields from source SI
                if si_doc.docstatus == 1:
                    item.customer = si_doc.customer
                    item.project = si_doc.project

                    if si_doc.items:
                        first_item = si_doc.items[0]
                        item.item_code = first_item.item_code
                        if first_item.item_code == VRM_ITEM_CODE:
                            item.invoice_type = "VRM"
                        else:
                            item.invoice_type = "Modular"

                    item.tax_template = si_doc.taxes_and_charges or ""

        # ── 6. Posting date & accounting period ──────────────────────
        if not item.posting_date:
            errors.append("Posting Date is missing or invalid")

        if item.posting_date:
            is_closed, period_name = is_accounting_period_closed(item.posting_date)
            if is_closed:
                errors.append(
                    f"Accounting period '{period_name}' is closed for "
                    f"date {item.posting_date}"
                )

        # ── Result ───────────────────────────────────────────────────
        if errors:
            item.row_status = "Failed"
            item.error_message = "; ".join(errors)
            invalid_count += 1
        else:
            item.row_status = "Valid"
            item.error_message = None
            valid_count += 1

    doc.save()

    return {
        "total": len(doc.items),
        "valid": valid_count,
        "invalid": invalid_count,
    }


# ── Invoice Creation ─────────────────────────────────────────────────────────

@frappe.whitelist()
def start_invoice_creation(doc_name):
    """
    Start Sales Invoice / Credit Note creation as a background job.
    Returns immediately - processing happens in background.
    """
    doc = frappe.get_doc("Bulk Sales Invoice Creation", doc_name)

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
        "bonito_customizations.bulk_si_native.create_invoices_background",
        doc_name=doc_name,
        queue="long",
        timeout=3600,
    )

    return {"status": "started", "total": len(valid_items)}


def create_invoices_background(doc_name):
    """
    Background job: Create Sales Invoices or Credit Notes for all valid items.
    Routes to the appropriate builder based on operation_type.
    Updates progress in the document for frontend polling.
    """
    doc = frappe.get_doc("Bulk Sales Invoice Creation", doc_name)
    company = frappe.defaults.get_defaults().get("company")
    is_credit_note = (doc.operation_type == "Credit Note")

    valid_items = [i for i in doc.items if i.row_status == "Valid"]
    total = len(valid_items)
    success = 0
    failed = 0
    skipped = 0

    for idx, item in enumerate(valid_items, start=1):
        try:
            # Update progress
            if is_credit_note:
                progress_label = f"{item.project_no} → {item.return_against}"
            else:
                progress_label = f"{item.project_no} - {item.milestone}"

            frappe.db.set_value(
                "Bulk Sales Invoice Creation",
                doc_name,
                {
                    "current_item": progress_label,
                    "processed_count": idx,
                },
                update_modified=False,
            )
            frappe.db.commit()

            # Build the document
            if is_credit_note:
                si = _build_credit_note(item, company)
            else:
                si = _build_sales_invoice(item, company)

            if not si:
                item.row_status = "Skipped"
                item.error_message = "Nothing to create"
                skipped += 1
                continue

            # Insert
            si.insert()

            # Set customer_invoice_no to the auto-generated doc name
            si.customer_invoice_no = si.name

            # Run document calculations (tax etc.)
            si.run_method("set_missing_values")
            si.run_method("calculate_taxes_and_totals")
            si.save()

            frappe.db.commit()

            # Update child row with the created SI/CN link
            item.sales_invoice = si.name
            item.row_status = "Created"
            item.error_message = None
            success += 1

        except Exception as e:
            frappe.db.rollback()
            item.row_status = "Failed"
            item.error_message = str(e)[:500]
            failed += 1
            frappe.log_error(
                title=f"Bulk SI Creation Error - {item.project_no or item.return_against}",
                message=frappe.get_traceback(),
            )

        # Batch save progress every 5 items
        if idx % 5 == 0 or idx == total:
            try:
                doc.save()
                frappe.db.set_value(
                    "Bulk Sales Invoice Creation",
                    doc_name,
                    {
                        "success_count": success,
                        "failed_count": failed,
                        "skipped_count": skipped,
                    },
                    update_modified=False,
                )
                frappe.db.commit()
            except Exception:
                frappe.db.rollback()
                frappe.db.commit()

    # Final update
    doc.reload()
    doc.processing_status = "Completed"
    doc.processed_count = total
    doc.success_count = success
    doc.failed_count = failed
    doc.skipped_count = skipped
    doc.current_item = ""

    if success > 0:
        try:
            doc.save()
            doc.submit()
            frappe.db.commit()
        except Exception:
            doc.save()
            frappe.db.commit()
    else:
        doc.save()
        frappe.db.commit()


def _build_sales_invoice(item, company):
    """
    Build a Sales Invoice frappe doc from a child table row.
    Each row is either a Modular or VRM invoice (never both).
    Returns the doc (not yet inserted) or None if nothing to invoice.
    """
    is_vrm = (item.invoice_type == "VRM")
    is_modular = (item.invoice_type == "Modular")

    if is_modular:
        inv_item_code = get_milestone_item_code(item.milestone)
        inv_amount = item.modular_amount or 0
        inv_desc = f"{item.milestone} - {item.project_no}"
    elif is_vrm:
        inv_item_code = VRM_ITEM_CODE
        inv_amount = item.vrm_amount or 0
        inv_desc = f"VRM Service - {item.project_no}"
    else:
        return None

    if not inv_amount or not inv_item_code:
        return None

    project_name = item.project if item.project else find_project_by_project_no(item.project_no)
    cost_center = None
    if project_name and frappe.db.exists("Project", project_name):
        cost_center = frappe.get_value("Project", project_name, "cost_center")

    si = frappe.new_doc("Sales Invoice")
    si.customer = item.customer
    si.project = project_name
    si.cost_center = cost_center
    si.posting_date = str(item.posting_date) if item.posting_date else None
    si.posting_time = str(item.posting_time) if item.posting_time else "00:00:00"
    si.set_posting_time = 1
    si.company = company

    if item.remarks:
        si.remarks = item.remarks

    si.customer_invoice_no = f"PENDING-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

    si.append(
        "items",
        {
            "item_code": inv_item_code,
            "qty": 1,
            "rate": inv_amount,
            "amount": inv_amount,
            "description": inv_desc,
            "project": project_name,
            "cost_center": cost_center,
        },
    )

    if item.tax_template:
        si.taxes_and_charges = item.tax_template
        si.run_method("set_missing_values")
        si.run_method("calculate_taxes_and_totals")

    return si


def _build_credit_note(item, company):
    """
    Build a Credit Note (return Sales Invoice) from a child table row.

    Supports both full and partial reversals:
    - Full: copies all items with negative qty (same as before)
    - Partial: prorates quantities so the CN total equals cn_reverse_amount

    Returns the doc (not yet inserted) or None.
    """
    if not item.return_against:
        return None

    source_si = frappe.get_doc("Sales Invoice", item.return_against)
    reverse_amount = item.cn_reverse_amount or 0
    is_partial = item.cn_is_partial if hasattr(item, "cn_is_partial") and item.cn_is_partial else False

    if reverse_amount <= 0:
        return None

    cn = frappe.new_doc("Sales Invoice")
    cn.customer = source_si.customer
    cn.project = source_si.project
    cn.cost_center = source_si.cost_center
    cn.company = company

    # Mark as return / credit note
    cn.is_return = 1
    cn.return_against = source_si.name

    # Posting date from CSV or today
    cn.posting_date = str(item.posting_date) if item.posting_date else frappe.utils.today()
    cn.posting_time = str(item.posting_time) if item.posting_time else "00:00:00"
    cn.set_posting_time = 1

    # Remarks
    cn.remarks = item.remarks or f"Return against {source_si.name}"

    # Temporary placeholder — updated to actual doc name after insert
    cn.customer_invoice_no = f"PENDING-{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

    if is_partial and source_si.total > 0:
        # ── Partial credit note ──────────────────────────────────────
        # Prorate: each item's reversed amount = (item.amount / si.total) * reverse_amount
        # We adjust qty to achieve the target amount while keeping rate unchanged.
        ratio = reverse_amount / source_si.total

        for si_item in source_si.items:
            prorated_amount = round(si_item.amount * ratio, 2)
            prorated_qty = round(si_item.qty * ratio, 4) if si_item.rate else 0

            # Ensure qty produces the right amount (rate * qty = amount)
            if si_item.rate and prorated_qty:
                prorated_amount = round(si_item.rate * prorated_qty, 2)

            cn.append(
                "items",
                {
                    "item_code": si_item.item_code,
                    "qty": -(abs(prorated_qty)),
                    "rate": si_item.rate,
                    "amount": -(abs(prorated_amount)),
                    "description": si_item.description,
                    "project": si_item.project,
                    "cost_center": si_item.cost_center,
                    "uom": si_item.uom,
                    "income_account": si_item.income_account,
                },
            )
    else:
        # ── Full credit note ─────────────────────────────────────────
        for si_item in source_si.items:
            cn.append(
                "items",
                {
                    "item_code": si_item.item_code,
                    "qty": -(si_item.qty),
                    "rate": si_item.rate,
                    "amount": -(si_item.amount),
                    "description": si_item.description,
                    "project": si_item.project,
                    "cost_center": si_item.cost_center,
                    "uom": si_item.uom,
                    "income_account": si_item.income_account,
                },
            )

    # Copy taxes from source SI
    if source_si.taxes_and_charges:
        cn.taxes_and_charges = source_si.taxes_and_charges

    cn.run_method("set_missing_values")
    cn.run_method("calculate_taxes_and_totals")

    return cn


# ── Progress Polling ─────────────────────────────────────────────────────────

@frappe.whitelist()
def get_creation_progress(doc_name):
    """
    Return current progress of invoice creation.
    Called by frontend polling.
    """
    doc = frappe.get_doc("Bulk Sales Invoice Creation", doc_name)

    total_valid = len([i for i in doc.items if i.row_status in ("Valid", "Created", "Failed", "Skipped")])

    return {
        "status": doc.processing_status or "Not Started",
        "total": total_valid,
        "processed": doc.processed_count or 0,
        "success": doc.success_count or 0,
        "failed": doc.failed_count or 0,
        "skipped": doc.skipped_count or 0,
        "current_item": doc.current_item or "",
        "complete": doc.processing_status in ("Completed", "Failed"),
        "items": [
            {
                "project_no": i.project_no,
                "milestone": i.milestone,
                "invoice_type": i.invoice_type,
                "customer": i.customer,
                "return_against": i.return_against if hasattr(i, "return_against") else None,
                "cn_reverse_amount": i.cn_reverse_amount if hasattr(i, "cn_reverse_amount") else None,
                "cn_is_partial": i.cn_is_partial if hasattr(i, "cn_is_partial") else False,
                "status": i.row_status,
                "sales_invoice": i.sales_invoice,
                "error": i.error_message,
            }
            for i in doc.items
            if i.row_status in ("Created", "Failed", "Skipped")
        ],
    }


# ── Hook: Prevent attachment deletion on submitted doc ───────────────────────

def before_save(doc, method=None):
    """Prevent CSV file deletion after document is submitted."""
    if doc.docstatus == 1:
        if doc.get_doc_before_save():
            old_doc = doc.get_doc_before_save()
            if old_doc.csv_file and not doc.csv_file:
                frappe.throw(_("Cannot delete attachment from a submitted document"))
            if old_doc.csv_file and doc.csv_file != old_doc.csv_file:
                frappe.throw(_("Cannot change attachment in a submitted document"))
