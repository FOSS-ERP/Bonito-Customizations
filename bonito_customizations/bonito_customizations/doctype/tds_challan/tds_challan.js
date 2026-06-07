// Copyright (c) 2026, Bonito Designs Pvt Ltd and contributors
// For license information, please see license.txt

frappe.ui.form.on("TDS Challan", {
  onload: function (frm) {
    frm.ignore_doctypes_on_cancel_all = ["Purchase Invoice", "Journal Entry"];
  },

  refresh: function (frm) {
    frm.ignore_doctypes_on_cancel_all = ["Purchase Invoice", "Journal Entry"];

    // Render the accounts HTML on refresh (for saved forms)
    if (frm.doc.tds_section && frm.doc.tds_account_head) {
      render_accounts_html(frm);
    }

    // Dashboard headline for submitted challans
    if (frm.doc.docstatus === 1) {
      if (frm.doc.status === "Reconciled") {
        frm.dashboard.set_headline(
          __("Fully Reconciled — Unallocated: ₹0.00"),
          "green"
        );
      } else {
        frm.dashboard.set_headline(
          __("Unallocated Amount: ₹{0}", [
            flt(frm.doc.unallocated_amount).toFixed(2),
          ]),
          "orange"
        );
      }
    }

    // Custom button to link payment entry after submission
    if (frm.doc.docstatus === 1 && !frm.doc.payment_entry) {
      frm.add_custom_button(__("Link Payment Entry"), function () {
        frappe.prompt(
          [
            {
              fieldname: "ref_type",
              label: __("Reference Type"),
              fieldtype: "Select",
              options: "Payment Entry\nJournal Entry",
              default: "Payment Entry",
              reqd: 1,
            },
            {
              fieldname: "ref_name",
              label: __("Reference Name"),
              fieldtype: "Dynamic Link",
              options: "ref_type",
              reqd: 1,
            },
          ],
          function (values) {
            frappe.call({
              method: "frappe.client.set_value",
              args: {
                doctype: "TDS Challan",
                name: frm.doc.name,
                fieldname: {
                  payment_ref_type: values.ref_type,
                  payment_entry: values.ref_name,
                },
              },
              callback: function () {
                frm.reload_doc();
                frappe.show_alert(
                  { message: __("Payment linked"), indicator: "green" },
                  3
                );
              },
            });
          },
          __("Link Payment to Challan"),
          __("Link")
        );
      });
    }
  },

  tds_section: function (frm) {
    // When section changes, resolve all matching accounts
    if (!frm.doc.tds_section || !frm.doc.company) {
      frm.set_value("tds_account_head", "");
      frm.set_value("tds_section_code", "");
      clear_accounts_html(frm);
      return;
    }

    frappe.call({
      method:
        "bonito_customizations.bonito_customizations.doctype.tds_challan.tds_section_mapping.resolve_section_accounts",
      args: {
        tds_section: frm.doc.tds_section,
        company: frm.doc.company,
      },
      callback: function (r) {
        if (!r.message) return;

        var result = r.message;

        // Store the section code
        frm.set_value("tds_section_code", result.section_code);

        // Store the account list as JSON
        frm.set_value("tds_account_head", JSON.stringify(result.accounts));

        // Render the matched accounts in the HTML field
        render_accounts_detail(frm, result);

        if (!result.accounts.length) {
          frappe.msgprint(
            __(
              "No TDS accounts found for section {0} in company {1}. " +
                "Check that Tax Withholding Categories exist with the correct " +
                "section code and are linked to accounts for this company.",
              [result.section_code, frm.doc.company]
            ),
            __("No Accounts Found")
          );
        } else {
          frappe.show_alert(
            {
              message: __(
                "Found {0} account(s) across {1} withholding categories for section {2}",
                [
                  result.accounts.length,
                  result.categories.length,
                  result.section_code,
                ]
              ),
              indicator: "green",
            },
            5
          );
        }
      },
    });
  },

  company: function (frm) {
    // Re-resolve accounts when company changes
    if (frm.doc.tds_section) {
      frm.trigger("tds_section");
    }
  },

  fetch_invoices: function (frm) {
    // Validate required fields
    if (!frm.doc.company) {
      frappe.msgprint(__("Please set Company first"));
      return;
    }
    if (!frm.doc.tds_section) {
      frappe.msgprint(__("Please select a TDS Section first"));
      return;
    }
    if (!frm.doc.tds_account_head) {
      frappe.msgprint(
        __(
          "No TDS accounts resolved for this section. Please re-select the TDS Section."
        )
      );
      return;
    }
    if (!frm.doc.period_from || !frm.doc.period_to) {
      frappe.msgprint(__("Please set Period From and Period To first"));
      return;
    }
    if (!flt(frm.doc.total_challan_amount)) {
      frappe.msgprint(
        __(
          "Please enter the Total Challan Amount first. Vouchers will be allocated FIFO up to this amount."
        )
      );
      return;
    }

    // Parse account list for display
    var account_list = [];
    try {
      account_list = JSON.parse(frm.doc.tds_account_head);
    } catch (e) {
      account_list = [frm.doc.tds_account_head];
    }

    // Collect existing vouchers
    var existing = [];
    (frm.doc.entries || []).forEach(function (row) {
      if (row.voucher_no) {
        existing.push({
          voucher_type: row.voucher_type,
          voucher_no: row.voucher_no,
          allocated_amount: flt(row.allocated_amount),
        });
      }
    });

    var allocatable =
      flt(frm.doc.total_challan_amount) -
      flt(frm.doc.total_interest) -
      flt(frm.doc.total_late_fee);

    frappe.confirm(
      __(
        "This will fetch Purchase Invoices and Journal Entries with TDS deductions " +
          "for section <b>{0}</b> across <b>{1} account(s)</b>. " +
          "Vouchers will be allocated FIFO up to ₹{2} " +
          "(Challan Amount minus Interest/Late Fee). " +
          "Already-linked vouchers will be skipped. Continue?",
        [
          frm.doc.tds_section_code || frm.doc.tds_section,
          account_list.length,
          allocatable.toFixed(2),
        ]
      ),
      function () {
        frappe.call({
          method:
            "bonito_customizations.bonito_customizations.doctype.tds_challan.tds_challan.fetch_tds_vouchers",
          args: {
            company: frm.doc.company,
            tds_account_head: frm.doc.tds_account_head,
            period_from: frm.doc.period_from,
            period_to: frm.doc.period_to,
            total_challan_amount: frm.doc.total_challan_amount,
            total_interest: frm.doc.total_interest || 0,
            total_late_fee: frm.doc.total_late_fee || 0,
            challan_name: frm.doc.name || "",
            existing_vouchers: JSON.stringify(existing),
          },
          freeze: true,
          freeze_message: __("Fetching TDS vouchers across all section accounts..."),
          callback: function (r) {
            if (!r.message) return;

            var result = r.message;
            var new_entries = result.entries || [];

            new_entries.forEach(function (entry) {
              var row = frm.add_child("entries");
              row.voucher_type = entry.voucher_type;
              row.voucher_no = entry.voucher_no;
              row.supplier = entry.supplier;
              row.supplier_name = entry.supplier_name;
              row.supplier_pan = entry.supplier_pan;
              row.posting_date = entry.posting_date;
              row.bill_no = entry.bill_no;
              row.bill_date = entry.bill_date;
              row.invoice_net_total = entry.invoice_net_total;
              row.invoice_grand_total = entry.invoice_grand_total;
              row.tds_amount = entry.tds_amount;
              row.allocated_amount = entry.allocated_amount;
              row.tax_withholding_category = entry.tax_withholding_category;
              row.tds_account = entry.tds_account;
              row.purchase_order = entry.purchase_order;
              row.purchase_receipt = entry.purchase_receipt;
              row.project = entry.project;
              row.cost_center = entry.cost_center;
              row.je_remark = entry.je_remark;
            });

            frm.refresh_field("entries");
            calculate_totals(frm);

            // Build summary message
            var parts = [__("{0} voucher(s) added", [result.added])];
            if (result.skipped_linked) {
              parts.push(
                __("{0} skipped (linked to another challan)", [
                  result.skipped_linked,
                ])
              );
            }
            if (result.skipped_duplicate) {
              parts.push(
                __("{0} skipped (already in this challan)", [
                  result.skipped_duplicate,
                ])
              );
            }
            if (flt(result.remaining) > 0) {
              parts.push(
                __("₹{0} still unallocated", [
                  flt(result.remaining).toFixed(2),
                ])
              );
            }

            frappe.show_alert(
              {
                message: parts.join(". "),
                indicator: result.added > 0 ? "green" : "orange",
              },
              10
            );

            frm.dirty();
          },
        });
      }
    );
  },

  quarter: function (frm) {
    if (!frm.doc.quarter || !frm.doc.financial_year) return;

    frappe.db.get_value(
      "Fiscal Year",
      frm.doc.financial_year,
      ["year_start_date", "year_end_date"],
      function (r) {
        if (!r) return;

        var start_year = new Date(r.year_start_date).getFullYear();
        var end_year = new Date(r.year_end_date).getFullYear();
        var from_date, to_date;

        if (frm.doc.quarter === "Q1 (Apr-Jun)") {
          from_date = start_year + "-04-01";
          to_date = start_year + "-06-30";
        } else if (frm.doc.quarter === "Q2 (Jul-Sep)") {
          from_date = start_year + "-07-01";
          to_date = start_year + "-09-30";
        } else if (frm.doc.quarter === "Q3 (Oct-Dec)") {
          from_date = start_year + "-10-01";
          to_date = start_year + "-12-31";
        } else if (frm.doc.quarter === "Q4 (Jan-Mar)") {
          from_date = end_year + "-01-01";
          to_date = end_year + "-03-31";
        }

        if (from_date && to_date) {
          frm.set_value("period_from", from_date);
          frm.set_value("period_to", to_date);
        }
      }
    );
  },

  total_challan_amount: function (frm) {
    calculate_unallocated(frm);
  },
  total_interest: function (frm) {
    calculate_unallocated(frm);
  },
  total_late_fee: function (frm) {
    calculate_unallocated(frm);
  },
});

