from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("recipes", "0010_recipe_macros_and_ascii_slugs"),
    ]

    operations = [
        migrations.AddField(
            model_name="importjob",
            name="source_title_checked_at",
            field=models.DateTimeField(
                blank=True,
                db_index=True,
                null=True,
                verbose_name="последняя проверка названия источника",
            ),
        ),
    ]
