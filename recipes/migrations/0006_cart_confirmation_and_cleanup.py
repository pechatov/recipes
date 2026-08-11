from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("recipes", "0005_cartattempt_cartitemmatch_cartrun_cartattempt_run_and_more")]

    operations = [
        migrations.AlterField(
            model_name="cartrun",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "В очереди"),
                    ("processing", "Собирается"),
                    ("completed", "Корзина готова"),
                    ("review", "Нужна проверка"),
                    ("confirmed", "Подтверждена"),
                    ("cleanup_pending", "Ожидает очистки"),
                    ("cleaning", "Очищается"),
                    ("cancelled", "Отменена"),
                    ("login_required", "Нужен вход"),
                    ("failed", "Ошибка"),
                ],
                db_index=True,
                default="pending",
                max_length=24,
                verbose_name="статус",
            ),
        ),
        migrations.AddField(
            model_name="cartrun",
            name="confirmation_deadline",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name="cartrun",
            name="confirmed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="cartrun",
            name="cleanup_requested_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="cartrun",
            name="cleaned_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
