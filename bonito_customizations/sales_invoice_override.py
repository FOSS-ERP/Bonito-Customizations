import frappe


def set_fallback_eway_bill_distance(doc, method=None):
    """
    Auto-fills e-way bill distance if NIC has no distance master
    for the pincode pair, to avoid NIC error 4030
    ('Could not retrieve distance').
    """
    if getattr(doc, "distance", None):
        return

    company_address = getattr(doc, "company_address", None)
    customer_address = getattr(doc, "customer_address", None)

    if not (company_address and customer_address):
        return

    from_pincode = frappe.db.get_value("Address", company_address, "pincode")
    to_pincode = frappe.db.get_value("Address", customer_address, "pincode")

    if not (from_pincode and to_pincode):
        return

    distance = frappe.db.get_value(
        "Pincode Distance Master",
        {"from_pincode": from_pincode, "to_pincode": to_pincode},
        "distance_km",
    )

    if distance:
        doc.distance = distance
        frappe.msgprint(
            f"E-Way Bill distance auto-filled as {distance} km "
            f"({from_pincode} → {to_pincode})",
            indicator="blue",
            alert=True,
        )