from india_compliance.gst_india.utils.e_invoice import EInvoiceData


def apply_e_invoice_override():
    if getattr(EInvoiceData, "_bonito_e_invoice_patched", False):
        return

    original_get_item_data = EInvoiceData.get_item_data

    def get_item_data(self, item_details):
        data = original_get_item_data(self, item_details)

        # Find the original Sales Invoice Item row.
        item = next(
            (
                row
                for row in self._items
                if row.idx == item_details.item_no
            ),
            None,
        )

        if item:
            # Use item_description for e-Invoice description.
            data["PrdDesc"] = self.sanitize_value(
                item.description,
                regex=3,
                max_length=300,
            )

            # Use custom_sac for e-Invoice HSN/SAC.
            data["HsnCd"] = item.custom_sac

        return data

    EInvoiceData.get_item_data = get_item_data
    EInvoiceData._bonito_e_invoice_patched = True