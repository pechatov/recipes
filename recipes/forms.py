from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import UserCreationForm
from django.forms import BaseInlineFormSet, inlineformset_factory

from .models import (
    ImportJob,
    Recipe,
    RecipeIngredient,
    RecipeRefinement,
    RecipeStep,
    is_water_ingredient_name,
)


class SetupForm(UserCreationForm):
    class Meta(UserCreationForm.Meta):
        model = get_user_model()
        fields = ("username",)
        labels = {"username": "Имя пользователя"}


class RecipeForm(forms.ModelForm):
    class Meta:
        model = Recipe
        fields = (
            "title",
            "description",
            "categories",
            "servings",
            "prep_minutes",
            "cook_minutes",
            "calories_per_serving",
            "proteins_per_serving",
            "fats_per_serving",
            "carbohydrates_per_serving",
            "calories_per_100g",
            "proteins_per_100g",
            "fats_per_100g",
            "carbohydrates_per_100g",
            "cover",
        )
        widgets = {
            "description": forms.Textarea(attrs={"rows": 4}),
            "categories": forms.CheckboxSelectMultiple(attrs={"class": "category-checkboxes"}),
            "servings": forms.NumberInput(attrs={"min": 1}),
            "prep_minutes": forms.NumberInput(attrs={"min": 0}),
            "cook_minutes": forms.NumberInput(attrs={"min": 0}),
            "cover": forms.ClearableFileInput(
                attrs={"accept": "image/jpeg,image/png,image/webp", "data-image-input": ""}
            ),
        }

    def save(self, commit=True):
        recipe = super().save(commit=False)
        if "cover" in self.changed_data:
            recipe.cover_imported = False
        if commit:
            recipe.save()
            self._save_m2m()
        return recipe


class ImportRecipeForm(forms.ModelForm):
    class Meta:
        model = ImportJob
        fields = ("source_url", "custom_prompt")
        labels = {
            "source_url": "Ссылка на рецепт или видео",
            "custom_prompt": "Дополнительные пожелания",
        }
        help_texts = {
            "source_url": "Подойдут страница с текстовым рецептом или видео на YouTube.",
        }
        widgets = {
            "source_url": forms.URLInput(
                attrs={"placeholder": "https://…", "autocomplete": "url"}
            ),
            "custom_prompt": forms.Textarea(
                attrs={
                    "rows": 4,
                    "placeholder": "Например: без мяса, сохранить авторские названия частей…",
                }
            ),
        }


class RecipeRefinementForm(forms.ModelForm):
    class Meta:
        model = RecipeRefinement
        fields = ("prompt",)
        labels = {"prompt": "Пожелания к рецепту"}
        widgets = {
            "prompt": forms.Textarea(
                attrs={
                    "rows": 3,
                    "maxlength": 4000,
                    "placeholder": (
                        "Например: сделай рецепт менее острым и замени сливки "
                        "на кокосовое молоко…"
                    ),
                }
            )
        }

    def clean_prompt(self):
        prompt = " ".join(self.cleaned_data["prompt"].split())
        if not prompt:
            raise forms.ValidationError("Напишите, что нужно изменить в рецепте.")
        return prompt[:4000]


class IngredientForm(forms.ModelForm):
    class Meta:
        model = RecipeIngredient
        fields = (
            "section",
            "name",
            "quantity",
            "unit",
            "search_query",
            "optional",
            "is_pantry",
            "estimated",
        )
        widgets = {
            "section": forms.TextInput(attrs={"placeholder": "Для основного блюда"}),
            "name": forms.TextInput(attrs={"placeholder": "Например, сливки"}),
            "quantity": forms.NumberInput(attrs={"step": "0.01", "min": 0}),
            "unit": forms.TextInput(attrs={"placeholder": "г, мл, шт."}),
            "search_query": forms.TextInput(attrs={"placeholder": "сливки 20%"}),
        }

    def clean_name(self):
        name = self.cleaned_data["name"]
        unchanged_existing_water = self.instance.pk and "name" not in self.changed_data
        if is_water_ingredient_name(name) and not unchanged_existing_water:
            raise forms.ValidationError(
                "Воду не нужно добавлять в ингредиенты — укажите её только в шагах."
            )
        return name


class StepForm(forms.ModelForm):
    class Meta:
        model = RecipeStep
        fields = ("section", "title", "instruction", "image")
        widgets = {
            "section": forms.TextInput(attrs={"placeholder": "Например, для соуса"}),
            "title": forms.TextInput(attrs={"placeholder": "Например, приготовить соус"}),
            "instruction": forms.Textarea(attrs={"rows": 5, "placeholder": "Подробно опишите действие"}),
            "image": forms.ClearableFileInput(
                attrs={"accept": "image/jpeg,image/png,image/webp", "data-image-input": ""}
            ),
        }

    def save(self, commit=True):
        step = super().save(commit=False)
        if "image" in self.changed_data:
            step.image_imported = False
        if commit:
            step.save()
            self._save_m2m()
        return step


class OrderedInlineFormSet(BaseInlineFormSet):
    def save(self, commit=True):
        instances = super().save(commit=commit)
        if commit:
            active_instances = [
                form.instance
                for form in self.forms
                if form.cleaned_data
                and not form.cleaned_data.get("DELETE", False)
                and form.instance.pk
            ]
            for index, instance in enumerate(active_instances):
                instance.order = index
            self.model.objects.bulk_update(active_instances, ["order"])
        return instances


IngredientFormSet = inlineformset_factory(
    Recipe,
    RecipeIngredient,
    form=IngredientForm,
    formset=OrderedInlineFormSet,
    extra=1,
    can_delete=True,
    min_num=1,
    validate_min=True,
)

StepFormSet = inlineformset_factory(
    Recipe,
    RecipeStep,
    form=StepForm,
    formset=OrderedInlineFormSet,
    extra=1,
    can_delete=True,
    min_num=1,
    validate_min=True,
)
