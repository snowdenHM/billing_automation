"""Which bill-sync endpoints actually consult the approval gate.

``Organization.require_bill_approval`` promises that a verified bill
"must be approved by a Manager or Admin before it can be posted to Tally
or Zoho". Both modules now keep that promise: the five Tally sync views
and all six Zoho ones consult :func:`gate_sync` before posting.

The Zoho case used to be marked ``expectedFailure`` -- the three Zoho
sync views that existed never called the gate, so with the flag on a
Zoho bill posted to the client's books with nobody having approved it.
That gap is closed, the marker is off, and this is now an ordinary
assertion that fails if anybody reopens it.

Source inspection rather than a live request on purpose: driving a Zoho
sync needs OAuth credentials, and all this needs to know is whether the
gate is consulted at all.
"""

import ast
import inspect

from django.test import SimpleTestCase


def _function_node(module, func_name):
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return node
    raise AssertionError(f"{module.__name__} has no function {func_name}")


def _calls_the_gate(module, func_name, delegates_to=None):
    """Does *func_name* consult the gate, directly or via its helper?

    Read from the module's syntax tree rather than from the imported
    object: by the time ``@extend_schema`` and ``@api_view`` are done
    with it, the name points at a wrapper whose source says nothing
    about the view that was written.

    ``delegates_to`` is a ``(module, function)`` pair for a view that is
    a thin wrapper over a shared body. Several Zoho voucher types share
    one sync implementation, and the gate belongs in that shared body --
    putting it there is what stops a type added later from skipping it.
    The view must still actually call the helper, which is checked.
    """
    node = _function_node(module, func_name)
    if "gate_sync" in ast.dump(node):
        return True
    if delegates_to is None:
        return False
    helper_module, helper_name = delegates_to
    if helper_name not in ast.dump(node):
        raise AssertionError(
            f"{module.__name__}.{func_name} is declared to delegate to "
            f"{helper_name}, but its body never calls it"
        )
    return "gate_sync" in ast.dump(_function_node(helper_module, helper_name))


class TallySyncGateCoverageTests(SimpleTestCase):

    def test_every_tally_bill_sync_consults_the_gate(self):
        from apps.module.tally.views import (
            expense_bills, payment_bills, receipt_bills, sales_bills,
            vendor_bills,
        )

        views = {
            "vendor": (vendor_bills, "vendor_bill_sync"),
            "expense": (expense_bills, "expense_bill_sync"),
            "payment": (payment_bills, "payment_bill_sync"),
            "receipt": (receipt_bills, "receipt_bill_sync"),
            "sales": (sales_bills, "sales_bill_sync"),
        }
        missing = [name for name, (mod, fn) in views.items()
                   if not _calls_the_gate(mod, fn)]
        self.assertEqual(missing, [], f"Tally sync views with no approval gate: {missing}")


class ZohoSyncGateCoverageTests(SimpleTestCase):

    def test_every_zoho_bill_sync_consults_the_gate(self):
        from apps.module.zoho.views import (
            expense_bills, journal_bills, payment_bills, payment_voucher_helpers,
            receipt_bills, sales_bills, vendor_bills,
        )

        shared_payment_sync = (payment_voucher_helpers, "zoho_payment_sync_base")
        views = {
            "vendor": (vendor_bills, "vendor_bill_sync_view", None),
            "expense": (expense_bills, "expense_bill_sync_view", None),
            "journal": (journal_bills, "journal_bill_sync_view", None),
            "sales": (sales_bills, "sales_bill_sync_view", None),
            "receipt": (receipt_bills, "receipt_bill_sync_view", shared_payment_sync),
            "payment": (payment_bills, "payment_bill_sync_view", shared_payment_sync),
        }
        missing = [name for name, (mod, fn, via) in views.items()
                   if not _calls_the_gate(mod, fn, via)]
        self.assertEqual(missing, [], f"Zoho sync views with no approval gate: {missing}")


class ApprovalBillModelCoverageTests(SimpleTestCase):
    """A gate nobody can satisfy is worse than no gate.

    ``gate_sync`` blocks a bill that has no approval row, and the only
    way to create one is ``find_bill`` -- so a bill model missing from
    its resolver becomes unpostable the moment the flag is turned on.
    The two lists must stay in step.
    """

    def test_every_syncable_bill_model_can_be_submitted_for_approval(self):
        from apps.common.services.activity import resolve_bill_kind
        from apps.common.views_approval import _bill_models

        unresolvable = [m.__name__ for m in _bill_models()
                        if resolve_bill_kind(m()) == (None, None)]
        self.assertEqual(
            unresolvable, [],
            f"Models find_bill can return but resolve_bill_kind cannot name: {unresolvable}",
        )

    def test_both_modules_bill_models_are_registered(self):
        from apps.common.views_approval import _bill_models

        names = {m.__name__ for m in _bill_models()}
        expected = {
            "TallyVendorBill", "TallyExpenseBill", "TallyPaymentBill",
            "TallySalesBill", "TallyReceiptBill",
            "VendorBill", "ExpenseBill", "JournalBill",
            "SalesBill", "ReceiptBill", "PaymentBill",
        }
        self.assertEqual(names, expected)
