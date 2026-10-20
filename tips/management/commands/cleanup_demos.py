"""Delete demo businesses older than a week:  python manage.py cleanup_demos"""
from django.core.management.base import BaseCommand

from tips import demo


class Command(BaseCommand):
    help = "Delete demo businesses (and their logins) older than 7 days."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=7)

    def handle(self, *args, **options):
        self.stdout.write(f"Removed {demo.cleanup(options['days'])} demo business(es).")
