from django import template

from tips.models import Role

register = template.Library()


@register.filter
def role_names(keys, location):
    """['manager', 'owner'] -> 'Manager, Owner' (using the location's own role names)."""
    names = {r.key: r.name for r in Role.objects.filter(location=location, key__in=keys or [])}
    return ", ".join(names.get(k, k.replace("-", " ").capitalize()) for k in keys or [])
