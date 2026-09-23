from django.urls import path
from . import finance_views as views

urlpatterns = [
    path("order-plans/<int:plan_id>/finance/", views.finance_detail, name="finance_detail"),
    path("order-plans/<int:plan_id>/finance/statement/", views.finance_statement, name="finance_statement"),
    path("order-plans/<int:plan_id>/finance/statement/file/", views.finance_document, name="finance_statement_file"),
    path("order-plans/<int:plan_id>/finance/payments/", views.finance_payment_upload, name="finance_payment_upload"),
    path("order-plans/<int:plan_id>/finance/payments/<int:payment_id>/confirm/", views.finance_payment_confirm, name="finance_payment_confirm"),
    path("order-plans/<int:plan_id>/finance/payments/<int:payment_id>/file/", views.finance_document, name="finance_payment_file"),
]
