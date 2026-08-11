from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import include, path

from recipes.views import login_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", login_view, name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("", include("recipes.urls")),
]
