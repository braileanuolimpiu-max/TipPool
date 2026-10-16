import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tips", "0015_business_accounts_data")]

    operations = [
        migrations.AlterField("location", "business", models.ForeignKey(
            on_delete=django.db.models.deletion.CASCADE, related_name="locations", to="tips.business")),
        migrations.AlterField("staffmember", "business", models.ForeignKey(
            on_delete=django.db.models.deletion.CASCADE, related_name="staff", to="tips.business")),
    ]
