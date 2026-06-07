"""
Auto Purchase Receipt - ERPNext Side (v2)

This version has NO email/IMAP logic. All email processing happens
in the Lambda function which calls ERPNext APIs to:
1. Create Auto Purchase Receipt Log (with extracted data already populated)
2. Upload invoice attachment
3. Trigger PR creation via whitelisted method

This module handles:
- Supplier matching
- PO matching
- Draft Purchase Receipt creation (queued)
- Notifications
- Manual actions (retry, PO tagging, manual PR creation)

Add to your app as:
  bonito_customizations/bonito_customizations/auto_purchase_receipt.py
"""

import frappe
import json
import re
import traceback
from frappe import _
from frappe.utils import now_datetime, getdate, cstr, flt, cint
from frappe.utils.file_manager import save_file

# ─────────────────────────────────────────────
# HELPER: fetch vendor_type from Supplier doc
# ─────────────────────────────────────────────

def _get_vendor_type(supplier_name):
    """
    Fetch vendor_type from the Supplier doctype.
    Returns "COGS", "NON COGS", or "" if not set / supplier not found.
    """
    if not supplier_name:
        return ""
    return cstr(frappe.db.get_value("Supplier", supplier_name, "vendor_type") or "").strip()


# ─────────────────────────────────────────────
# 1. API ENDPOINTS - Called by Lambda
# ─────────────────────────────────────────────

@frappe.whitelist()
def check_duplicates(message_ids=None, attachment_hashes=None):
    """
    Batch dedup check. Lambda sends all message_ids and attachment_hashes
    from the current batch. Returns which ones already exist in the DB.
    Single query per field — no N+1.
    """
    if isinstance(message_ids, str):
        message_ids = json.loads(message_ids)
    if isinstance(attachment_hashes, str):
        attachment_hashes = json.loads(attachment_hashes)

    message_ids = [m.strip() for m in (message_ids or []) if m and m.strip()]
    attachment_hashes = [h.strip() for h in (attachment_hashes or []) if h and h.strip()]

    duplicate_message_ids = []
    duplicate_hashes = []

    if message_ids:
        existing = frappe.db.sql("""
            SELECT email_message_id FROM `tabAuto Purchase Receipt Log`
            WHERE email_message_id IN (%s)
        """ % ", ".join(["%s"] * len(message_ids)),
            tuple(message_ids), as_dict=True
        )
        duplicate_message_ids = [r.email_message_id for r in existing]

    if attachment_hashes:
        existing = frappe.db.sql("""
            SELECT attachment_hash FROM `tabAuto Purchase Receipt Log`
            WHERE attachment_hash IN (%s)
        """ % ", ".join(["%s"] * len(attachment_hashes)),
            tuple(attachment_hashes), as_dict=True
        )
        duplicate_hashes = [r.attachment_hash for r in existing]

    return {
        "duplicate_message_ids": duplicate_message_ids,
        "duplicate_hashes": duplicate_hashes
    }


@frappe.whitelist()
def create_pr_from_log(log_name):
    """
    Called by Lambda after creating the log and uploading attachment.
    Runs supplier matching, PO matching, then queues PR creation.
    """
    log = frappe.get_doc("Auto Purchase Receipt Log", log_name)

    if log.purchase_receipt:
        return {"status": "exists", "message": "PR already created"}

    if log.processing_status not in ("Textract Complete", "Manual Review", "Pending"):
        return {"status": "skipped", "message": "Status is %s" % log.processing_status}

    # Run matching
    try:
        # Supplier matching
        if not log.supplier:
            supplier = match_supplier(log)
            if supplier:
                log.supplier = supplier
                log.vendor_type = _get_vendor_type(supplier)
        
        # Backfill vendor_type if supplier already set but type is blank
        if log.supplier and not cstr(log.get("vendor_type")).strip():
            log.vendor_type = _get_vendor_type(log.supplier)

        # PO matching
        if not log.purchase_order:
            # Fallback: if Lambda didn't find PO in summary fields,
            # scan extracted item descriptions (PO sometimes embedded
            # in line items like "Leads 212@800 - BD-PO-2026-00943")
            if not log.buyer_order_no:
                po_from_items = _extract_po_from_items(log)
                if po_from_items:
                    log.buyer_order_no = po_from_items

            po = match_purchase_order(log)
            if po:
                log.purchase_order = po
                log.po_tagged = 1
                log.needs_manual_po_tagging = 0
            else:
                log.needs_manual_po_tagging = 1

        log.save(ignore_permissions=True)
        frappe.db.commit()

    except Exception:
        frappe.log_error(
            title="Auto PR - Matching Failed - %s" % log_name,
            message=traceback.format_exc()
        )

    # Do not proceed if supplier could not be determined
    if not log.supplier:
        log.reload()
        log.processing_status = "Manual Review"
        log.error_message = (log.error_message or "") + "\nCould not determine supplier. PR creation skipped."
        log.save(ignore_permissions=True)
        frappe.db.commit()
        send_notification(log, "manual_review")
        return {"status": "manual_review", "message": "Supplier not detected for %s" % log.name}

    # Queue PR creation - non-blocking
    frappe.enqueue(
        "bonito_customizations.auto_purchase_receipt.create_draft_purchase_receipt",
        queue="default",
        timeout=300,
        log_name=log.name,
        is_async=True
    )

    return {"status": "queued", "message": "PR creation queued for %s" % log.name}


# ─────────────────────────────────────────────
# 2. SUPPLIER MATCHING
# ─────────────────────────────────────────────

