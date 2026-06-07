"""
Setup script for Auto Purchase Receipt system (v2 - Lambda architecture).
Run: bench --site your-site execute bonito_customizations.auto_purchase_receipt_setup.setup
"""

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def setup():
    print("Setting up Auto Purchase Receipt system (v2 - Lambda)...")

    create_child_doctype()
    create_parent_doctype()
    create_supplier_custom_fields()
    create_placeholder_item()
    create_indexes()

    frappe.db.commit()
    print("\nSetup complete!")
    print("\nNext steps:")
    print("1. Deploy Lambda function with IMAP + Textract logic")
    print("2. Create API key/secret in ERPNext for Lambda access")
    print("3. Store secrets in AWS Secrets Manager")
    print("4. Set up EventBridge rule to trigger Lambda every 5 minutes")
    print("5. Populate supplier_email_domain on Supplier records")
    print("6. Add Client Scripts for the Log doctype")
    print("7. bench --site your-site migrate && clear-cache")


def create_child_doctype():
    if frappe.db.exists("DocType", "Auto Purchase Receipt Log Item"):
        print("  Auto Purchase Receipt Log Item already exists.")
        return

    print("  Creating Auto Purchase Receipt Log Item...")
    doc = frappe.get_doc({
        "doctype": "DocType",
        "name": "Auto Purchase Receipt Log Item",
        "module": "Buying",
        "custom": 1,
        "istable": 1,
        "fields": [
            {"fieldname": "description", "fieldtype": "Data", "label": "Description", "in_list_view": 1},
            {"fieldname": "quantity", "fieldtype": "Float", "label": "Quantity", "in_list_view": 1},
            {"fieldname": "rate", "fieldtype": "Currency", "label": "Rate", "in_list_view": 1},
            {"fieldname": "amount", "fieldtype": "Currency", "label": "Amount", "in_list_view": 1},
            {"fieldname": "item_code_extracted", "fieldtype": "Data", "label": "Item Code (Extracted)"},
            {"fieldname": "matched_item_code", "fieldtype": "Link", "label": "Matched Item Code", "options": "Item"},
            {"fieldname": "uom", "fieldtype": "Data", "label": "UOM"},
            {"fieldname": "hsn_sac", "fieldtype": "Data", "label": "HSN/SAC"}
        ]
    })
    doc.insert(ignore_permissions=True)
    print("  Done.")


