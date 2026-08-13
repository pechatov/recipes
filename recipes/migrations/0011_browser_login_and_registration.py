import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def create_application_locks(apps, schema_editor):
    ApplicationLock = apps.get_model("recipes", "ApplicationLock")
    ApplicationLock.objects.bulk_create(
        [
            ApplicationLock(name="cart_browser"),
            ApplicationLock(name="registration"),
        ]
    )


class Migration(migrations.Migration):
    dependencies = [
        ("recipes", "0010_recipe_macros_and_ascii_slugs"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ApplicationLock",
            fields=[
                ("name", models.CharField(max_length=32, primary_key=True, serialize=False)),
            ],
            options={
                "verbose_name": "блокировка приложения",
                "verbose_name_plural": "блокировки приложения",
            },
        ),
        migrations.RunPython(create_application_locks, migrations.RunPython.noop),
        migrations.CreateModel(
            name="BrowserLoginSession",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("remote_session_id", models.CharField(blank=True, max_length=128, null=True, unique=True)),
                ("status", models.CharField(choices=[("starting", "Запускается"), ("active", "Открыта"), ("completed", "Сохранена"), ("expired", "Истекла"), ("failed", "Ошибка")], db_index=True, default="starting", max_length=16)),
                ("error", models.CharField(blank=True, max_length=500)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("finished_at", models.DateTimeField(blank=True, null=True)),
                ("run", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="browser_login_sessions", to="recipes.cartrun")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="browser_login_sessions", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "ручной вход в браузер",
                "verbose_name_plural": "ручные входы в браузер",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="RegistrationInvite",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("token_digest", models.CharField(max_length=64, unique=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("expires_at", models.DateTimeField(db_index=True)),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("closed_at", models.DateTimeField(blank=True, null=True)),
                ("is_open", models.BooleanField(default=True, editable=False)),
                ("created_by", models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="created_registration_invites", to=settings.AUTH_USER_MODEL)),
                ("registered_user", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="registration_invite", to=settings.AUTH_USER_MODEL)),
            ],
            options={
                "verbose_name": "приглашение в семейную книгу",
                "verbose_name_plural": "приглашения в семейную книгу",
                "ordering": ["-created_at"],
                "constraints": [models.UniqueConstraint(condition=models.Q(("is_open", True)), fields=("is_open",), name="unique_open_registration_invite")],
            },
        ),
    ]