def match_supplier(log):
    """
    Match supplier using cascading strategy:
    1. GSTIN (most reliable — unique identifier)
    2. PAN (fallback — some invoices have PAN but no GSTIN)
    3. Email domain from sender
    4. Extracted supplier name (fuzzy)
    Returns supplier name or None.
    """
    supplier_gstin = cstr(log.supplier_gstin).strip()
    supplier_pan = cstr(log.get("supplier_pan") if hasattr(log, "supplier_pan") else "").strip()
    supplier_name_ext = cstr(log.supplier_name_extracted).strip()
    email_from = cstr(log.email_from).strip()
    email_domain = ""

    if email_from and "@" in email_from:
        match = re.search(r"[\w.+-]+@([\w-]+\.[\w.-]+)", email_from)
        if match:
            email_domain = match.group(1).lower()

    # Strategy 1: GSTIN match (highest priority — definitive identifier)
    if supplier_gstin:
        # ERPNext stores GSTIN in the Address doctype linked to Supplier
        # via Dynamic Link. Also check the tax_id field on Supplier.
        supplier = frappe.db.get_value(
            "Supplier",
            {"tax_id": supplier_gstin, "disabled": 0},
            "name"
        )
        if supplier:
            return supplier

        # Check in Address doctype (GST field: gstin)
        supplier_from_address = frappe.db.sql("""
            SELECT dl.link_name
            FROM `tabAddress` a
            JOIN `tabDynamic Link` dl ON dl.parent = a.name
                AND dl.parenttype = 'Address'
                AND dl.link_doctype = 'Supplier'
            WHERE a.gstin = %s
            LIMIT 1
        """, (supplier_gstin,), as_dict=True)

        if supplier_from_address:
            return supplier_from_address[0].link_name
    
    # Strategy 2: PAN match (fallback when GSTIN not on invoice)
    # Suppliers have a dedicated `pan` field in ERPNext
    if supplier_pan:
        supplier = frappe.db.get_value(
            "Supplier",
            {"pan": supplier_pan, "disabled": 0},
            "name"
        )
        if supplier:
            return supplier

    # If GSTIN or PAN was extracted but didn't match any supplier,
    # do NOT fall through to fuzzy name/email matching — the supplier
    # likely doesn't exist in ERP yet. Fuzzy matching here produces
    # false positives (e.g. matching "Supraja Murali" on first-word LIKE).
    # Email domain and name matching only run for invoices with zero
    # tax identifiers — the only case where fuzzy matching is reasonable.
    if supplier_gstin or supplier_pan:
        return None

    # Strategy 3: Email domain match
    if email_domain:
        supplier = frappe.db.get_value(
            "Supplier",
            {"supplier_email_domain": email_domain, "disabled": 0},
            "name"
        )
        if supplier:
            return supplier

        supplier_from_contact = frappe.db.sql("""
            SELECT dl.link_name
            FROM `tabContact Email` ce
            JOIN `tabContact` c ON c.name = ce.parent
            JOIN `tabDynamic Link` dl ON dl.parent = c.name
                AND dl.parenttype = 'Contact'
                AND dl.link_doctype = 'Supplier'
            WHERE ce.email_id LIKE %s
            LIMIT 1
        """, ("%%%s%%" % email_domain,), as_dict=True)

        if supplier_from_contact:
            return supplier_from_contact[0].link_name

    # Strategy 3: Name match (least reliable)
    if supplier_name_ext:
        supplier = frappe.db.get_value(
            "Supplier",
            {"supplier_name": supplier_name_ext, "disabled": 0},
            "name"
        )
        if supplier:
            return supplier

        first_word = supplier_name_ext.split()[0] if supplier_name_ext.split() else supplier_name_ext
        suppliers = frappe.db.sql("""
            SELECT name, supplier_name
            FROM `tabSupplier`
            WHERE disabled = 0
            AND (
                supplier_name LIKE %s
                OR supplier_name LIKE %s
                OR name LIKE %s
            )
            LIMIT 5
        """, (
            "%%%s%%" % supplier_name_ext,
            "%%%s%%" % first_word,
            "%%%s%%" % supplier_name_ext
        ), as_dict=True)

        if len(suppliers) == 1:
            return suppliers[0].name

        for s in suppliers:
            if s.supplier_name.lower() == supplier_name_ext.lower():
                return s.name

        if suppliers:
            return suppliers[0].name

    return None


# ─────────────────────────────────────────────
# 3. PURCHASE ORDER MATCHING
# ─────────────────────────────────────────────

def _extract_po_from_items(log):
    """
    Fallback: scan extracted item descriptions for Bonito PO numbers.
    Only called when buyer_order_no is empty (Lambda didn't find PO
    in summary fields). No DB queries — pure regex on in-memory data.

    Handles Textract line breaks splitting PO numbers:
      "Leads 212@800 BD-PO-2026-\n-\n00943" → finds "BD-PO-2026-00943"
    """
    patterns = [
        r"(BD-PO-\d{4}-\d{4,6})",
        r"(PO-BDPL-\d{4}-\d{4,6})",
        r"(PUR-ORD-\d{4}-\d{4,6})",
    ]
    for item in log.extracted_items:
        text = cstr(item.description)
        # Normalize line breaks and stray hyphens
        text = re.sub(r"[\n\r]+", " ", text)
        text = re.sub(r"-\s+-", "-", text)
        text = re.sub(r"-\s+(\d)", r"-\1", text)
        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip().upper()
    return ""

def match_purchase_order(log):
    """
    Match PO from extracted buyer_order_no
    Only returns POs that are:
        1. Submitted (docstatus == 1)
        2. Not fully received (per_received < 100)
        3. For the SAME supplier as the log (if supplier is known)
    
    The LIKE/pattern match is intentionally removed — it caused
    false positives (e.g. BD-PO-2026-0770 matching BD-PO-2026-07704).
    Only exact matches are reliable for auto-processing.
    """
    buyer_order_no = cstr(log.buyer_order_no).strip().strip(".,;:!?()[]{}\"'")
    if not buyer_order_no:
        return None

    # Only exact match — no LIKE/pattern matching
    if not frappe.db.exists("Purchase Order", buyer_order_no):
        return None

    po = frappe.get_doc("Purchase Order", buyer_order_no)

    if po.docstatus != 1:
        log.error_message = (
            "PO %s found but is not submitted (docstatus: %s)."
            % (po.name, po.docstatus)
        )
        return None

    # Supplier cross-validation: PO supplier must match log supplier
    if log.supplier and po.supplier != log.supplier:
        log.error_message = (
            "PO %s found but belongs to supplier '%s', not '%s'. "
            "Refusing to auto-tag a PO from a different supplier."
            % (po.name, po.supplier, log.supplier)
        )
        return None

    if flt(po.per_received) >= 100:
        log.error_message = (
            "PO %s found but already fully received "
            "(received: %s%%, billed: %s%%, status: %s). "
            "This invoice may be a duplicate or needs a different PO."
            % (po.name, po.per_received, po.per_billed, po.status)
        )
        return None

    return po.name


# ─────────────────────────────────────────────
# 4. PURCHASE RECEIPT CREATION (Queued)
# ─────────────────────────────────────────────

