__version__ = "0.0.1"

from india_compliance.gst_india.utils import original_e_invoice
from india_compliance.gst_india.utils import original_e_waybill



# Apply custom overrides
from bonito_customizations.e_invoice_override import apply_e_invoice_override
from bonito_customizations.e_waybill_override import apply_e_waybill_override

original_e_invoice.e_invoice = apply_e_invoice_override
original_e_waybill = apply_e_invoice_override