def create_parent_doctype():
    if frappe.db.exists("DocType", "Auto Purchase Receipt Log"):
        print("  Auto Purchase Receipt Log already exists.")
        return

    print("  Creating Auto Purchase Receipt Log...")
    doc = frappe.get_doc({
        "doctype": "DocType",
        "name": "Auto Purchase Receipt Log",
        "module": "Buying",
        "custom": 1,
        "naming_rule": "Expression (old style)",
        "autoname": "APRL-.#####",
        "track_changes": 1,
        "sort_field": "creation",
        "sort_order": "DESC",
        "title_field": "email_subject",
        "fields": [
            # Email Details
            {"fieldname": "section_email", "fieldtype": "Section Break", "label": "Email Details"},
            {"fieldname": "email_subject", "fieldtype": "Data", "label": "Email Subject", "read_only": 1},
            {"fieldname": "email_from", "fieldtype": "Data", "label": "Email From", "read_only": 1},
            {"fieldname": "email_date", "fieldtype": "Datetime", "label": "Email Date", "read_only": 1},
            {"fieldname": "email_message_id", "fieldtype": "Data", "label": "Email Message-ID",
             "read_only": 1, "hidden": 1, "unique": 1},
            {"fieldname": "attachment_hash", "fieldtype": "Data", "label": "Attachment Hash",
             "read_only": 1, "hidden": 1,
             "description": "SHA-256 hash of invoice attachment for content-level dedup"},
            {"fieldname": "communication", "fieldtype": "Link", "label": "Communication",
             "options": "Communication", "read_only": 1},
            {"fieldname": "column_break_email", "fieldtype": "Column Break"},
            {"fieldname": "processing_status", "fieldtype": "Select", "label": "Processing Status",
             "options": "\nPending\nProcessing\nTextract Complete\nPR Created\nFailed\nManual Review",
             "default": "Pending", "in_list_view": 1, "in_standard_filter": 1},
            {"fieldname": "error_message", "fieldtype": "Small Text", "label": "Error / Notes", "read_only": 1},
            {"fieldname": "retry_count", "fieldtype": "Int", "label": "Retry Count",
             "default": "0", "read_only": 1},

            # Extracted Information
            {"fieldname": "section_extracted", "fieldtype": "Section Break", "label": "Extracted Information"},
            {"fieldname": "supplier_name_extracted", "fieldtype": "Data",
             "label": "Supplier Name (Extracted)", "read_only": 1},
            {"fieldname": "supplier", "fieldtype": "Link", "label": "Matched Supplier",
             "options": "Supplier", "in_list_view": 1, "in_standard_filter": 1},
            {"fieldname": "supplier_invoice_no", "fieldtype": "Data", "label": "Supplier Invoice No",
             "read_only": 1, "in_list_view": 1},
            {"fieldname": "column_break_extracted", "fieldtype": "Column Break"},
            {"fieldname": "buyer_order_no", "fieldtype": "Data",
             "label": "Buyer Order No (Extracted)", "read_only": 1},
            {"fieldname": "purchase_order", "fieldtype": "Link", "label": "Matched Purchase Order",
             "options": "Purchase Order", "in_list_view": 1, "in_standard_filter": 1},
            {"fieldname": "total_amount", "fieldtype": "Currency",
             "label": "Total Amount (Extracted)", "read_only": 1},
            {"fieldname": "invoice_date_extracted", "fieldtype": "Data",
             "label": "Invoice Date (Extracted)", "read_only": 1},
            {"fieldname": "supplier_gstin", "fieldtype": "Data",
             "label": "Supplier GSTIN (Extracted)", "read_only": 1},

            # Extracted Items
            {"fieldname": "section_items", "fieldtype": "Section Break", "label": "Extracted Line Items"},
            {"fieldname": "extracted_items", "fieldtype": "Table", "label": "Extracted Items",
             "options": "Auto Purchase Receipt Log Item"},

            # Raw JSON
            {"fieldname": "section_raw", "fieldtype": "Section Break",
             "label": "Raw Textract Output", "collapsible": 1},
            {"fieldname": "raw_textract_json", "fieldtype": "Code",
             "label": "Raw Textract JSON", "read_only": 1, "options": "JSON"},

            # Result
            {"fieldname": "section_result", "fieldtype": "Section Break", "label": "Result"},
            {"fieldname": "purchase_receipt", "fieldtype": "Link", "label": "Purchase Receipt",
             "options": "Purchase Receipt", "read_only": 1, "in_list_view": 1},
            {"fieldname": "column_break_result", "fieldtype": "Column Break"},
            {"fieldname": "po_tagged", "fieldtype": "Check", "label": "PO Tagged",
             "read_only": 1, "default": "0"},
            {"fieldname": "needs_manual_po_tagging", "fieldtype": "Check",
             "label": "Needs Manual PO Tagging", "read_only": 1, "default": "0"},

            # Attachments
            {"fieldname": "section_attachments", "fieldtype": "Section Break", "label": "Attachments"},
            {"fieldname": "invoice_attachment", "fieldtype": "Attach",
             "label": "Invoice Attachment", "read_only": 1},

            {"fieldname": "amended_from", "fieldtype": "Link", "label": "Amended From",
             "options": "Auto Purchase Receipt Log", "read_only": 1, "hidden": 1}
        ],
        "permissions": [
            {"role": "Purchase Manager", "read": 1, "write": 1, "create": 1, "delete": 1,
             "print": 1, "email": 1},
            {"role": "Purchase User", "read": 1, "write": 1, "print": 1, "email": 1},
            {"role": "Accounts Manager", "read": 1, "write": 1, "print": 1, "email": 1}
        ]
    })
    doc.insert(ignore_permissions=True)
    print("  Done.")


def create_supplier_custom_fields():
    print("  Creating custom fields on Supplier...")
    custom_fields = {
        "Supplier": [
            {
                "fieldname": "supplier_email_domain",
                "fieldtype": "Data",
                "label": "Email Domain (for auto-matching)",
                "description": "e.g. 'vendor.com' - used to auto-match invoice emails",
                "insert_after": "supplier_name",
                "unique": 0
            }
        ]
    }

    if not frappe.db.exists("Custom Field", {"dt": "Supplier", "fieldname": "spoc_email"}):
        custom_fields["Supplier"].append({
            "fieldname": "spoc_email",
            "fieldtype": "Small Text",
            "label": "SPOC Email(s)",
            "description": "Comma-separated SPOC emails for purchase notifications",
            "insert_after": "supplier_email_domain"
        })

    create_custom_fields(custom_fields, update=True)
    print("  Done.")


def create_placeholder_item():
    placeholder_code = "AUTO-PR-PLACEHOLDER"
    if frappe.db.exists("Item", placeholder_code):
        print("  Placeholder item already exists.")
        return

    print("  Creating placeholder item...")
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
    print("  Done.")


def create_indexes():
    """Add database indexes on fields used in batch dedup IN queries."""
    print("  Creating indexes...")
    table = "tabAuto Purchase Receipt Log"

    # attachment_hash — used in IN query for content-level dedup
    # email_message_id already gets an index from unique=1, but add explicitly
    # in case the DocType was created without it
    indexes = [
        ("idx_aprl_attachment_hash", "attachment_hash"),
        ("idx_aprl_email_message_id", "email_message_id"),
    ]

    for idx_name, column in indexes:
        try:
            # Check if index already exists
            existing = frappe.db.sql(
                "SHOW INDEX FROM `%s` WHERE Key_name = %%s" % table,
                (idx_name,)
            )
            if not existing:
                frappe.db.sql(
                    "ALTER TABLE `%s` ADD INDEX `%s` (`%s`)" % (table, idx_name, column)
                )
                print("    Created index %s on %s" % (idx_name, column))
            else:
                print("    Index %s already exists" % idx_name)
        except Exception as e:
            # Table may not exist yet if DocType creation failed
            print("    Could not create index %s: %s" % (idx_name, e))

    print("  Done.")


if __name__ == "__main__":
    setup()

