# -*- coding: utf-8 -*-
"""
Ensures the 'Bonito Customizations' Module Def exists.

This is required because Frappe maps doctypes to modules, and the module
must exist as a Module Def document before bench migrate can create doctypes
that reference it.

If your app already has a Module Def (check: bench --site <site> console,
then frappe.get_doc("Module Def", "Bonito Customizations")), you can skip this.

Otherwise, add this to your app's after_install hook or run it once manually:
    bench --site <site> execute bonito_customizations.patches.setup_module.execute
"""

from __future__ import unicode_literals
import frappe


def execute():
    module_name = "Bonito Customizations"

    if not frappe.db.exists("Module Def", module_name):
        module_def = frappe.new_doc("Module Def")
        module_def.module_name = module_name
        module_def.app_name = "bonito_customizations"
        module_def.insert(ignore_permissions=True)
        frappe.db.commit()
        print("✓ Module Def '{0}' created".format(module_name))
    else:
        print("✓ Module Def '{0}' already exists".format(module_name))
