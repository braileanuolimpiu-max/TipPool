"""Extra password rules."""
from django.core.exceptions import ValidationError


class NotTheCurrentPassword:
    """Changing or resetting a password to the one already in use doesn't count as a change."""

    def validate(self, password, user=None):
        if user is not None and user.pk and user.has_usable_password() and user.check_password(password):
            raise ValidationError("That's your current password. Choose a new one.", code="password_unchanged")

    def get_help_text(self):
        return "Your new password can't be the same as your current one."
