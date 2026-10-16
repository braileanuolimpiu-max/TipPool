from django.db import migrations


def forwards(apps, schema_editor):
    """Everything that exists becomes one business; every existing login manages it."""
    Location = apps.get_model("tips", "Location")
    StaffMember = apps.get_model("tips", "StaffMember")
    if not (Location.objects.exists() or StaffMember.objects.exists()):
        return
    Business = apps.get_model("tips", "Business")
    first = Location.objects.order_by("created_at", "pk").first()
    business = Business.objects.create(name=(first.name if first and first.name else "My business"))
    Location.objects.filter(business__isnull=True).update(business=business)
    StaffMember.objects.filter(business__isnull=True).update(business=business)
    Manager = apps.get_model("tips", "Manager")
    for n, user in enumerate(apps.get_model("auth", "User").objects.order_by("pk")):
        Manager.objects.get_or_create(user=user, defaults={"business": business, "is_owner": n == 0})


class Migration(migrations.Migration):
    dependencies = [("tips", "0014_business_accounts"), ("auth", "0012_alter_user_first_name_max_length")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
