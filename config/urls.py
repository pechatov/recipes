from django.contrib import admin
from django.contrib.auth.views import LogoutView
from django.urls import include, path

from recipes.views import login_view, register_invite, registration_access

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", login_view, name="login"),
    path("logout/", LogoutView.as_view(), name="logout"),
    path("access/", registration_access, name="registration-access"),
    path("register/<str:token>/", register_invite, name="register-invite"),
    path("", include("recipes.urls")),
]
