from django.contrib import admin

from .forms import IngredientForm, RecipeForm, StepForm
from .models import (
    CartAttempt,
    CartItemMatch,
    CartRun,
    Category,
    ImportJob,
    Recipe,
    RecipeIngredient,
    RecipeStep,
    StorePreference,
)


class AdminRecipeForm(RecipeForm):
    class Meta(RecipeForm.Meta):
        fields = "__all__"


class AdminIngredientForm(IngredientForm):
    class Meta(IngredientForm.Meta):
        fields = "__all__"


class AdminStepForm(StepForm):
    class Meta(StepForm.Meta):
        fields = "__all__"


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name", "slug", "order")
    list_editable = ("order",)


class IngredientInline(admin.TabularInline):
    model = RecipeIngredient
    form = AdminIngredientForm
    extra = 1


class StepInline(admin.StackedInline):
    model = RecipeStep
    form = AdminStepForm
    extra = 1


@admin.register(Recipe)
class RecipeAdmin(admin.ModelAdmin):
    form = AdminRecipeForm
    list_display = ("title", "status", "created_by", "servings", "updated_at")
    list_filter = ("status", "categories")
    search_fields = ("title", "description", "ingredients__name")
    readonly_fields = ("created_at", "updated_at")
    inlines = (IngredientInline, StepInline)


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    list_display = ("source_type", "status", "source_title", "requested_by", "created_at")
    list_filter = ("source_type", "status")
    readonly_fields = ("created_at", "started_at", "finished_at")


@admin.register(StorePreference)
class StorePreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "store", "position", "enabled")
    list_filter = ("store", "enabled")


class CartItemMatchInline(admin.TabularInline):
    model = CartItemMatch
    extra = 0
    readonly_fields = ("ingredient_name", "requested_quantity", "product_name", "quality", "warning")


@admin.register(CartAttempt)
class CartAttemptAdmin(admin.ModelAdmin):
    list_display = ("run", "store", "status", "started_at", "finished_at")
    list_filter = ("store", "status")
    readonly_fields = ("started_at", "finished_at", "result")
    inlines = (CartItemMatchInline,)


@admin.register(CartRun)
class CartRunAdmin(admin.ModelAdmin):
    list_display = ("recipe", "requested_by", "status", "created_at", "finished_at")
    list_filter = ("status",)
    readonly_fields = ("created_at", "started_at", "finished_at", "ingredient_snapshot")
