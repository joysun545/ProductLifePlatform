from django.urls import path
from . import finance_views as views

urlpatterns = [
    path("factory-payments/orders/<int:plan_id>/quote/", views.factory_order_quote),
    path("factory-payments/orders/<int:plan_id>/confirm/", views.factory_order_price_confirm),
    path("factory-payments/", views.factory_payment_accounts),
    path("factory-payments/factory/<int:factory_id>/", views.factory_payment_submit),
    path("factory-payments/<int:payment_id>/confirm/", views.factory_payment_confirm),
    path("factory-payments/<int:payment_id>/file/", views.factory_payment_file),
    path("order-plans/<int:plan_id>/finance/", views.finance_detail, name="finance_detail"),
    path("order-plans/<int:plan_id>/finance/statement/", views.finance_statement, name="finance_statement"),
    path("order-plans/<int:plan_id>/finance/statement/file/", views.finance_document, name="finance_statement_file"),
    path("order-plans/<int:plan_id>/finance/payments/", views.finance_payment_upload, name="finance_payment_upload"),
    path("order-plans/<int:plan_id>/finance/payments/<int:payment_id>/confirm/", views.finance_payment_confirm, name="finance_payment_confirm"),
    path("order-plans/<int:plan_id>/finance/payments/<int:payment_id>/file/", views.finance_document, name="finance_payment_file"),
]
