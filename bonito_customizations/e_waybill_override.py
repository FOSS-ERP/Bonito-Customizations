import frappe

import india_compliance.gst_india.utils.e_waybill as ewb_module
from india_compliance.gst_india.utils.e_waybill import EWaybillData


def apply_e_waybill_override():
    if getattr(EWaybillData, "_bonito_e_waybill_patched", False):
        return

    original_get_item_data = EWaybillData.get_item_data
    original_get_data = EWaybillData.get_data
    original_generate_e_waybill = ewb_module._generate_e_waybill

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

        if description:
            description = frappe.utils.strip_html(description).strip()

            if description:
                data["productDesc"] = self.sanitize_value(
                    description,
                    regex=3,
                    max_length=300,
                )

        return data

    def get_data(self, *args, **kwargs):
        # Always regenerate e-waybill from fresh goods data,
        # never via the IRN-linked shortcut
        kwargs["with_irn"] = False
        data = original_get_data(self, *args, **kwargs)₹₹

        # Override toTrdName
        custom_customer_name = self.doc.get("custom_customer_name_without_pid")
        if custom_customer_name:
            data["toTrdName"] = custom_customer_name


    def patched_generate_e_waybill(doc, throw=True, force=False):
        # Temporarily hide the IRN so with_irn evaluates to False,
        # forcing the EWaybillAPI (goods-based) path instead of EInvoiceAPI
        original_irn = doc.irn
        doc.irn = None
        try:
            return original_generate_e_waybill(doc, throw=throw, force=force)
        finally:
            doc.irn = original_irn  # restore, don't corrupt the actual document

    EWaybillData.get_item_data = get_item_data
    EWaybillData.get_data = get_data
    ewb_module._generate_e_waybill = patched_generate_e_waybill
    EWaybillData._bonito_e_waybill_patched = True