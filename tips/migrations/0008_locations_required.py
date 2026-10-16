import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("tips", "0007_locations_data")]

    operations = [
        migrations.AlterField("location", "slug", models.SlugField(max_length=40, unique=True)),
        migrations.AlterField("role", "location", models.ForeignKey(
            on_delete=django.db.models.deletion.CASCADE, related_name="roles", to="tips.location")),
        migrations.AlterField("tipruleset", "location", models.ForeignKey(
            on_delete=django.db.models.deletion.CASCADE, related_name="rule_sets", to="tips.location")),
        migrations.AlterField("night", "location", models.ForeignKey(
            on_delete=django.db.models.deletion.CASCADE, related_name="nights", to="tips.location")),
        migrations.AlterField("closetime", "location", models.ForeignKey(
            on_delete=django.db.models.deletion.CASCADE, related_name="close_times", to="tips.location")),
        migrations.AlterUniqueTogether("role", {("location", "key")}),
        migrations.AlterUniqueTogether("closetime", {("location", "weekday")}),
        migrations.RemoveField("staffmember", "default_role"),
        migrations.RemoveField("staffmember", "active"),
    ]
