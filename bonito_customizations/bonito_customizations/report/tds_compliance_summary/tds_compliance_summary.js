// Copyright (c) 2026, Bonito Designs Pvt Ltd and contributors
// For license information, please see license.txt

frappe.query_reports["TDS Compliance Summary"] = {
  filters: [
    {
      fieldname: "company",
      label: __("Company"),
      fieldtype: "Link",
      options: "Company",
      default: frappe.defaults.get_user_default("Company"),
      reqd: 1,
    },
    {
      fieldname: "financial_year",
      label: __("Financial Year"),
      fieldtype: "Link",
      options: "Fiscal Year",
      default: frappe.sys_defaults.fiscal_year,
      reqd: 1,
    },
    {
      fieldname: "quarter",
      label: __("Quarter"),
      fieldtype: "Select",
      options: "\nQ1 (Apr-Jun)\nQ2 (Jul-Sep)\nQ3 (Oct-Dec)\nQ4 (Jan-Mar)",
    },
    {
      fieldname: "tds_section",
      label: __("TDS Section"),
      fieldtype: "Select",
      options:
        "\n194C\n194J\n194I\n194A\n194Q\n194H\n192B\n194R\n194D\n194DA",
      description: "Filter by specific TDS section (shows all if blank)",
    },
    {
      fieldname: "group_by",
      label: __("Group By"),
      fieldtype: "Select",
      options: "\nMonthly",
      default: "",
    },
  ],
};

