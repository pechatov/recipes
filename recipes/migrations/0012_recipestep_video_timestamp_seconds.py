from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("recipes", "0011_importjob_source_title_checked_at")]

    operations = [
        migrations.AddField(
            model_name="recipestep",
            name="video_timestamp_seconds",
            field=models.PositiveIntegerField(
                blank=True,
                editable=False,
                null=True,
                verbose_name="тайм-код видео, секунды",
            ),
        ),
    ]
