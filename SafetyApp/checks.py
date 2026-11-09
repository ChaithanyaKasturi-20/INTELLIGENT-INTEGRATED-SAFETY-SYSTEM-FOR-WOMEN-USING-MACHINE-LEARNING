from django.core.checks import Warning, register


@register()
def check_sos_public_location_origin(app_configs, **kwargs):
    from .views import get_sos_public_base_url_diagnostic

    diagnostic = get_sos_public_base_url_diagnostic()
    if not diagnostic:
        return []

    return [
        Warning(
            diagnostic,
            hint='Set SOS_PUBLIC_BASE_URL to one public HTTPS origin, for example https://sos.example.org.',
            id='SafetyApp.sos.W001',
        )
    ]
