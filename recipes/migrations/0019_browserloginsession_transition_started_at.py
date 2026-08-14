from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("recipes", "0018_alter_browserloginsession_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="browserloginsession",
            name="transition_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
