# Written by hand (same as makemigrations would make): the bot check on guest tip pages

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('tips', '0036_night_review_from'),
    ]

    operations = [
        migrations.AddField(
            model_name='platformconfig',
            name='turnstile_site_key',
            field=models.CharField(blank=True, max_length=100),
        ),
        migrations.AddField(
            model_name='platformconfig',
            name='turnstile_secret_encrypted',
            field=models.TextField(blank=True),
        ),
    ]