def create_draft_purchase_receipt(log_name):
    """
    Creates a draft Purchase Receipt from extracted data.
    Runs in background queue.
    """
    try:
        log = frappe.get_doc("Auto Purchase Receipt Log", log_name)

        if log.purchase_receipt:
            return

        supplier = log.supplier
        if not supplier:
            log.processing_status = "Manual Review"
            log.error_message = (log.error_message or "") + "\nCould not determine supplier."
            log.save(ignore_permissions=True)
            frappe.db.commit()
            send_notification(log, "manual_review")
            return

        # Duplicate check: supplier + supplier_invoice_no must be unique
        if log.supplier_invoice_no:
            existing_pr = frappe.db.get_value(
                "Purchase Receipt",
                {
                    "supplier": supplier,
                    "supplier_invoice_no": log.supplier_invoice_no,
                    "docstatus": ["!=", 2]  # Not cancelled
                },
                "name"
            )
            if existing_pr:
                log.processing_status = "Manual Review"
                log.error_message = (
                    "Duplicate: Purchase Receipt %s already exists for supplier %s "
                    "with invoice no %s" % (existing_pr, supplier, log.supplier_invoice_no)
                )
                log.save(ignore_permissions=True)
                frappe.db.commit()
                send_notification(log, "manual_review")
                return

        # RULE: Only create PR when a valid PO is tagged.
        # No-PO cases MUST go to Manual Review — finance needs to find/create
        # the correct PO before a receipt can be created.
        if not (log.purchase_order and log.po_tagged):
            log.processing_status = "Manual Review"
            log.needs_manual_po_tagging = 1
            log.error_message = (log.error_message or "") + (
                "\nNo Purchase Order matched. Cannot auto-create Purchase Receipt "
                "without a valid PO. Please tag the correct PO manually."
            )
            log.save(ignore_permissions=True)
            frappe.db.commit()
            send_notification(log, "manual_review")
            return

        # Guard: PO supplier must match log supplier (defense-in-depth)
        po_doc = frappe.get_doc("Purchase Order", log.purchase_order)
        if log.supplier and po_doc.supplier != log.supplier:
            log.processing_status = "Manual Review"
            log.error_message = (log.error_message or "") + (
                "\nPO %s belongs to supplier '%s' but invoice is from '%s'. "
                "Supplier mismatch — cannot auto-create Purchase Receipt."
                % (log.purchase_order, po_doc.supplier, log.supplier)
            )
            log.po_tagged = 0
            log.needs_manual_po_tagging = 1
            log.save(ignore_permissions=True)
            frappe.db.commit()
            send_notification(log, "manual_review")
            return
        
        pr_doc = _create_pr_from_po(log)

        # Tier 3 matching failed — item matching could not determine
        # which items were delivered. Route to manual review rather
        # than creating a PR with full PO quantities that is known
        # to be wrong (invoice total != PO total).
        if pr_doc is None:
            log.processing_status = "Manual Review"
            log.error_message = (log.error_message or "") + (
                "\nPartial delivery detected (invoice total does not match "
                "PO total) but item matching failed — could not determine "
                "which items were delivered. Please create the Purchase "
                "Receipt manually from PO %s." % log.purchase_order
            )
            log.save(ignore_permissions=True)
            frappe.db.commit()
            send_notification(log, "manual_review")
            return

        # Guard: make_purchase_receipt returns 0 items ONLY when ALL
        # PO items are fully received (received_qty >= ordered_qty).
        # _adjust_quantities_from_invoice has an invariant that it
        # NEVER empties pr_doc.items. So if we get 0 items here, the
        # PO is genuinely fully received despite per_received < 100
        # (can happen with rounding or manual qty adjustments).

        pr_item_count = len(pr_doc.items) if pr_doc.items else 0

        if pr_item_count == 0:
            # Double-check: is the PO actually received?
            po_received_pct = flt(po_doc.per_received)
            log.processing_status = "Manual Review"
            if po_received_pct >= 100:
                log.error_message = (log.error_message or "") + (
                    "\nPO %s is fully received (%.1f%%) — no items available "
                    "for receipt. This invoice may be a duplicate or needs "
                    "a different PO." % (log.purchase_order, po_received_pct)
                )
            else:
                # PO shows as not fully received but make_purchase_receipt
                # returned 0 items — likely all individual items are received
                # even though the aggregate % is below 100 (rounding).
                log.error_message = (log.error_message or "") + (
                    "\nPO %s shows %.1f%% received, but all individual items "
                    "appear fully received (no remaining qty). Cannot create "
                    "Purchase Receipt. Please verify the PO item quantities "
                    "and create the receipt manually if needed."
                    % (log.purchase_order, po_received_pct)
                )
            log.save(ignore_permissions=True)
            frappe.db.commit()
            send_notification(log, "manual_review")
            return
        
        # Note: pr_item_count != po_item_count is intentionally NOT checked.
        # Partial deliveries are a legitimate use case — supplier ships fewer
        # items than the PO. The quantity adjustment in _create_pr_from_po
        # handles this by removing unmatched PR items. The total validation
        # below catches cases where adjustment went wrong.

        # Set supplier invoice number and date on Purchase Receipt
        if log.supplier_invoice_no:
            pr_doc.supplier_invoice_no = log.supplier_invoice_no
        if log.invoice_date_extracted:
            try:
                for fmt in (
                    "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y",
                    # dd-Mon-yy / dd.Mon.yy (e.g. 7-Apr-26, 07.Apr.26)
                    "%d-%b-%y", "%d.%b.%y", "%d/%b/%y",
                    # dd-Mon-yyyy / dd.Mon.yyyy (e.g. 7-Apr-2026, 07.Apr.2026)
                    "%d-%b-%Y", "%d.%b.%Y", "%d/%b/%Y",
                    # Mon dd, yyyy (e.g. Apr 7, 2026)
                    "%b %d, %Y",
                    # dd Mon yyyy without separator (e.g. 7 Apr 2026)
                    "%d %b %Y", "%d %b %y",
                    # yyyy/mm/dd
                    "%Y/%m/%d",
                ):
                    try:
                        from datetime import datetime as dt
                        parsed_date = dt.strptime(log.invoice_date_extracted.strip(), fmt)
                        # 2-digit year: strptime maps 00-68 → 2000-2068, 69-99 → 1969-1999
                        # which is fine for our use case (invoices are always recent)
                        pr_doc.supplier_invoice_date = parsed_date.strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        # Add remarks
        remarks_parts = []
        if log.supplier_invoice_no:
            remarks_parts.append("Supplier Invoice No: %s" % log.supplier_invoice_no)
        if log.buyer_order_no:
            remarks_parts.append("Buyer Order No: %s" % log.buyer_order_no)
        if log.invoice_date_extracted:
            remarks_parts.append("Invoice Date: %s" % log.invoice_date_extracted)
        remarks_parts.append("Auto-created from email: %s" % log.email_subject)
        remarks_parts.append("Log: %s" % log.name)
        pr_doc.remarks = " | ".join(remarks_parts)

        # ── Total validation ──
        # For Tier 1/2 (totals match), the PR uses PO remaining items
        # as-is, so PR total should be close to PO remaining total.
        # A large divergence means something unexpected happened in
        # make_purchase_receipt — send to manual review.
        #
        # For Tier 3 (partial delivery), the PR total will differ from
        # invoice total by design. Log it but don't block — the PR is
        # a draft and finance reviews before submitting.
        if log.total_amount and flt(log.total_amount) > 0:
            pr_total = flt(pr_doc.grand_total)
            inv_total = flt(log.total_amount)
            total_diff = abs(pr_total - inv_total)

            po_doc_check = frappe.get_doc("Purchase Order", log.purchase_order)
            po_grand_total = flt(po_doc_check.grand_total)
            was_totals_match = _totals_are_close(po_grand_total, inv_total)

            if was_totals_match and total_diff > max(inv_total * 0.02, 100):
                # Totals matched (Tier 1 or 2) but the resulting PR total
                # diverges significantly from the invoice. This can happen
                # when PO has partially received items that reduce the PR
                # total well below the full PO total. Don't block — log
                # a warning for finance review. The PR is still a draft.
                frappe.logger("auto_pr").info(
                    "Total divergence for PO %s (Tier 1/2): PR total (%.2f) "
                    "vs invoice total (%.2f), diff %.2f. PO may have partial "
                    "receipts. Creating draft for finance review."
                    % (log.purchase_order, pr_total, inv_total, total_diff)
                )

            if not was_totals_match and total_diff > 0:
                # Tier 3 — expected divergence, log for transparency
                frappe.logger("auto_pr").info(
                    "Partial delivery for PO %s (Tier 3): PR total (%.2f) vs "
                    "invoice total (%.2f), diff %.2f. Creating draft for "
                    "finance review."
                    % (log.purchase_order, pr_total, inv_total, total_diff)
                )

        pr_doc.flags.ignore_permissions = True
        pr_doc.flags.ignore_mandatory = True
        pr_doc.flags.ignore_validate = True

        # Disable server scripts during auto PR creation
        # (server scripts on Item can block insert with irrelevant validations)
        frappe.flags.in_import = True
        try:
            pr_doc.insert(ignore_permissions=True)
        finally:
            frappe.flags.in_import = False

        # Attach invoice to PR
        if log.invoice_attachment:
            _attach_invoice_to_pr(log, pr_doc.name)

        log.purchase_receipt = pr_doc.name
        log.processing_status = "PR Created"
        log.save(ignore_permissions=True)
        frappe.db.commit()

        send_notification(log, "pr_created")

        frappe.logger("auto_pr").info(
            "Draft PR %s created from %s" % (pr_doc.name, log.name)
        )

    except Exception as e:
        frappe.log_error(
            title="Auto PR - PR Creation Failed - %s" % log_name,
            message=traceback.format_exc()
        )
        try:
            log = frappe.get_doc("Auto Purchase Receipt Log", log_name)
            log.processing_status = "Failed"

            # Translate cryptic errors to user-friendly messages
            error_str = cstr(e)
            if "Disabled" in error_str and "Item" in error_str:
                user_message = "Could not create PR: an item validation blocked the operation. Please create the Purchase Receipt manually from the PO."
            elif "does not exist" in error_str.lower():
                user_message = "Could not create PR: a referenced record (item/supplier/warehouse) was not found. Please verify and retry."
            else:
                user_message = "PR creation failed: %s" % error_str[:200]

            log.error_message = (log.error_message or "") + "\n" + user_message
            log.retry_count = cint(log.retry_count) + 1
            log.save(ignore_permissions=True)
            frappe.db.commit()
            send_notification(log, "failed")
        except Exception:
            pass


