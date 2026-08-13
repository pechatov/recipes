from django.urls import path

from . import views

urlpatterns = [
    path("", views.recipe_list, name="recipe-list"),
    path("healthz/", views.health, name="health"),
    path("setup/", views.setup_owner, name="setup-owner"),
    path("drafts/", views.draft_list, name="draft-list"),
    path("tasks/", views.task_list, name="task-list"),
    path("imports/new/", views.import_create, name="import-create"),
    path("imports/<int:pk>/", views.import_detail, name="import-detail"),
    path("imports/<int:pk>/retry/", views.import_retry, name="import-retry"),
    path("imports/<int:pk>/reprocess/", views.import_reprocess, name="import-reprocess"),
    path("shopping/stores/", views.store_preferences, name="store-preferences"),
    path("shopping/browser-login/start/", views.browser_login_start, name="browser-login-start"),
    path("shopping/browser-login/<int:pk>/", views.browser_login, name="browser-login"),
    path("shopping/browser-login/<int:pk>/complete/", views.browser_login_complete, name="browser-login-complete"),
    path("shopping/carts/<int:pk>/", views.cart_detail, name="cart-detail"),
    path("shopping/carts/<int:pk>/browser-login/start/", views.browser_login_start, name="cart-browser-login-start"),
    path("shopping/carts/<int:pk>/continue/", views.cart_continue, name="cart-continue"),
    path("shopping/carts/<int:pk>/retry/", views.cart_retry, name="cart-retry"),
    path("shopping/carts/<int:pk>/confirm/", views.cart_confirm, name="cart-confirm"),
    path("shopping/carts/<int:pk>/cancel/", views.cart_cancel, name="cart-cancel"),
    path(
        "shopping/carts/<int:pk>/manual-resolved/",
        views.cart_manual_resolved,
        name="cart-manual-resolved",
    ),
    path("recipes/new/", views.recipe_create, name="recipe-create"),
    path("recipes/<str:slug>/", views.recipe_detail, name="recipe-detail"),
    path("recipes/<str:slug>/edit/", views.recipe_update, name="recipe-update"),
    path("recipes/<str:slug>/refine/", views.recipe_refine, name="recipe-refine"),
    path(
        "recipes/<str:slug>/refinements/<int:pk>/status/",
        views.recipe_refinement_status,
        name="recipe-refinement-status",
    ),
    path("recipes/<str:slug>/publish/", views.recipe_publish, name="recipe-publish"),
    path("recipes/<str:slug>/delete/", views.recipe_delete, name="recipe-delete"),
    path("recipes/<str:slug>/shopping/", views.shopping_list, name="shopping-list"),
    path("recipes/<str:slug>/shopping/start/", views.cart_start, name="cart-start"),
    path("media/<path:path>", views.media_file, name="media-file"),
]
