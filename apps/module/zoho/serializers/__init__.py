from .base import EmptySerializer, SyncResultSerializer, GenerateTokenResponseSerializer
from .settings import (
    ZohoCredentialsSerializer,
    ZohoVendorSerializer,
    ZohoCustomerSerializer,
    ZohoChartOfAccountSerializer,
    ZohoTaxesSerializer,
    ZohoTdsTcsSerializer,
    ZohoVendorCreditsSerializer,
)

from .sales_bills import (
    SalesZohoProductSerializer,
    SalesZohoConsolidatedProductSerializer,
    SalesZohoBillSerializer,
    ZohoSalesBillSerializer,
    ZohoSalesBillDetailSerializer,
    ZohoSalesBillUploadSerializer,
    ZohoSalesBillMultipleUploadSerializer,
)
from .payment_bills import (
    ReceiptZohoBillSerializer,
    ZohoReceiptBillSerializer,
    ZohoReceiptBillDetailSerializer,
    ZohoReceiptBillUploadSerializer,
    ZohoReceiptBillMultipleUploadSerializer,
    PaymentZohoBillSerializer,
    ZohoPaymentBillSerializer,
    ZohoPaymentBillDetailSerializer,
    ZohoPaymentBillUploadSerializer,
    ZohoPaymentBillMultipleUploadSerializer,
)

__all__ = [
    "EmptySerializer",
    "SyncResultSerializer",
    "GenerateTokenResponseSerializer",
    "ZohoCredentialsSerializer",
    "ZohoVendorSerializer",
    "ZohoCustomerSerializer",
    "ZohoChartOfAccountSerializer",
    "ZohoTaxesSerializer",
    "ZohoTdsTcsSerializer",
    "ZohoVendorCreditsSerializer",
    # Sales invoices
    "SalesZohoProductSerializer",
    "SalesZohoConsolidatedProductSerializer",
    "SalesZohoBillSerializer",
    "ZohoSalesBillSerializer",
    "ZohoSalesBillDetailSerializer",
    "ZohoSalesBillUploadSerializer",
    "ZohoSalesBillMultipleUploadSerializer",
    # Receipt / payment vouchers
    "ReceiptZohoBillSerializer",
    "ZohoReceiptBillSerializer",
    "ZohoReceiptBillDetailSerializer",
    "ZohoReceiptBillUploadSerializer",
    "ZohoReceiptBillMultipleUploadSerializer",
    "PaymentZohoBillSerializer",
    "ZohoPaymentBillSerializer",
    "ZohoPaymentBillDetailSerializer",
    "ZohoPaymentBillUploadSerializer",
    "ZohoPaymentBillMultipleUploadSerializer",
]
