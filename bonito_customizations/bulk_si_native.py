"""
Bulk Sales Invoice Creation from CSV
======================================
Creates Sales Invoices based on milestone billing CSV uploads.

CSV Format Required:
- Project No
- Milestone
- Date
- Modular Price
- VRM Price
- Sales Tax Template
- Remarks

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
        # Multiple matches - return first but caller should note ambiguity
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


# ── CSV Parsing ──────────────────────────────────────────────────────────────

@frappe.whitelist()
def parse_csv_to_items(file_url):
    """
    Parse an uploaded CSV file and return structured items for the child table.
    Called when CSV is attached to load data into the grid.
    """
    if not file_url:
        frappe.throw(_("No file URL provided"))

    file_doc = frappe.get_doc("File", {"file_url": file_url})
    content = file_doc.get_content()

    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")

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
            continue  # skip empty rows

        # Resolve customer and project by project_no
        customer = find_customer_by_project(project_no)
        project = find_project_by_project_no(project_no)

        # Resolve item code
        item_code = get_milestone_item_code(milestone)

        # Parse date
        posting_date, posting_time = parse_date(date_str) if date_str else (None, None)

        # Common fields for each row
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
        }

        # Split into separate rows — each becomes its own Sales Invoice
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

        # If neither amount present, still add a row so validation can flag it
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


# ── Validation ───────────────────────────────────────────────────────────────

@frappe.whitelist()
def validate_items(doc_name):
    """
    Validate all items in the Bulk Sales Invoice Creation document.
    Updates row_status and error_message for each row.
    """
    doc = frappe.get_doc("Bulk Sales Invoice Creation", doc_name)

    valid_count = 0
    invalid_count = 0

    for item in doc.items:
        errors = []
        is_vrm = (item.invoice_type == "VRM")
        is_modular = (item.invoice_type == "Modular")

        # 1. Project No is required
        if not item.project_no:
            errors.append("Project No is missing")

        # 2. Milestone is required and must be valid
        if not item.milestone:
            errors.append("Milestone is missing")
        elif not get_milestone_item_code(item.milestone):
            errors.append(
                f"Unknown milestone '{item.milestone}'. "
                f"Valid: {', '.join(MILESTONE_ITEM_MAP.keys())}"
            )

        # 3. Customer must exist — look up by project_no prefix
        if item.project_no and not item.customer:
            customer = find_customer_by_project(item.project_no)
            if customer:
                item.customer = customer
            else:
                errors.append(
                    f"No customer found with name starting with '{item.project_no} - '"
                )

        # 4. Project must exist — look up by project_no prefix
        if item.project_no and not item.project:
            project = find_project_by_project_no(item.project_no)
            if project:
                item.project = project
            else:
                errors.append(
                    f"No project found with name starting with '{item.project_no} - '"
                )

        # 4a. Verify project has a cost center
        if item.project:
            project_cc = frappe.get_value("Project", item.project, "cost_center")
            if not project_cc:
                errors.append(
                    f"Project '{item.project}' has no cost center assigned"
                )

        # 5. Posting date is required
        if not item.posting_date:
            errors.append("Date is missing or invalid")

        # 6. Amount must be present for the row's type
        if is_modular and not item.modular_amount:
            errors.append("Modular Price is zero/empty")
        elif is_vrm and not item.vrm_amount:
            errors.append("VRM Price is zero/empty")
        elif not is_modular and not is_vrm:
            errors.append("Both Modular and VRM amounts are zero/empty")

        # 7. Check for duplicate sales invoice for the specific item
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

        # 8. Check accounting period is open
        if item.posting_date:
            is_closed, period_name = is_accounting_period_closed(item.posting_date)
            if is_closed:
                errors.append(
                    f"Accounting period '{period_name}' is closed for date {item.posting_date}"
                )

        # 9. Validate tax template exists
        if item.tax_template:
            if not frappe.db.exists(
                "Sales Taxes and Charges Template", item.tax_template
            ):
                errors.append(f"Tax template '{item.tax_template}' not found")

        # 10. Validate item code exists
        if is_modular and item.milestone:
            milestone_item = get_milestone_item_code(item.milestone)
            if milestone_item and not frappe.db.exists("Item", milestone_item):
                errors.append(f"Item '{milestone_item}' does not exist in ERPNext")

        if is_vrm:
            if not frappe.db.exists("Item", VRM_ITEM_CODE):
                errors.append(f"Item '{VRM_ITEM_CODE}' does not exist in ERPNext")

        # Set status
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
    Start Sales Invoice creation as a background job.
    Returns immediately - processing happens in background.
    """
    doc = frappe.get_doc("Bulk Sales Invoice Creation", doc_name)

    # Check there are valid items to process
    valid_items = [i for i in doc.items if i.row_status == "Valid"]
    if not valid_items:
        frappe.throw(_("No valid items to process. Run validation first."))

    # Initialize progress tracking
    doc.processing_status = "In Progress"
    doc.processed_count = 0
    doc.success_count = 0
    doc.failed_count = 0
    doc.skipped_count = 0
    doc.current_item = ""
    doc.save()
    frappe.db.commit()

    # Enqueue the background job
    frappe.enqueue(
        "bonito_customizations.bulk_si_native.create_invoices_background",
        doc_name=doc_name,
        queue="long",
        timeout=3600,
    )

    return {"status": "started", "total": len(valid_items)}


def create_invoices_background(doc_name):
    """
    Background job: Create Sales Invoices for all valid items.
    Updates progress in the document for frontend polling.
    """
    doc = frappe.get_doc("Bulk Sales Invoice Creation", doc_name)
    company = frappe.defaults.get_defaults().get("company")

    valid_items = [i for i in doc.items if i.row_status == "Valid"]
    total = len(valid_items)
    success = 0
    failed = 0
    skipped = 0

    for idx, item in enumerate(valid_items, start=1):
        try:
            # Update progress
            frappe.db.set_value(
                "Bulk Sales Invoice Creation",
                doc_name,
                {
                    "current_item": f"{item.project_no} - {item.milestone}",
                    "processed_count": idx,
                },
                update_modified=False,
            )
            frappe.db.commit()

            # Build Sales Invoice
            si = _build_sales_invoice(item, company)

            if not si:
                item.row_status = "Skipped"
                item.error_message = "No items to invoice (both amounts zero)"
                skipped += 1
                continue

            # Insert (triggers the server script that sets the name based on posting date)
            si.insert()

            # Set customer_invoice_no to the document name (set by server script on before_insert)
            si.customer_invoice_no = si.name

            # Run document calculations (tax etc.)
            si.run_method("set_missing_values")
            si.run_method("calculate_taxes_and_totals")
            si.save()

            frappe.db.commit()

            # Update child row with the created SI link
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
                title=f"Bulk SI Creation Error - {item.project_no}",
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

    # Auto-submit the bulk document if at least one invoice was created
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

    # Look up project by project_no prefix
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
    si.set_posting_time = 1  # Allow backdating
    si.company = company

    # Set remarks from CSV if provided
    if item.remarks:
        si.remarks = item.remarks

    # Temporary placeholder — updated to actual doc name after insert
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

    # Apply tax template
    if item.tax_template:
        si.taxes_and_charges = item.tax_template
        si.run_method("set_missing_values")
        si.run_method("calculate_taxes_and_totals")

    return si


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

