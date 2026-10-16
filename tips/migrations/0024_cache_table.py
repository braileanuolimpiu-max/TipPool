from django.core.management import call_command
from django.db import migrations


def create_cache_table(apps, schema_editor):
    """Rate limits are counted in a shared database cache in production: make sure its table exists."""
    call_command("createcachetable", verbosity=0)


class Migration(migrations.Migration):
    dependencies = [("tips", "0023_payout_attempt")]
    operations = [migrations.RunPython(create_cache_table, migrations.RunPython.noop)]
