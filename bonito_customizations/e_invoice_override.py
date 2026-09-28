import frappe

from india_compliance.gst_india.constants import SERVICE_HSN_PREFIX
from india_compliance.gst_india.utils.e_invoice import EInvoiceData


def apply_e_invoice_override():
    if getattr(EInvoiceData, "_bonito_e_invoice_patched", False):
        return

    original_get_item_data = EInvoiceData.get_item_data
    original_get_invoice_data = EInvoiceData.get_invoice_data

    def get_item_data(self, item_details):
        data = original_get_item_data(self, item_details)

        item = next(
            (row for row in self.doc.items if row.idx == item_details.item_no),
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
                data["IsServc"] = (
                    "Y" if custom_sac_clean.startswith(SERVICE_HSN_PREFIX) else "N"
                )

        return data

    def get_invoice_data(self):
        data = original_get_invoice_data(self)

        customer_name = (
            getattr(self.doc, "custom_customer_name_without_pid", None) or ""
        ).strip()

        if customer_name and isinstance(data, dict) and data.get("BuyerDtls"):
            name = self.sanitize_value(customer_name, regex=3, max_length=100)
            data["BuyerDtls"]["LglNm"] = name
            data["BuyerDtls"]["TrdNm"] = name

        return data

    EInvoiceData.get_item_data = get_item_data
    EInvoiceData.get_invoice_data = get_invoice_data
    EInvoiceData._bonito_e_invoice_patched = True