from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("recipes", "0017_cartrun_browser_operation_started_at"),
    ]

    operations = [
        migrations.AlterField(
            model_name="browserloginsession",
            name="status",
            field=models.CharField(
                choices=[
                    ("starting", "Запускается"),
                    ("active", "Открыта"),
                    ("stopping", "Закрывается"),
                    ("completing", "Сохраняется"),
                    ("completed", "Сохранена"),
                    ("expired", "Истекла"),
                    ("failed", "Ошибка"),
                ],
                db_index=True,
                default="starting",
                max_length=16,
            ),
        ),
    ]