frappe.ui.form.on("TDS Challan Entry", {
  allocated_amount: function (frm, cdt, cdn) {
    var row = locals[cdt][cdn];
    if (flt(row.allocated_amount) > flt(row.tds_amount)) {
      frappe.model.set_value(cdt, cdn, "allocated_amount", row.tds_amount);
      frappe.msgprint(
        __("Row {0}: Allocated amount cannot exceed TDS amount", [row.idx])
      );
    }
    calculate_totals(frm);
  },

  tds_amount: function (frm) {
    calculate_totals(frm);
  },

  entries_remove: function (frm) {
    calculate_totals(frm);
  },
});

// ---------------------------------------------------------------------------
// Helper functions
// ---------------------------------------------------------------------------

function calculate_totals(frm) {
  var total_tds = 0;
  var total_alloc = 0;
  (frm.doc.entries || []).forEach(function (row) {
    total_tds += flt(row.tds_amount);
    total_alloc += flt(row.allocated_amount);
  });
  frm.set_value("total_tds_amount", flt(total_tds, 2));
  frm.set_value("allocated_amount", flt(total_alloc, 2));
  calculate_unallocated(frm);
}

function calculate_unallocated(frm) {
  var allocatable =
    flt(frm.doc.total_challan_amount) -
    flt(frm.doc.total_interest) -
    flt(frm.doc.total_late_fee);
  var unalloc = flt(allocatable - flt(frm.doc.allocated_amount), 2);
  frm.set_value("unallocated_amount", unalloc);
}