def _totals_are_close(amount_a, amount_b):
    """
    Check if two totals are close enough to be considered a full match.
    Uses percentage-based tolerance for larger amounts and a floor for
    small amounts. This accounts for:
    - Textract rounding errors on extracted totals
    - Minor GST computation differences (e.g. ₹18 vs ₹17.82)
    - Freight/misc charges that Textract may or may not include

    Tolerance: 1% of the larger amount, with a floor of ₹50 and a
    ceiling of ₹500.
    """
    if not amount_a or not amount_b:
        return False
    diff = abs(flt(amount_a) - flt(amount_b))
    larger = max(flt(amount_a), flt(amount_b))
    tolerance = min(max(larger * 0.01, 50), 500)
    return diff <= tolerance


def _create_pr_from_po(log):
    """
    Use ERPNext's native make_purchase_receipt to create PR from PO.
    This is the same as clicking "Create > Purchase Receipt" from the PO form.
    Brings over: items, rates, taxes, project, cost center, department,
    warehouse, expense account, pricing rules — everything.

    make_purchase_receipt ALREADY handles partial receipts — it only
    returns items with remaining qty (qty - received_qty). If an item
    is fully received, it is silently omitted. So pr_doc.items from
    make_purchase_receipt is always the correct "what's left to receive"
    set. We never need to check per_received ourselves for item filtering.

    Three-tier logic for quantity adjustment:

    TIER 1: Totals match + PO has no partial receipts at all.
        The invoice covers the full PO. Use PO items as-is with zero
        modification. Run item matching as a sanity-check log, but
        NEVER block or modify the PR based on match results. This
        handles the common case where supplier nomenclature differs
        completely from PO items.

    TIER 2: Totals match + PO has some items partially received.
        make_purchase_receipt gives us only the remaining quantities.
        The invoice total ≈ PO total means the supplier is billing for
        the full PO (possibly including items we already received from
        a different shipment). Use the remaining items as-is — finance
        will reconcile. Item matching runs as sanity-check only.

    TIER 3: Totals don't match (partial delivery).
        The invoice covers a subset of the PO. Run _adjust_quantities
        to figure out which items were shipped. But NEVER strip items
        so aggressively that the PR ends up empty — that produces the
        misleading "fully received" error.
    """
    from erpnext.buying.doctype.purchase_order.purchase_order import make_purchase_receipt

    # This returns a fully mapped Purchase Receipt doc with all PO details.
    # Items already fully received are ALREADY excluded by make_purchase_receipt.
    # Partially received items have qty = (ordered - received).
    pr_doc = make_purchase_receipt(log.purchase_order)
    pr_doc.posting_date = getdate()
    pr_doc.title = pr_doc.supplier_name or pr_doc.supplier or log.supplier

    # Note: supplier_invoice_no is set in create_draft_purchase_receipt
    # after this function returns.

    po_doc = frappe.get_doc("Purchase Order", log.purchase_order)
    po_grand_total = flt(po_doc.grand_total)
    invoice_total = flt(log.total_amount)

    totals_match = _totals_are_close(po_grand_total, invoice_total)

    # Check if PO has any partial receipts
    po_has_partial_receipts = flt(po_doc.per_received) > 0

    if totals_match and not po_has_partial_receipts:
        # ── TIER 1: Full PO, no partial receipts ──
        # Straightforward: invoice covers entire PO, nothing received yet.
        # Use all PO items as-is. Run item matching as diagnostic only.
        frappe.logger("auto_pr").info(
            "TIER 1: PO %s total (%.2f) ≈ invoice total (%.2f), "
            "no partial receipts. Using PO items as-is."
            % (log.purchase_order, po_grand_total, invoice_total)
        )
        _run_item_match_sanity_check(pr_doc, log)

    elif totals_match and po_has_partial_receipts:
        # ── TIER 2: Full PO total match, but some items partially received ──
        # make_purchase_receipt already gave us only remaining quantities.
        # The invoice total matching PO total likely means the supplier
        # is billing for the full PO value. Use remaining items as-is.
        frappe.logger("auto_pr").info(
            "TIER 2: PO %s total (%.2f) ≈ invoice total (%.2f), "
            "PO %.1f%% received. Using remaining items from make_purchase_receipt."
            % (log.purchase_order, po_grand_total, invoice_total,
               flt(po_doc.per_received))
        )
        _run_item_match_sanity_check(pr_doc, log)

    else:
        # ── TIER 3: Totals don't match — partial delivery ──
        frappe.logger("auto_pr").info(
            "TIER 3: PO %s total (%.2f) vs invoice total (%.2f) — "
            "partial delivery. Running quantity adjustment."
            % (log.purchase_order, po_grand_total, invoice_total)
        )
        if not _adjust_quantities_from_invoice(pr_doc, log):
            # Matching failed — cannot safely create a PR with full PO
            # quantities when the invoice total tells us it's a partial
            # delivery. Return None to signal manual review.
            return None

    # Recompute all taxes and totals
    try:
        pr_doc.run_method("set_missing_values")
        pr_doc.run_method("calculate_taxes_and_totals")
    except Exception:
        # Non-fatal — taxes can be fixed during review
        frappe.log_error(
            title="Auto PR - Tax computation warning",
            message="Could not compute taxes for PO %s: %s" % (
                log.purchase_order, traceback.format_exc())
        )

    return pr_doc


