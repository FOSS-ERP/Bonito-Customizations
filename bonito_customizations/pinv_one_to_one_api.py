"""
PINV One-to-One: Client API Only
==================================

This file ONLY contains the whitelisted API method that the Purchase Receipt
client script calls to check if a PINV already exists.

The actual validation logic lives in the Server Script (Purchase Invoice → validate).

Installation:
    1. Place at: apps/bonito_customizations/bonito_customizations/pinv_one_to_one_api.py
    2. No hooks.py changes needed (no doc_events)
    3. bench --site <site> clear-cache

Author: Bonito Designs Tech Team
Date:   March 2026
"""

import frappe
import json


@frappe.whitelist()
def check_pre_already_invoiced(pre_names, exclude_pinv=""):
    """
    Client-callable method to check if any of the given PREs already
    have a linked PINV. Used by the Purchase Receipt client script
    to show an error when the user clicks "Create > Purchase Invoice".

    Args:
        pre_names: list of Purchase Receipt names (or JSON string)
        exclude_pinv: current PINV name to exclude from the check

    Returns:
        dict: {has_conflicts: bool, conflicts: {pre_name: [pinv_names]}}
    """
    if isinstance(pre_names, str):
        pre_names = json.loads(pre_names)

    if not pre_names:
        return {"has_conflicts": False, "conflicts": {}}

    existing = frappe.db.sql("""
        SELECT DISTINCT
            pii.purchase_receipt,
            pii.parent AS pinv_name
        FROM `tabPurchase Invoice Item` pii
        JOIN `tabPurchase Invoice` pi ON pi.name = pii.parent
        WHERE pii.purchase_receipt IN %(pre_names)s
          AND pi.docstatus < 2
          AND pi.name != %(exclude)s
    """, {
        "pre_names": pre_names,
        "exclude": exclude_pinv or "",
    }, as_dict=True)

    if not existing:
        return {"has_conflicts": False, "conflicts": {}}

    conflicts = {}
    for row in existing:
        conflicts.setdefault(row["purchase_receipt"], []).append(row["pinv_name"])

    return {"has_conflicts": True, "conflicts": conflicts}


@frappe.whitelist()
def check_po_already_invoiced(po_name):
    """
    Client-callable method to check if a Purchase Order already has a
    PINV (draft or submitted) linked via its Purchase Receipt items.

    Flow: PO → PRE items (linked to PO) → any PINV referencing those PRE items?

    Also checks for PINVs created directly from the PO (no PRE in between).

    Args:
        po_name: Purchase Order name

    Returns:
        dict: {has_conflicts: bool, pinv_names: [str], pre_names: [str], via: "pre"|"po"|"both"}
    """
    if not po_name:
        return {"has_conflicts": False, "pinv_names": [], "pre_names": [], "via": ""}

    # Check 1: PINVs linked via PRE
    # Find PREs that have items from this PO, then check if those PREs have PINVs
    pinvs_via_pre = frappe.db.sql("""
        SELECT DISTINCT
            pii.parent AS pinv_name,
            pii.purchase_receipt AS pre_name
        FROM `tabPurchase Invoice Item` pii
        JOIN `tabPurchase Invoice` pi ON pi.name = pii.parent
        WHERE pii.purchase_receipt IN (
            SELECT DISTINCT parent
            FROM `tabPurchase Receipt Item`
            WHERE purchase_order = %(po_name)s
        )
          AND pi.docstatus < 2
    """, {"po_name": po_name}, as_dict=True)

    # Check 2: PINVs linked directly to this PO (no PRE)
    pinvs_via_po = frappe.db.sql("""
        SELECT DISTINCT
            pii.parent AS pinv_name
        FROM `tabPurchase Invoice Item` pii
        JOIN `tabPurchase Invoice` pi ON pi.name = pii.parent
        WHERE pii.purchase_order = %(po_name)s
          AND pi.docstatus < 2
    """, {"po_name": po_name}, as_dict=True)

    all_pinv_names = []
    all_pre_names = []

    for r in pinvs_via_pre:
        if r["pinv_name"] not in all_pinv_names:
            all_pinv_names.append(r["pinv_name"])
        if r["pre_name"] not in all_pre_names:
            all_pre_names.append(r["pre_name"])

    for r in pinvs_via_po:
        if r["pinv_name"] not in all_pinv_names:
            all_pinv_names.append(r["pinv_name"])

    if not all_pinv_names:
        return {"has_conflicts": False, "pinv_names": [], "pre_names": [], "via": ""}

    via = "both" if pinvs_via_pre and pinvs_via_po else ("pre" if pinvs_via_pre else "po")

    return {
        "has_conflicts": True,
        "pinv_names": all_pinv_names,
        "pre_names": all_pre_names,
        "via": via,
    }