function render_accounts_detail(frm, result) {
  // Render matched categories and accounts in the HTML field
  var html = "";

  if (result.categories && result.categories.length) {
    html += '<div style="margin-bottom: 10px;">';
    html +=
      '<div style="font-weight: 600; margin-bottom: 5px; color: var(--text-muted);">Matched Withholding Categories:</div>';
    html += '<table class="table table-bordered table-sm" style="font-size: 12px;">';
    html += "<thead><tr><th>Category</th><th>GL Account</th></tr></thead><tbody>";
    result.categories.forEach(function (cat) {
      html +=
        "<tr><td>" +
        frappe.utils.escape_html(cat.name) +
        "</td><td>" +
        frappe.utils.escape_html(cat.account) +
        "</td></tr>";
    });
    html += "</tbody></table></div>";
  }

  if (result.direct_accounts && result.direct_accounts.length) {
    html += '<div style="margin-bottom: 10px;">';
    html +=
      '<div style="font-weight: 600; margin-bottom: 5px; color: var(--text-muted);">Direct TDS Accounts (from Chart of Accounts):</div>';
    html += "<ul style='margin: 0; padding-left: 20px; font-size: 12px;'>";
    result.direct_accounts.forEach(function (acc) {
      html += "<li>" + frappe.utils.escape_html(acc) + "</li>";
    });
    html += "</ul></div>";
  }

  if (!result.categories.length && !result.direct_accounts.length) {
    html =
      '<div class="text-muted" style="font-size: 12px;">No matching accounts found for this section.</div>';
  }

  frm.fields_dict.tds_accounts_html.$wrapper.html(html);
}

function render_accounts_html(frm) {
  // Re-render from stored JSON on refresh (without making an API call)
  // This is a lightweight version — just show the account list
  if (!frm.doc.tds_account_head) {
    clear_accounts_html(frm);
    return;
  }

  var accounts = [];
  try {
    accounts = JSON.parse(frm.doc.tds_account_head);
  } catch (e) {
    accounts = [frm.doc.tds_account_head];
  }

  if (!accounts.length) {
    clear_accounts_html(frm);
    return;
  }

  var html =
    '<div style="font-weight: 600; margin-bottom: 5px; color: var(--text-muted);">TDS Accounts for section ' +
    frappe.utils.escape_html(frm.doc.tds_section_code || "") +
    ":</div>";
  html += "<ul style='margin: 0; padding-left: 20px; font-size: 12px;'>";
  accounts.forEach(function (acc) {
    html += "<li>" + frappe.utils.escape_html(acc) + "</li>";
  });
  html += "</ul>";

  if (frm.fields_dict.tds_accounts_html) {
    frm.fields_dict.tds_accounts_html.$wrapper.html(html);
  }
}

function clear_accounts_html(frm) {
  if (frm.fields_dict.tds_accounts_html) {
    frm.fields_dict.tds_accounts_html.$wrapper.html("");
  }
}

