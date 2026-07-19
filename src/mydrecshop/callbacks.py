from aiogram.filters.callback_data import CallbackData


class NavigationCallback(CallbackData, prefix="nav"):
    page: str


class ProductCallback(CallbackData, prefix="prd"):
    product_id: int


class BuyCallback(CallbackData, prefix="buy"):
    product_id: int


class CheckoutCallback(CallbackData, prefix="pay"):
    product_id: int
    quantity: int = 1


class PurchaseCheckoutCallback(CallbackData, prefix="pchk"):
    product_id: int
    quantity: int
    unit_price_usdt_micros: int


class TermsCallback(CallbackData, prefix="trm"):
    product_id: int
    quantity: int = 1


class QuantityCallback(CallbackData, prefix="qty"):
    product_id: int
    quantity: int


class CustomQuantityCallback(CallbackData, prefix="qcustom"):
    product_id: int


class LanguageCallback(CallbackData, prefix="lng"):
    locale: str


class OrderCallback(CallbackData, prefix="ord"):
    order_id: int


class CancelOrderCallback(CallbackData, prefix="cnl"):
    order_id: int


class SubmitBinanceCallback(CallbackData, prefix="bpay"):
    order_id: int


class OpenBinanceCallback(CallbackData, prefix="bopen"):
    order_id: int


class ConfirmBinanceCallback(CallbackData, prefix="bcfm"):
    order_id: int


class RejectBinanceCallback(CallbackData, prefix="brjt"):
    order_id: int


class DeliverAccountCallback(CallbackData, prefix="bdlv"):
    order_id: int


class AdminPaymentReviewCallback(CallbackData, prefix="aprv"):
    action: str
    order_id: int


class AdminActionCallback(CallbackData, prefix="adm"):
    action: str


class SubscriptionCallback(CallbackData, prefix="sub"):
    action: str


class AdminProductCallback(CallbackData, prefix="aprd"):
    action: str
    product_id: int
