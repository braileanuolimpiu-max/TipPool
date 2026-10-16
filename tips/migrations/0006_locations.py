import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    """Several locations: the old single settings row becomes the first Location."""

    dependencies = [("tips", "0005_custom_roles")]

    operations = [
        migrations.RenameModel("RestaurantSettings", "Location"),
        migrations.AlterModelOptions("location", {"ordering": ["created_at", "pk"]}),
        migrations.AddField("location", "slug", models.SlugField(max_length=40, null=True)),
        migrations.AddField("location", "created_at", models.DateTimeField(default=django.utils.timezone.now)),
        migrations.AddField("role", "location", models.ForeignKey(
            null=True, on_delete=django.db.models.deletion.CASCADE, related_name="roles", to="tips.location")),
        migrations.AddField("tipruleset", "location", models.ForeignKey(
            null=True, on_delete=django.db.models.deletion.CASCADE, related_name="rule_sets", to="tips.location")),
        migrations.AddField("night", "location", models.ForeignKey(
            null=True, on_delete=django.db.models.deletion.CASCADE, related_name="nights", to="tips.location")),
        migrations.AddField("closetime", "location", models.ForeignKey(
            null=True, on_delete=django.db.models.deletion.CASCADE, related_name="close_times", to="tips.location")),
        migrations.AlterField("role", "key", models.SlugField(max_length=20)),
        migrations.AlterField("closetime", "weekday", models.PositiveSmallIntegerField()),
        migrations.CreateModel(
            name="Membership",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("default_role", models.CharField(max_length=20)),
                ("active", models.BooleanField(default=True)),
                ("location", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                               related_name="memberships", to="tips.location")),
                ("staff", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                            related_name="memberships", to="tips.staffmember")),
            ],
            options={"ordering": ["staff__name"], "unique_together": {("staff", "location")}},
        ),
    ]