def _run_item_match_sanity_check(pr_doc, log):
    """
    Diagnostic-only item matching for Tier 1 and Tier 2.
    Tries to match extracted invoice items to PR items by description.
    Logs the results but NEVER modifies pr_doc — PO is authoritative.

    Purpose: surface potential issues in the log for finance review
    (e.g. "only 2/5 items matched by name — verify items during review").
    """
    extracted_items = []
    for ext_item in log.extracted_items:
        desc = cstr(ext_item.description).strip().lower()
        if not desc:
            continue
        extracted_items.append(desc)

    if not extracted_items:
        frappe.logger("auto_pr").info(
            "Sanity check for PO %s: no extracted items to match against. "
            "PR uses PO items as-is." % log.purchase_order
        )
        return

    matched_count = 0
    for pr_item in pr_doc.items:
        item_name = cstr(pr_item.item_name).lower()
        item_code = cstr(pr_item.item_code).lower()
        for desc in extracted_items:
            if desc in item_name or item_name in desc:
                matched_count += 1
                break
            elif desc in item_code or item_code in desc:
                matched_count += 1
                break

    total_pr = len(pr_doc.items)
    total_ext = len(extracted_items)

    frappe.logger("auto_pr").info(
        "Sanity check for PO %s: %d/%d PR items matched against "
        "%d extracted items by name. PR items NOT modified (PO is authoritative)."
        % (log.purchase_order, matched_count, total_pr, total_ext)
    )

