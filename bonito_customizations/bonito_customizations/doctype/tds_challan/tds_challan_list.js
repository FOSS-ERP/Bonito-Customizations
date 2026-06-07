// Copyright (c) 2026, Bonito Designs Pvt Ltd and contributors
// For license information, please see license.txt

frappe.listview_settings["TDS Challan"] = {
  add_fields: ["status", "total_challan_amount", "total_tds_amount", "difference"],

  get_indicator: function (doc) {
    if (doc.docstatus === 0) {
      return [__("Draft"), "red", "status,=,Draft"];
    }
    if (doc.status === "Reconciled") {
      return [__("Reconciled"), "green", "status,=,Reconciled"];
    }
    if (doc.status === "Partially Reconciled") {
      return [__("Partially Reconciled"), "orange", "status,=,Partially Reconciled"];
    }
    if (doc.docstatus === 2) {
      return [__("Cancelled"), "darkgrey", "docstatus,=,2"];
    }
  },
};
