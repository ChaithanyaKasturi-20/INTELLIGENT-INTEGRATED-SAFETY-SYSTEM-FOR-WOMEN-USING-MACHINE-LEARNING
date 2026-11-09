from Safety.settings import *
import os

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ["SAFETY_TEST_DB"],
    }
}