def _adjust_quantities_from_invoice(pr_doc, log):
    """
    TIER 3 ONLY: Match extracted invoice line items to PR items and
    adjust quantities for partial deliveries.
 
    Called when invoice total does NOT match PO total, meaning the
    invoice covers a subset of the PO.
 
    Returns True if matching produced a usable result (quantities
    adjusted, unmatched items removed). Returns False if matching
    failed — caller should route to manual review instead of
    creating a PR with full PO quantities.
 
    PO-driven three-pass matching strategy:
 
    PASS 0 — Item code match (highest priority):
      Checks if any PR item_code (e.g. "BD-LAM-2023-0226") appears
      in the invoice description. When multiple item_codes appear
      (Textract leak from adjacent rows), the one at the earliest
      string position wins — leaked codes always appear at the end.
 
    PASS 1 — IDF-weighted token overlap:
      PR items are grouped by item_code. Each group gets a token
      profile from its item_name (whitespace-split, uppercased).
      Token group frequency drives IDF weights: tokens in fewer
      groups score higher. Tokens in ALL groups are excluded as
      ubiquitous noise. Requires >= 2 shared non-ubiquitous tokens
      and a clear winner (no ties).
 
    PASS 2 — Substring fallback:
      For items not matched in Pass 0 or 1.
 
    Both aggregated and disaggregated supplier invoices are handled:
    PR items are grouped by item_code. One invoice line distributes
    its qty across multiple PO rows (aggregated). Multiple invoice
    lines for the same product each consume remaining available
    rows (disaggregated).
 
    Rules:
    - Skip empty/blank descriptions.
    - Each extracted item can only match once (consumed after match).
    - Apply min(extracted_qty, remaining_po_qty) to never exceed PO qty.
 
    Item removal policy:
    - Only remove unmatched PR items if >= 50% of PR items were matched
      AND at least 2 items matched.
    - If match ratio is below threshold, return False — the caller
      routes to manual review. We do NOT silently keep all PR items
      at full PO quantities when the invoice total already told us
      this is a partial delivery.
    - INVARIANT: pr_doc.items is NEVER left empty.
    """
    # ── Build extracted items list ──
    extracted_items = []
    for ext_item in log.extracted_items:
        desc = cstr(ext_item.description).strip()
        if not desc:
            continue
        extracted_items.append({
            "description": desc,
            "tokens": set(desc.upper().split()),
            "qty": flt(ext_item.quantity),
            "rate": flt(ext_item.rate),
            "amount": flt(ext_item.amount),
            "consumed": False,
        })
 
    if not extracted_items:
        frappe.logger("auto_pr").info(
            "Qty adjustment for PO %s: no usable extracted items. "
            "Cannot adjust quantities for partial delivery."
            % log.purchase_order
        )
        return False
 
    # ── Group PR items by item_code ──
    pr_groups = {}
    for pr_idx, pr_item in enumerate(pr_doc.items):
        ic = cstr(pr_item.item_code).strip().upper()
        if not ic:
            continue
        if ic not in pr_groups:
            name_tokens = set(cstr(pr_item.item_name).strip().upper().split())
            pr_groups[ic] = {"indices": [], "name_tokens": name_tokens}
        pr_groups[ic]["indices"].append(pr_idx)
 
    # ── Token group frequency + ubiquitous tokens ──
    token_gf = {}
    for group in pr_groups.values():
        for t in group["name_tokens"]:
            token_gf[t] = token_gf.get(t, 0) + 1
    total_groups = len(pr_groups)
    ubiquitous = {t for t, gf in token_gf.items() if gf >= total_groups}
    for group in pr_groups.values():
        group["tokens_filtered"] = group["name_tokens"] - ubiquitous
 
    frappe.logger("auto_pr").info(
        "Tier 3 for PO %s: %d PR items → %d item_code groups, "
        "%d extracted items. Ubiquitous tokens: %s"
        % (log.purchase_order, len(pr_doc.items), total_groups,
           len(extracted_items), sorted(ubiquitous))
    )
 
    matched_pr_indices = set()
 
    # ── Pass 0: Item code match (positional priority) ──
    # When Textract leaks the next row's item_code into the current
    # row's description, multiple codes appear in one description.
    # The correct code is always at the start (it's the line's own
    # code); the leaked code is at the end. Positional priority
    # (earliest string position wins) resolves this correctly.
    for ext in extracted_items:
        if ext["consumed"]:
            continue
 
        desc_upper = ext["description"].upper()
        best_code = None
        best_pos = len(desc_upper) + 1
 
        for ic in pr_groups:
            pos = desc_upper.find(ic)
            if pos != -1 and pos < best_pos:
                available = [i for i in pr_groups[ic]["indices"]
                             if i not in matched_pr_indices]
                if available:
                    best_code = ic
                    best_pos = pos
 
        if best_code is None:
            continue
 
        available = [i for i in pr_groups[best_code]["indices"]
                     if i not in matched_pr_indices]
        remaining = flt(ext["qty"])
        for pr_idx in available:
            if remaining <= 0:
                break
            pr_item = pr_doc.items[pr_idx]
            allocated = min(remaining, flt(pr_item.qty))
            pr_item.qty = allocated
            remaining -= allocated
            matched_pr_indices.add(pr_idx)
        ext["consumed"] = True
 
    # ── Pass 1: IDF-weighted token overlap ──
    for ext in extracted_items:
        if ext["consumed"]:
            continue
 
        ext_filtered = ext["tokens"] - ubiquitous
 
        candidates = []
        for ic, group in pr_groups.items():
            available = [i for i in group["indices"]
                         if i not in matched_pr_indices]
            if not available:
                continue
            shared = ext_filtered & group["tokens_filtered"]
            if len(shared) < 2:
                continue
            score = sum(1.0 / token_gf[t] for t in shared)
            candidates.append((ic, score, len(shared)))
 
        if not candidates:
            continue
 
        candidates.sort(key=lambda x: (x[2], x[1]), reverse=True)
        best = candidates[0]
 
        if len(candidates) > 1:
            second = candidates[1]
            if best[2] == second[2] and abs(best[1] - second[1]) < 0.01:
                continue  # Ambiguous — skip to Pass 2
 
        best_ic = best[0]
        available = [i for i in pr_groups[best_ic]["indices"]
                     if i not in matched_pr_indices]
        remaining = flt(ext["qty"])
        for pr_idx in available:
            if remaining <= 0:
                break
            pr_item = pr_doc.items[pr_idx]
            allocated = min(remaining, flt(pr_item.qty))
            pr_item.qty = allocated
            remaining -= allocated
            matched_pr_indices.add(pr_idx)
        ext["consumed"] = True
 
    # ── Pass 2: Substring fallback ──
    for pr_idx, pr_item in enumerate(pr_doc.items):
        if pr_idx in matched_pr_indices:
            continue
 
        item_name = cstr(pr_item.item_name).lower()
        item_code = cstr(pr_item.item_code).lower()
        remaining = flt(pr_item.qty)
 
        best_match_idx = None
        best_match_score = -1
 
        for ext_idx, ext in enumerate(extracted_items):
            if ext["consumed"]:
                continue
 
            desc_lower = ext["description"].lower()
 
            match = False
            if desc_lower in item_name or item_name in desc_lower:
                match = True
            elif desc_lower in item_code or item_code in desc_lower:
                match = True
 
            if not match:
                continue
 
            score = 0
            if ext["qty"] and abs(ext["qty"] - remaining) < 0.01:
                score += 10
            if ext["rate"] and flt(pr_item.rate) and abs(ext["rate"] - flt(pr_item.rate)) < 0.01:
                score += 5
            if ext["amount"] and abs(ext["amount"] - flt(pr_item.amount)) < 0.01:
                score += 5
            if score == 0:
                score = 1
 
            if score > best_match_score:
                best_match_score = score
                best_match_idx = ext_idx
 
        if best_match_idx is not None:
            ext = extracted_items[best_match_idx]
            if ext["qty"] > 0:
                pr_item.qty = min(ext["qty"], remaining)
            ext["consumed"] = True
            matched_pr_indices.add(pr_idx)
 
    # ── Item removal decision ──
    total_pr_items = len(pr_doc.items)
    matched_count = len(matched_pr_indices)
    unmatched_count = total_pr_items - matched_count
    consumed_count = sum(1 for e in extracted_items if e["consumed"])
 
    match_ratio = matched_count / total_pr_items if total_pr_items > 0 else 0
 
    frappe.logger("auto_pr").info(
        "Qty adjustment for PO %s: %d/%d PR items matched (%.0f%%), "
        "%d/%d extracted items consumed."
        % (log.purchase_order, matched_count, total_pr_items,
           match_ratio * 100, consumed_count, len(extracted_items))
    )
 
    if unmatched_count == 0:
        return True
 
    can_remove = (
        match_ratio >= 0.5
        and matched_count >= 2
        and matched_count > 0
    )
 
    if can_remove:
        frappe.logger("auto_pr").info(
            "Removing %d unmatched PR items for PO %s "
            "(match ratio %.0f%% >= 50%%, %d matches >= 2). "
            "Treating as partial delivery."
            % (unmatched_count, log.purchase_order,
               match_ratio * 100, matched_count)
        )
        pr_doc.items = [item for idx, item in enumerate(pr_doc.items)
                        if idx in matched_pr_indices]
        for i, item in enumerate(pr_doc.items):
            item.idx = i + 1
        return True
 
    frappe.logger("auto_pr").info(
        "Tier 3 matching FAILED for PO %s (match ratio %.0f%%, "
        "%d matches — below removal threshold). "
        "Invoice total does not match PO total, so full-PO fallback "
        "is not safe. Returning False to route to manual review."
        % (log.purchase_order, match_ratio * 100, matched_count)
    )
    return False


