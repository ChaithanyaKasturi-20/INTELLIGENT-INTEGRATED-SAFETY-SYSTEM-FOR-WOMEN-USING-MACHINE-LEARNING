from django.apps import AppConfig


class SafetyappConfig(AppConfig):
    name = 'SafetyApp'

    def ready(self):
        from . import checks  # noqa: F401
