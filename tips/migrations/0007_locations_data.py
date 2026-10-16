from django.db import migrations
from django.utils.text import slugify


def forwards(apps, schema_editor):
    Location = apps.get_model("tips", "Location")
    location = Location.objects.order_by("pk").first() or Location.objects.create()
    for loc in Location.objects.all():
        if not loc.slug:
            loc.slug = slugify(loc.name)[:34] or ("main" if loc.pk == location.pk else f"location-{loc.pk}")
            loc.save(update_fields=["slug"])
    for model in ("Role", "TipRuleSet", "Night", "CloseTime"):
        apps.get_model("tips", model).objects.filter(location__isnull=True).update(location=location)
    Membership = apps.get_model("tips", "Membership")
    for staff in apps.get_model("tips", "StaffMember").objects.all():
        Membership.objects.get_or_create(staff=staff, location=location,
                                         defaults={"default_role": staff.default_role, "active": staff.active})


class Migration(migrations.Migration):
    dependencies = [("tips", "0006_locations")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]