def _create_pr_without_po(log, supplier):
    """
    Create PR without PO linkage.
    Rate priority: ERP last purchase rate > derived from amount/qty > 0 (PM fills in).
    The extracted amount from invoice is stored in description for PM reference.
    """
    pr_doc = frappe.new_doc("Purchase Receipt")
    pr_doc.supplier = supplier
    pr_doc.posting_date = getdate()
    pr_doc.title = frappe.db.get_value("Supplier", supplier, "supplier_name") or supplier
    pr_doc.company = frappe.defaults.get_defaults().get("company", "Bonito Designs Pvt Ltd")
    pr_doc.set_warehouse = (
        frappe.db.get_single_value("Stock Settings", "default_warehouse") or ""
    )

    default_warehouse = pr_doc.set_warehouse

    if log.extracted_items and len(log.extracted_items) > 0:
        for ext_item in log.extracted_items:
            # Skip empty/zero items (Textract sometimes creates phantom rows)
            if not ext_item.quantity and not ext_item.amount and not ext_item.rate:
                continue

            item_code = ext_item.matched_item_code or ""
            if not item_code and ext_item.item_code_extracted:
                if frappe.db.exists("Item", ext_item.item_code_extracted):
                    item_code = ext_item.item_code_extracted

            # Always fall back to placeholder if item doesn't exist in ERP
            if item_code and not frappe.db.exists("Item", item_code):
                item_code = ""

            if not item_code:
                item_code = get_or_create_placeholder_item()

            # Determine rate: ERP rate first, then derive from amount/qty
            rate = _get_item_rate(item_code, log.supplier)
            qty = ext_item.quantity or 1
            ext_amount = flt(ext_item.amount)

            if not rate and ext_amount and qty:
                # Derive rate from extracted amount / quantity
                rate = ext_amount / qty

            # Build description with extracted amount for PM reference
            desc = ext_item.description or "Auto-extracted from invoice"
            if ext_amount:
                desc += " [Invoice amount: %s]" % frappe.format_value(
                    ext_amount, {"fieldtype": "Currency"})

            pr_doc.append("items", {
                "item_code": item_code,
                "item_name": ext_item.description or item_code,
                "description": desc,
                "qty": qty,
                "rate": rate or 0,
                "warehouse": default_warehouse,
                "uom": ext_item.uom or "Nos",
                "stock_uom": ext_item.uom or "Nos",
                "conversion_factor": 1
            })
    else:
        item_code = get_or_create_placeholder_item()
        pr_doc.append("items", {
            "item_code": item_code,
            "item_name": "Invoice from %s" % (
                log.supplier_name_extracted or log.email_from or "Unknown"
            ),
            "description": (
                "Auto-created from email. Invoice No: %s. Total: %s. Requires manual review."
                % (log.supplier_invoice_no or "N/A", log.total_amount or "N/A")
            ),
            "qty": 1,
            "rate": flt(log.total_amount) or 0,
            "warehouse": default_warehouse,
            "uom": "Nos",
            "stock_uom": "Nos",
            "conversion_factor": 1
        })

    return pr_doc


def _get_item_rate(item_code, supplier=None):
    """
    Get the best known rate for an item from ERP.
    Priority: supplier-specific price > last purchase rate > item valuation rate
    Returns rate or 0 if not found.
    """
    if not item_code or item_code == "AUTO-PR-PLACEHOLDER":
        return 0

    # 1. Supplier-specific price from Item Price
    if supplier:
        price = frappe.db.get_value(
            "Item Price",
            {"item_code": item_code, "supplier": supplier,
             "buying": 1, "price_list": frappe.db.get_single_value(
                 "Buying Settings", "buying_price_list") or "Standard Buying"},
            "price_list_rate"
        )
        if price:
            return flt(price)

    # 2. Last purchase rate from Item master
    last_rate = frappe.db.get_value("Item", item_code, "last_purchase_rate")
    if flt(last_rate):
        return flt(last_rate)

    # 3. Valuation rate
    val_rate = frappe.db.get_value("Item", item_code, "valuation_rate")
    if flt(val_rate):
        return flt(val_rate)

    return 0


def get_or_create_placeholder_item():
    """Get or create placeholder item for unmatched items"""
    placeholder_code = "AUTO-PR-PLACEHOLDER"

    if not frappe.db.exists("Item", placeholder_code):
        try:
            item = frappe.get_doc({
                "doctype": "Item",
                "item_code": placeholder_code,
                "item_name": "Auto Purchase Receipt - Placeholder Item",
                "item_group": "Services",
                "stock_uom": "Nos",
                "is_stock_item": 0,
                "description": "Placeholder for auto-created purchase receipts. "
                               "Replace with actual item during review."
            })
            item.insert(ignore_permissions=True)
            frappe.db.commit()
        except frappe.DuplicateEntryError:
            pass

    return placeholder_code


def _attach_invoice_to_pr(log, pr_name):
    """Attach invoice file to Purchase Receipt"""
    try:
        if not log.invoice_attachment:
            return

        file_url = log.invoice_attachment
        file_doc = frappe.db.get_value(
            "File",
            {"file_url": file_url},
            ["name", "file_name", "file_url", "is_private"],
            as_dict=True
        )

        if not file_doc:
            return

        original_file = frappe.get_doc("File", file_doc.name)
        content = original_file.get_content()

        save_file(
            fname=file_doc.file_name,
            content=content,
            dt="Purchase Receipt",
            dn=pr_name,
            is_private=1
        )
    except Exception:
        frappe.log_error(
            title="Auto PR - Attachment Failed - %s" % pr_name,
            message=traceback.format_exc()
        )


# ─────────────────────────────────────────────
# 5. NOTIFICATIONS
# ─────────────────────────────────────────────

def send_notification(log, event_type):
    """Send email notifications based on event type"""
    try:
        recipients = get_notification_recipients(log)
        if not recipients:
            return

        subject = ""
        message = ""

        if event_type == "pr_created":
            pr_link = frappe.utils.get_url_to_form("Purchase Receipt", log.purchase_receipt)
            log_link = frappe.utils.get_url_to_form("Auto Purchase Receipt Log", log.name)

            po_status = ""
            if log.po_tagged:
                po_status = "<p><strong>Purchase Order:</strong> %s &#10004; Tagged</p>" % log.purchase_order
            elif log.needs_manual_po_tagging:
                po_status = "<p><strong>&#9888; Purchase Order not matched - Manual tagging required</strong></p>"

            subject = "[Auto PR] Draft Purchase Receipt Created - %s" % (log.supplier or "Unknown Supplier")
            message = """
                <h3>Draft Purchase Receipt Created</h3>
                <p>An invoice received on the invoice inbox has been processed
                and a draft Purchase Receipt has been created.</p>
                <table style="border-collapse: collapse; width: 100%%;">
                    <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Supplier</strong></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">%s</td></tr>
                    <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Supplier Invoice No</strong></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">%s</td></tr>
                    <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Buyer Order No</strong></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">%s</td></tr>
                    <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Total Amount</strong></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">%s</td></tr>
                    <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Email From</strong></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">%s</td></tr>
                </table>
                %s
                <p><strong>Action Required:</strong> Please verify and submit the Purchase Receipt.</p>
                <p>
                    <a href="%s" style="background: #5e64ff; color: white; padding: 10px 20px;
                       text-decoration: none; border-radius: 5px;">Review Purchase Receipt</a>
                    <a href="%s" style="background: #6c757d; color: white; padding: 10px 20px;
                       text-decoration: none; border-radius: 5px; margin-left: 5px;">View Log</a>
                </p>
            """ % (
                log.supplier or log.supplier_name_extracted or "Unknown",
                log.supplier_invoice_no or "N/A",
                log.buyer_order_no or "N/A",
                frappe.format_value(log.total_amount, {"fieldtype": "Currency"}),
                log.email_from,
                po_status,
                pr_link,
                log_link
            )

        elif event_type == "manual_review":
            log_link = frappe.utils.get_url_to_form("Auto Purchase Receipt Log", log.name)
            subject = "[Auto PR] Manual Review Required - %s" % (
                log.supplier_name_extracted or log.email_from
            )
            message = """
                <h3>&#9888; Manual Review Required</h3>
                <p>An invoice email could not be fully processed.</p>
                <table style="border-collapse: collapse; width: 100%%;">
                    <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Issue</strong></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">%s</td></tr>
                    <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Email From</strong></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">%s</td></tr>
                    <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Subject</strong></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">%s</td></tr>
                </table>
                <p><a href="%s" style="background: #ffc107; color: black; padding: 10px 20px;
                       text-decoration: none; border-radius: 5px;">Review Log Entry</a></p>
            """ % (
                log.error_message or "Unknown issue",
                log.email_from,
                log.email_subject,
                log_link
            )

        elif event_type == "failed":
            log_link = frappe.utils.get_url_to_form("Auto Purchase Receipt Log", log.name)
            subject = "[Auto PR] Processing Failed - %s" % log.email_subject
            message = """
                <h3>&#10060; Invoice Processing Failed</h3>
                <table style="border-collapse: collapse; width: 100%%;">
                    <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Error</strong></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">%s</td></tr>
                    <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Retry Count</strong></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">%s</td></tr>
                    <tr><td style="padding: 8px; border: 1px solid #ddd;"><strong>Email From</strong></td>
                        <td style="padding: 8px; border: 1px solid #ddd;">%s</td></tr>
                </table>
                <p><a href="%s" style="background: #dc3545; color: white; padding: 10px 20px;
                       text-decoration: none; border-radius: 5px;">View Error Details</a></p>
            """ % (
                log.error_message or "Unknown error",
                log.retry_count,
                log.email_from,
                log_link
            )

        if subject and message and recipients:
            frappe.sendmail(
                recipients=recipients,
                subject=subject,
                message=message,
                reference_doctype="Auto Purchase Receipt Log",
                reference_name=log.name,
                now=False
            )

    except Exception:
        frappe.log_error(
            title="Auto PR - Notification Failed - %s" % log.name,
            message=traceback.format_exc()
        )


