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
                for row in self._items
                if row.idx == item_details.item_no
            ),
            None,
        )

        if item:
            description = getattr(item, "description", None)

            if description:
                data["productDesc"] = self.sanitize_value(
                    description,
                    regex=3,
                    max_length=300,
                )

        return data

    EWaybillData.get_item_data = get_item_data
    EWaybillData._bonito_e_waybill_patched = True