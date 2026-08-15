# apps/account/web/urls.py

from django.urls import path
from . import views

app_name = 'account'

urlpatterns = [
    path('users/', views.user_manage, name='user_manage'),
    path('users/<int:pk>/edit/', views.user_edit, name='user_edit'),
]
