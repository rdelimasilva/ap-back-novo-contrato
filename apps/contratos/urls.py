from django.urls import path
from . import views

urlpatterns = [
    path("health", views.health),
    path("webhooks/contrato/<str:financiador_id>", views.webhook_contrato),
]
