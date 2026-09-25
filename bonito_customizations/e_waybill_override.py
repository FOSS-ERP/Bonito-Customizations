import frappe

from india_compliance.gst_india.utils.e_waybill import EWaybillData


def apply_e_waybill_override():
    if getattr(EWaybillData, "_bonito_e_waybill_patched", False):
        return

    original_get_item_data = EWaybillData.get_item_data

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

    EWaybillData.get_item_data = get_item_data

    # ---------------------------------------------------------
    # Override Party Address Details
    # ---------------------------------------------------------
    original_set_party_address_details = EWaybillData.set_party_address_details

    def set_party_address_details(self):
        original_set_party_address_details(self)

        custom_customer_name = self.doc.get("custom_customer_name_without_pid")

        if custom_customer_name:
            self.bill_to.legal_name = custom_customer_name

    EWaybillData.set_party_address_details = set_party_address_details

    EWaybillData._bonito_e_waybill_patched = True