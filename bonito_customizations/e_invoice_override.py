import frappe

from india_compliance.gst_india.utils.e_invoice import EInvoiceData


def apply_e_invoice_override():
    if getattr(EInvoiceData, "_bonito_e_invoice_patched", False):
        return

    original_get_item_data = EInvoiceData.get_item_data

    def get_item_data(self, item_details):
        data = original_get_item_data(self, item_details)

        item = next(
            (
                row
                for row in self.doc.items
                if row.idx == item_details.item_no
            ),
            None,
        )

        if not item:
            return data

        description = getattr(item, "description", None)
        custom_sac = getattr(item, "custom_sac", None)

        if description:
            description = frappe.utils.strip_html(description).strip()

            if description:
                data["PrdDesc"] = self.sanitize_value(
                    description,
                    regex=3,
                    max_length=300,
                )

        if custom_sac:
            custom_sac_clean = str(custom_sac).strip()
            if custom_sac_clean.isdigit() and len(custom_sac_clean) in (4, 6, 8):
                data["HsnCd"] = custom_sac_clean

        return data

    EInvoiceData.get_item_data = get_item_data
    EInvoiceData._bonito_e_invoice_patched = True