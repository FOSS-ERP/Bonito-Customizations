# -*- coding: utf-8 -*-
"""
Setup script to create the custom field on Purchase Invoice for TDS Challan linkage.

Run via bench console or as a one-time patch:
    bench --site <site> execute bonito_customizations.patches.setup_tds_challan_fields.execute
"""

from __future__ import unicode_literals
import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def execute():
    """Create custom fields needed for TDS Challan tracking on both
    Purchase Invoice and Journal Entry.

    IMPORTANT: These are deliberately Data fields, NOT Link fields.
    Using Link would cause Frappe's get_submitted_linked_docs to scan
    all 700+ PIs recursively on every TDS Challan cancel, causing timeouts.
    Data fields store the challan name but are invisible to the link scanner.
    """

    custom_fields = {
        "Purchase Invoice": [
            {
                "fieldname": "custom_tds_challan",
                "label": "TDS Challan",
                "fieldtype": "Data",
                "insert_after": "tax_withholding_category",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "description": "Linked TDS Challan (auto-set when challan is submitted)",
                "allow_on_submit": 1,
            }
        ],
        "Journal Entry": [
            {
                "fieldname": "custom_tds_challan",
                "label": "TDS Challan",
                "fieldtype": "Data",
                "insert_after": "remark",
                "read_only": 1,
                "no_copy": 1,
                "print_hide": 1,
                "description": "Linked TDS Challan (auto-set when challan is submitted)",
                "allow_on_submit": 1,
            }
        ],
    }

    create_custom_fields(custom_fields, update=True)
    frappe.db.commit()
    print("✓ Custom field 'custom_tds_challan' created on Purchase Invoice and Journal Entry")


def teardown():
    """Remove custom fields (use with caution)"""
    frappe.delete_doc_if_exists("Custom Field", "Purchase Invoice-custom_tds_challan")
    frappe.delete_doc_if_exists("Custom Field", "Journal Entry-custom_tds_challan")
    frappe.db.commit()
    print("✓ Custom fields removed")
