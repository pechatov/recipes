from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("recipes", "0006_cart_confirmation_and_cleanup")]

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
                    ("manual_check", "Нужна ручная проверка"),
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
    ]
