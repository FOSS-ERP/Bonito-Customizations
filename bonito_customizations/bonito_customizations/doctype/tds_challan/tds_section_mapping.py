# -*- coding: utf-8 -*-
# Copyright (c) 2026, Bonito Designs Pvt Ltd and contributors
# For license information, please see license.txt

"""
TDS Section Mapping — Central registry for mapping government TDS sections
to ERPNext Tax Withholding Categories and GL accounts.

The government collects TDS per section (e.g. 194C), not per rate.
But ERPNext deductions happen at specific rates via Tax Withholding Categories
(e.g. "TDS-194C-2%", "TDS-194C-1%"). When creating a challan for a section,
we need to find ALL categories and GL accounts under that section.

This module provides:
1. SECTION_PATTERNS: regex patterns to match category names to sections
2. resolve_section_accounts(): given a section + company, return all
   matching GL accounts (from both Tax Withholding Categories and
   direct TDS accounts in the Chart of Accounts)
3. extract_section_code(): parse the section code from the Select field value

Design for extensibility:
- New withholding categories added to ERPNext are auto-discovered via
  regex matching against the category name, so "TDS-194C-3%" would
  automatically be included under 194C without code changes.
- Direct TDS GL accounts are matched by a keyword map (ACCOUNT_KEYWORDS),
  so "TDS on Contractors Payable - BDPL" matches 194C.
- The 194 (bare) categories are clubbed with 194A (Dividends) per
  business requirement.
"""

from __future__ import unicode_literals

import re

import frappe
from frappe import _
from frappe.utils import cstr


# ---------------------------------------------------------------------------
# Section code extraction from the Select field value
# ---------------------------------------------------------------------------
# The tds_section Select field stores values like "194C - Contractors".
# We extract the code portion (e.g. "194C").

def extract_section_code(tds_section_value):
    """Extract the section code (e.g. '194C') from a Select value like
    '194C - Contractors'."""
    if not tds_section_value:
        return ""
    parts = cstr(tds_section_value).split(" - ", 1)
    return parts[0].strip()


# ---------------------------------------------------------------------------
# Regex patterns for matching Tax Withholding Category names to sections
# ---------------------------------------------------------------------------
# These patterns are applied to the Tax Withholding Category `name` field.
# They are intentionally broad to catch variations like:
#   "TDS-194C-2%", "194C- 0.1%", "Tax- 194C-1%-Contractor",
#   "TDS - 194C - 0.25%", "TDS 194 C .01%"
#
# The order matters: more specific patterns (194DA, 194C) must come before
# less specific ones (194, which is a fallback for dividends).

SECTION_PATTERNS = [
    # 194DA must come before 194D
    ("194DA", re.compile(r"194\s*DA", re.IGNORECASE)),
    # 194C, 194H, 194I, 194J, 194Q, 194R, 194D (without A suffix)
    ("194C", re.compile(r"194\s*C(?!\w)", re.IGNORECASE)),
    ("194H", re.compile(r"194\s*H(?!\w)", re.IGNORECASE)),
    ("194I", re.compile(r"194\s*I(?!\w)", re.IGNORECASE)),
    ("194J", re.compile(r"194\s*J(?!\w)", re.IGNORECASE)),
    ("194Q", re.compile(r"194\s*Q(?!\w)", re.IGNORECASE)),
    ("194R", re.compile(r"194\s*R(?!\w)", re.IGNORECASE)),
    ("194D", re.compile(r"194\s*D(?!\w)", re.IGNORECASE)),
    ("194A", re.compile(r"194\s*A(?!\w)", re.IGNORECASE)),
    ("192B", re.compile(r"192\s*B(?!\w)", re.IGNORECASE)),
    # Bare "194" (no letter suffix) → clubbed with 194A (Dividends)
    # This pattern matches "TDS - 194 - Dividends" but NOT "194C", "194J" etc.
    # The negative lookahead ensures we don't match section codes already handled above.
    ("194A", re.compile(r"(?<!\d)194(?!\s*[A-Za-z])(?!\d)", re.IGNORECASE)),
]


def classify_category(category_name):
    """Given a Tax Withholding Category name, return the section code it
    belongs to, or None if no match."""
    name = cstr(category_name).strip()
    for section_code, pattern in SECTION_PATTERNS:
        if pattern.search(name):
            return section_code
    return None


# ---------------------------------------------------------------------------
# Direct TDS GL account keyword mapping
# ---------------------------------------------------------------------------
# Some TDS accounts in the Chart of Accounts receive TDS credits directly
# (not via Tax Withholding Categories on invoices). These are typically
# booked via Journal Entries. We map them to sections by keywords in the
# account name.
#
# This is separate from the Tax Withholding Account child table mapping.

ACCOUNT_KEYWORDS = {
    "194C": ["contractor"],
    "194J": ["professional", "technical", "director"],
    "194I": ["rent"],
    "194A": ["interest", "dividend"],
    "194Q": ["purchase", "194q"],
    "194H": ["commission", "brokerage"],
    "192B": ["salary"],
    "194R": ["business promotion", "perquisite", "benefit"],
    "194D": ["insurance commission"],
    "194DA": ["life insurance"],
}

