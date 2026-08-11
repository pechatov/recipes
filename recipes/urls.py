from django.urls import path

from . import views

urlpatterns = [
    path("", views.recipe_list, name="recipe-list"),
    path("healthz/", views.health, name="health"),
    path("setup/", views.setup_owner, name="setup-owner"),
    path("drafts/", views.draft_list, name="draft-list"),
    path("imports/new/", views.import_create, name="import-create"),
    path("imports/<int:pk>/", views.import_detail, name="import-detail"),
    path("imports/<int:pk>/retry/", views.import_retry, name="import-retry"),
    path("imports/<int:pk>/reprocess/", views.import_reprocess, name="import-reprocess"),
    path("shopping/stores/", views.store_preferences, name="store-preferences"),
    path("shopping/carts/<int:pk>/", views.cart_detail, name="cart-detail"),
    path("shopping/carts/<int:pk>/continue/", views.cart_continue, name="cart-continue"),
    path("shopping/carts/<int:pk>/retry/", views.cart_retry, name="cart-retry"),
    path("recipes/new/", views.recipe_create, name="recipe-create"),
    path("recipes/<str:slug>/", views.recipe_detail, name="recipe-detail"),
    path("recipes/<str:slug>/edit/", views.recipe_update, name="recipe-update"),
    path("recipes/<str:slug>/publish/", views.recipe_publish, name="recipe-publish"),
    path("recipes/<str:slug>/delete/", views.recipe_delete, name="recipe-delete"),
    path("recipes/<str:slug>/shopping/", views.shopping_list, name="shopping-list"),
    path("recipes/<str:slug>/shopping/start/", views.cart_start, name="cart-start"),
    path("media/<path:path>", views.media_file, name="media-file"),
]
