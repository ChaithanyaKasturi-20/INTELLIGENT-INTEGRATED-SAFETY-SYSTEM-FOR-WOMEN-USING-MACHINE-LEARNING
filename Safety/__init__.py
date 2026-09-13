import gettext

_original_translation = gettext.translation

def _compat_translation(domain, localedir=None, languages=None, class_=None, fallback=False, **kwargs):
    kwargs.pop('codeset', None)
    return _original_translation(domain, localedir=localedir, languages=languages, class_=class_, fallback=fallback)

gettext.translation = _compat_translation