def get_notification_recipients(log):
    """Get recipients: supplier SPOC + Purchase Managers"""
    recipients = set()

    if log.supplier:
        spoc_email = frappe.db.get_value("Supplier", log.supplier, "spoc_email")
        if spoc_email:
            for em in spoc_email.split(","):
                em = em.strip()
                if em:
                    recipients.add(em)

    purchase_managers = frappe.get_all(
        "Has Role",
        filters={"role": "Purchase Manager", "parenttype": "User"},
        fields=["parent"]
    )
    for pm in purchase_managers:
        user = pm.parent
        if user and user != "Administrator" and frappe.db.get_value("User", user, "enabled"):
            recipients.add(user)

    if not recipients:
        system_managers = frappe.get_all(
            "Has Role",
            filters={"role": "System Manager", "parenttype": "User"},
            fields=["parent"],
            limit=3
        )
        for sm in system_managers:
            user = sm.parent
            if user and user != "Administrator" and frappe.db.get_value("User", user, "enabled"):
                recipients.add(user)

    return list(recipients)


# ─────────────────────────────────────────────
# 6. MANUAL ACTIONS (Whitelisted for UI)
# ─────────────────────────────────────────────

@frappe.whitelist()
def retry_processing(log_name):
    """Retry PR creation for a failed log entry"""
    log = frappe.get_doc("Auto Purchase Receipt Log", log_name)
    if log.processing_status not in ("Failed", "Manual Review"):
        frappe.throw(_("Can only retry Failed or Manual Review entries"))

    # Re-run matching and PR creation
    log.processing_status = "Textract Complete"
    log.error_message = ""
    log.save(ignore_permissions=True)
    frappe.db.commit()

    return create_pr_from_log(log_name)


@frappe.whitelist()
def manually_tag_po(log_name, purchase_order):
    """Manually tag a PO to a log entry"""
    log = frappe.get_doc("Auto Purchase Receipt Log", log_name)

    if not frappe.db.exists("Purchase Order", purchase_order):
        frappe.throw(_("Purchase Order %s not found" % purchase_order))

    po = frappe.get_doc("Purchase Order", purchase_order)
    if po.docstatus != 1:
        frappe.throw(_("Purchase Order %s is not submitted" % purchase_order))
    
    # Warn if supplier doesn't match (manual tagging allows override but flags it)
    if log.supplier and po.supplier != log.supplier:
        frappe.msgprint(
            _("Warning: PO %s belongs to supplier '%s' but this invoice is from '%s'. "
              "Please verify this is the correct PO." % (purchase_order, po.supplier, log.supplier)),
            indicator="orange",
            title=_("Supplier Mismatch")
        )

    log.purchase_order = purchase_order
    log.po_tagged = 1
    log.needs_manual_po_tagging = 0
    log.save(ignore_permissions=True)

    if log.purchase_receipt:
        pr = frappe.get_doc("Purchase Receipt", log.purchase_receipt)
        if pr.docstatus == 0:
            # Delete the old draft PR and recreate from PO
            old_pr_name = pr.name
            frappe.delete_doc("Purchase Receipt", old_pr_name, ignore_permissions=True)
            log.purchase_receipt = ""
            log.save(ignore_permissions=True)
            frappe.db.commit()

            # Recreate using make_purchase_receipt
            new_pr = _create_pr_from_po(log)

            remarks_parts = []
            if log.supplier_invoice_no:
                remarks_parts.append("Supplier Invoice No: %s" % log.supplier_invoice_no)
            remarks_parts.append("PO manually tagged: %s" % purchase_order)
            remarks_parts.append("Log: %s" % log.name)
            new_pr.remarks = " | ".join(remarks_parts)

            new_pr.flags.ignore_permissions = True
            new_pr.flags.ignore_mandatory = True
            new_pr.flags.ignore_validate = True
            new_pr.insert(ignore_permissions=True)

            if log.invoice_attachment:
                _attach_invoice_to_pr(log, new_pr.name)

            log.purchase_receipt = new_pr.name
            log.processing_status = "PR Created"
            log.save(ignore_permissions=True)
            frappe.db.commit()

            return {
                "status": "updated",
                "message": "PR %s recreated from PO %s (old PR %s deleted)" % (
                    new_pr.name, purchase_order, old_pr_name)
            }

    frappe.db.commit()
    return {"status": "tagged", "message": "PO %s tagged to %s" % (purchase_order, log.name)}


@frappe.whitelist()
def create_pr_manually(log_name):
    """Manually trigger PR creation"""
    log = frappe.get_doc("Auto Purchase Receipt Log", log_name)
    if log.purchase_receipt:
        frappe.throw(_("PR %s already exists" % log.purchase_receipt))

    frappe.enqueue(
        "bonito_customizations.auto_purchase_receipt.create_draft_purchase_receipt",
        queue="default",
        timeout=300,
        log_name=log.name,
        is_async=True
    )

    return {"status": "queued", "message": "PR creation queued for %s" % log.name}

