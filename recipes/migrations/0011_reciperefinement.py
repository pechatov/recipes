from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("recipes", "0010_recipe_macros_and_ascii_slugs"),
    ]

    operations = [
        migrations.CreateModel(
            name="RecipeRefinement",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("prompt", models.TextField(verbose_name="пожелание")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "В очереди"),
                            ("processing", "Обрабатывается"),
                            ("completed", "Готово"),
                            ("failed", "Ошибка"),
                        ],
                        db_index=True,
                        default="pending",
                        max_length=16,
                        verbose_name="статус",
                    ),
                ),
                (
                    "expected_recipe_updated_at",
                    models.DateTimeField(verbose_name="версия рецепта перед обработкой"),
                ),
                ("error", models.TextField(blank=True, verbose_name="ошибка")),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                (
                    "recipe",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="refinements",
                        to="recipes.recipe",
                        verbose_name="рецепт",
                    ),
                ),
                (
                    "requested_by",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="recipe_refinements",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "пожелание к рецепту",
                "verbose_name_plural": "пожелания к рецептам",
                "ordering": ["created_at", "pk"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(status__in=["pending", "processing"]),
                        fields=("recipe",),
                        name="one_active_refinement_per_recipe",
                    )
                ],
            },
        )
    ]
