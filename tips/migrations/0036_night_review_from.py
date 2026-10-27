# Written by hand (same as makemigrations would make): the review window restarts after an automatic re-split,
# and a new scheduler address retires the environment's key

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tips', '0035_more_ai_services'),
    ]

    operations = [
        migrations.AddField(
            model_name='night',
            name='review_from',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='platformconfig',
            name='cron_env_retired',
            field=models.BooleanField(default=False),
        ),
    ]
