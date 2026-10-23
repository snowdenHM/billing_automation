from .ledger_serializers import LedgerSerializer, ParentLedgerSerializer, LedgerBulkCreateSerializer, StockItemSerializer, StockItemBulkCreateSerializer
from .config_serializers import TallyConfigSerializer, GstRateLedgerMappingSerializer
from .setup_guide_serializers import TallySetupStepSerializer
from .bill_base import (
    SafeDecimalField,
    BaseTallyBillSerializer,
    BaseBillUploadSerializer,
    BillAnalysisRequestSerializer,
    BillSyncRequestSerializer,
    BaseBillDetailSerializer,
)
from .vendor_serializers import (
    TallyVendorBillSerializer,
    TallyVendorBillDetailSerializer,
    TallyVendorAnalyzedBillSerializer,
    TallyVendorAnalyzedProductSerializer,
    VendorBillUploadSerializer,
    BillVerificationSerializer,
    BillSyncResponseSerializer
)
from .expense_serializers import (
    TallyExpenseBillSerializer,
    TallyExpenseBillDetailSerializer,
    TallyExpenseAnalyzedBillSerializer,
    TallyExpenseAnalyzedProductSerializer,
    ExpenseBillUploadSerializer,
    ExpenseBillAnalysisRequestSerializer,
    ExpenseBillVerificationSerializer,
    ExpenseBillSyncResponseSerializer
)
from .sales_serializers import (
    TallySalesBillSerializer,
    TallySalesBillDetailSerializer,
    TallySalesAnalyzedBillSerializer,
    TallySalesAnalyzedProductSerializer,
    SalesBillUploadSerializer,
    SalesBillVerificationSerializer,
    SalesBillSyncResponseSerializer,
)
from .payment_serializers import (
    TallyPaymentBillSerializer,
    TallyPaymentBillDetailSerializer,
    TallyPaymentAnalyzedBillSerializer,
    TallyPaymentAnalyzedProductSerializer,
    PaymentBillUploadSerializer,
    PaymentBillAnalysisRequestSerializer,
    PaymentBillVerificationSerializer,
    PaymentBillSyncResponseSerializer,
)

from .receipt_serializers import (
    TallyReceiptBillSerializer,
    TallyReceiptBillDetailSerializer,
    TallyReceiptAnalyzedBillSerializer,
    TallyReceiptAnalyzedProductSerializer,
    ReceiptBillUploadSerializer,
    ReceiptBillAnalysisRequestSerializer,
    ReceiptBillVerificationSerializer,
    ReceiptBillSyncResponseSerializer,
)

# Backward compatibility - alias for expense sync request
ExpenseBillSyncRequestSerializer = BillSyncRequestSerializer
PaymentBillSyncRequestSerializer = BillSyncRequestSerializer
SalesBillSyncRequestSerializer = BillSyncRequestSerializer
ReceiptBillSyncRequestSerializer = BillSyncRequestSerializer

__all__ = [
    # Base classes
    'SafeDecimalField',
    'BaseTallyBillSerializer',
    'BaseBillUploadSerializer',
    'BaseBillDetailSerializer',
    # Ledger/Config
    'LedgerSerializer',
    'ParentLedgerSerializer',
    'LedgerBulkCreateSerializer',
    'StockItemSerializer',
    'StockItemBulkCreateSerializer',
    'TallyConfigSerializer',
    'GstRateLedgerMappingSerializer',
    'TallySetupStepSerializer',
    # Vendor
    'TallyVendorBillSerializer',
    'TallyVendorBillDetailSerializer',
    'TallyVendorAnalyzedBillSerializer',
    'TallyVendorAnalyzedProductSerializer',
    'VendorBillUploadSerializer',
    'BillAnalysisRequestSerializer',
    'BillVerificationSerializer',
    'BillSyncRequestSerializer',
    'BillSyncResponseSerializer',
    # Expense
    'TallyExpenseBillSerializer',
    'TallyExpenseBillDetailSerializer',
    'TallyExpenseAnalyzedBillSerializer',
    'TallyExpenseAnalyzedProductSerializer',
    'ExpenseBillUploadSerializer',
    'ExpenseBillAnalysisRequestSerializer',
    'ExpenseBillVerificationSerializer',
    'ExpenseBillSyncRequestSerializer',
    'ExpenseBillSyncResponseSerializer',
    # Payment Voucher
    'TallyPaymentBillSerializer',
    'TallyPaymentBillDetailSerializer',
    'TallyPaymentAnalyzedBillSerializer',
    'TallyPaymentAnalyzedProductSerializer',
    'PaymentBillUploadSerializer',
    'PaymentBillAnalysisRequestSerializer',
    'PaymentBillVerificationSerializer',
    'PaymentBillSyncRequestSerializer',
    'PaymentBillSyncResponseSerializer',
    # Sales Voucher
    'TallySalesBillSerializer',
    'TallySalesBillDetailSerializer',
    'TallySalesAnalyzedBillSerializer',
    'TallySalesAnalyzedProductSerializer',
    'SalesBillUploadSerializer',
    'SalesBillVerificationSerializer',
    'SalesBillSyncRequestSerializer',
    'SalesBillSyncResponseSerializer',
    # Receipt Voucher
    'TallyReceiptBillSerializer',
    'TallyReceiptBillDetailSerializer',
    'TallyReceiptAnalyzedBillSerializer',
    'TallyReceiptAnalyzedProductSerializer',
    'ReceiptBillUploadSerializer',
    'ReceiptBillAnalysisRequestSerializer',
    'ReceiptBillVerificationSerializer',
    'ReceiptBillSyncRequestSerializer',
    'ReceiptBillSyncResponseSerializer',
]
