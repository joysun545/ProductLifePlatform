from django.urls import path
from . import views
from . import merchant_accounts
urlpatterns=[
 path("merchant-accounts/",merchant_accounts.workbench,name="merchant_accounts"),
 path('',views.workbench,name='workbench'),
 path('api/start/',views.start,name='start'),
 path('api/approve/',views.approve,name='approve'),
 path('api/status/',views.status,name='status'),
 path('api/session/',views.session_info,name='session'),
 path('api/cancel/',views.cancel,name='cancel'),
 path('api/logout/',views.logout,name='logout'),
]