# Special accounts that don't map to a specific section but should be
# available as additional sources. The parent TDS account is the root
# under which section-specific accounts sit.
PARENT_TDS_ACCOUNTS = [
    "Tax Deduction at Source (TDS)",
]

# Accounts that are expense/liability related to TDS compliance but NOT
# TDS deduction accounts. Exclude these from voucher fetching.
EXCLUDED_ACCOUNT_KEYWORDS = [
    "interest on delay",
    "late filing",
    "receivable",
    "advance tax",
    "foreign payment",
]


def classify_account(account_name):
    """Given a GL account name, return the section code it likely belongs to,
    or None."""
    name = cstr(account_name).lower()

    # First check exclusions
    for excl in EXCLUDED_ACCOUNT_KEYWORDS:
        if excl in name:
            return None

    # Then check section keywords
    for section_code, keywords in ACCOUNT_KEYWORDS.items():
        for kw in keywords:
            if kw in name:
                return section_code

    return None


# ---------------------------------------------------------------------------
# Main resolver: get all accounts for a section + company
# ---------------------------------------------------------------------------

@frappe.whitelist()
def resolve_section_accounts(tds_section, company):
    """Given a TDS section (Select value like '194C - Contractors') and company,
    return all matching GL accounts from two sources:

    1. Tax Withholding Categories whose names match the section pattern
       → their linked accounts from Tax Withholding Account child table

    2. Direct TDS GL accounts in the Chart of Accounts whose names match
       the section's keyword patterns

    Returns:
    {
        "section_code": "194C",
        "accounts": ["TDS on Contractors Payable - BDPL", ...],
        "categories": [
            {"name": "TDS-194C-2%", "account": "TDS on Contractors Payable - BDPL"},
            ...
        ],
        "direct_accounts": ["TDS on Contractors Payable - BDPL", ...]
    }
    """
    if not tds_section or not company:
        return {"section_code": "", "accounts": [], "categories": [],
                "direct_accounts": []}

    section_code = extract_section_code(tds_section)
    if not section_code:
        return {"section_code": "", "accounts": [], "categories": [],
                "direct_accounts": []}

    accounts_set = set()
    categories = []
    direct_accounts = []

    # -----------------------------------------------------------------------
    # Source 1: Tax Withholding Categories matching this section
    # -----------------------------------------------------------------------
    all_categories = frappe.db.get_all(
        "Tax Withholding Category",
        fields=["name"],
        filters={},  # no filter — we match by regex in Python
    )

    matching_category_names = []
    for cat in all_categories:
        cat_section = classify_category(cat.name)
        if cat_section == section_code:
            matching_category_names.append(cat.name)

    # Get the GL accounts linked to these categories for this company
    if matching_category_names:
        linked_accounts = frappe.db.sql(
            """
            SELECT twa.parent AS category, twa.account
            FROM `tabTax Withholding Account` twa
            WHERE twa.parent IN %(categories)s
              AND twa.company = %(company)s
        """,
            {"categories": matching_category_names, "company": company},
            as_dict=True,
        )

        for row in linked_accounts:
            categories.append({
                "name": row.category,
                "account": row.account,
            })
            accounts_set.add(row.account)

    # -----------------------------------------------------------------------
    # Source 2: Direct TDS GL accounts from Chart of Accounts
    # -----------------------------------------------------------------------
    # Get all liability accounts with "TDS" in the name for this company
    tds_gl_accounts = frappe.db.get_all(
        "Account",
        filters={
            "company": company,
            "root_type": "Liability",
            "is_group": 0,
            "name": ("like", "%TDS%"),
        },
        fields=["name", "account_name"],
    )

    for acc in tds_gl_accounts:
        acc_section = classify_account(acc.account_name)
        if acc_section == section_code:
            direct_accounts.append(acc.name)
            accounts_set.add(acc.name)

    # Also check for the parent "Tax Deduction at Source (TDS)" account
    # if the section is generic — but typically we don't include it
    # because it's a group account or catch-all

    return {
        "section_code": section_code,
        "accounts": sorted(accounts_set),
        "categories": categories,
        "direct_accounts": sorted(direct_accounts),
    }


@frappe.whitelist()
def get_all_sections_for_company(company):
    """Return a summary of all TDS sections and their matched accounts/categories
    for the given company. Useful for debugging and the Compliance Summary report."""
    if not company:
        return []

    # Get all TDS sections from the Select options
    sections = [
        "194C - Contractors",
        "194J - Professional/Technical Fees",
        "194I - Rent",
        "194A - Interest/Dividends",
        "194Q - Purchase of Goods",
        "194H - Commission/Brokerage",
        "192B - Salary",
        "194R - Benefits/Perquisites",
        "194D - Insurance Commission",
        "194DA - Life Insurance Policy",
    ]

    result = []
    for section in sections:
        data = resolve_section_accounts(section, company)
        if data["accounts"]:
            result.append(data)

    return result

