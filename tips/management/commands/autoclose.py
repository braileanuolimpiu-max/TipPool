"""Close out nights whose closing time has passed.

    python manage.py autoclose          # check once (use from a cron job every 5 minutes)
    python manage.py autoclose --loop   # keep checking every minute (handy when running locally)
"""
import time

from django.core.management.base import BaseCommand

from tips import autoclose, digest, watch


class Command(BaseCommand):
    help = "Automatically close nights at their scheduled closing time."

    def add_arguments(self, parser):
        parser.add_argument("--loop", action="store_true", help="Keep running, checking every minute.")

    def handle(self, *args, **options):
        while True:
            for line in autoclose.run():
                self.stdout.write(line)
            for alert in watch.run():
                self.stdout.write(f"Alert: {alert.text}")
            if digest.run():
                self.stdout.write("Sent summary emails")
            if not options["loop"]:
                break
            time.sleep(60)
