"""Notify mode settings: definitions, defaults and validation.

Shared by the web service (which edits them) and the cry detector (which reads them
from the database on every recording, so changes apply without a restart).
Upstream's listen_settings in config/config.json are the reference.
"""

SETTINGS_KEY = 'notify'

MODELS = ('large_network', 'small_network', 'sound_level_threshold')

# name -> (type, default, extra). extra: (min, max) for numbers, allowed values for 'choice'.
SPEC = {
    'model': ('choice', 'large_network', MODELS),
    # Seconds to pause between ~2 s recordings (0: record continuously).
    'interval': ('int', 0, (0, 60)),
    # Gain applied to the recording before the network sees it (networks only).
    'amplification': ('float', 10.0, (1, 40)),
    # dB above the background a recording must reach to count as sound at all.
    'min_sound_contrast': ('float', 15.0, (0, 40)),
    'background_loudness_level_offset': ('float', 0.0, (-25, 25)),
    # True probability (%) a recording needs to count as crying/babbling. Upstream's
    # default of 80 was applied to over-sharpened probabilities (10**x instead of
    # e**x on the network's log-probabilities); 65 is about the same in true terms.
    'probability_threshold': ('int', 65, (0, 100)),
    # Alert when at least this % of the last `consecutive_recordings` match.
    'fraction_threshold': ('int', 50, (0, 100)),
    'consecutive_recordings': ('int', 6, (1, 20)),
    # Seconds between alerts.
    'min_notification_interval': ('int', 180, (0, 1800)),
    'notify_on_crying': ('bool', True, None),
    'notify_on_babbling': ('bool', False, None),
    'notify_and_or': ('choice', 'or', ('or', 'and')),
    # Browser: switch to Listen mode when an alert arrives.
    'autoplay_on_notify': ('bool', True, None),
}

DEFAULTS = {name: spec[1] for name, spec in SPEC.items()}


def coerce(name, value):
    """Return `value` converted to the setting's type, or raise ValueError."""
    kind, _, extra = SPEC[name]
    if kind == 'bool':
        if not isinstance(value, bool):
            raise ValueError(f'{name} must be true or false')
        return value
    if kind == 'choice':
        if value not in extra:
            raise ValueError(f'{name} must be one of {", ".join(extra)}')
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f'{name} must be a number')
    if kind == 'int':
        if value != int(value):
            raise ValueError(f'{name} must be a whole number')
        value = int(value)
    else:
        value = float(value)
    low, high = extra
    if not low <= value <= high:
        raise ValueError(f'{name} must be between {low} and {high}')
    return value


def merged(stored):
    """Defaults overlaid with the valid stored values (unknown or invalid ones are dropped)."""
    settings = dict(DEFAULTS)
    for name, value in (stored or {}).items():
        if name in SPEC:
            try:
                settings[name] = coerce(name, value)
            except ValueError:
                pass
    return settings


def updated(current, changes):
    """Apply a partial update; raise ValueError for unknown names or bad values."""
    unknown = set(changes) - set(SPEC)
    if unknown:
        raise ValueError(f'unknown setting: {", ".join(sorted(unknown))}')
    settings = dict(current)
    for name, value in changes.items():
        settings[name] = coerce(name, value)
    return settings


def describe():
    """The setting definitions in a form the browser can build a form from."""
    out = {}
    for name, (kind, default, extra) in SPEC.items():
        entry = {'type': kind, 'default': default}
        if kind == 'choice':
            entry['values'] = list(extra)
        elif kind in ('int', 'float'):
            entry['min'], entry['max'] = extra
        out[name] = entry
    return out
