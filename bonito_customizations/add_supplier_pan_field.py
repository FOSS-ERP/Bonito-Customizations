"""
Migration: Add supplier_pan field to Auto Purchase Receipt Log

Run:
  bench --site your-site execute bonito_customizations.add_supplier_pan_field.execute
"""

import frappe


def execute():
    doctype = "Auto Purchase Receipt Log"

    if not frappe.db.exists("DocType", doctype):
        print("DocType %s does not exist. Run full setup first." % doctype)
        return

    # Check if field already exists
    if frappe.db.exists("Custom Field", {"dt": doctype, "fieldname": "supplier_pan"}):
        print("Field supplier_pan already exists on %s. Skipping." % doctype)
        return

    # Add field after supplier_gstin
    from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

    create_custom_fields({
        doctype: [
            {
                "fieldname": "supplier_pan",
                "fieldtype": "Data",
                "label": "Supplier PAN (Extracted)",
                "read_only": 1,
                "length": 10,
                "insert_after": "supplier_gstin",
            }
        ]
    })

    frappe.db.commit()
    print("Added supplier_pan field to %s (after supplier_gstin)." % doctype)
