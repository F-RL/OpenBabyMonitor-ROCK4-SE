"""Deciding when to alert, from a stream of per-recording results.

Ported from upstream control/listen.py (SoundLevelNotifier, InferenceNotifier) with
two upstream bugs fixed:
- "crying or babbling" alerted as soon as a single recently-uncertain recording
  was in the history, because the fraction was never compared to the threshold;
- "and" behaved like "or" (one of the two was enough).
"""
import time
from collections import deque

import numpy as np

AMBIENT, CRYING, BABBLING = 0, 1, 2
LABELS = ('ambient', 'crying', 'babbling')  # network output order
UNSURE = frozenset((CRYING, BABBLING))  # baby sound, but unclear which


def classify(probabilities, threshold):
    """Map one recording's probabilities to a set of labels it counts as."""
    best = int(np.argmax(probabilities))
    if best != AMBIENT and probabilities[best] >= threshold:
        return frozenset((best,))
    if 1 - probabilities[AMBIENT] >= threshold:
        return UNSURE
    return frozenset((AMBIENT,))


class _Notifier:
    def __init__(self, consecutive_recordings, fraction_threshold, min_notification_interval,
                 clock=time.monotonic):
        self.history = deque(maxlen=consecutive_recordings)
        self.fraction_threshold = fraction_threshold / 100
        self.min_interval = min_notification_interval
        self.clock = clock
        self.last_time = None

    def fraction(self, matches):
        return sum(1 for item in self.history if matches(item)) / len(self.history)

    def detect(self):
        if len(self.history) < self.history.maxlen:
            return None
        if self.last_time is not None and self.clock() - self.last_time <= self.min_interval:
            return None
        kind = self.decide()
        if kind is not None:
            self.last_time = self.clock()
            # Start over, so the same sound doesn't alert again right after the pause.
            self.history.clear()
        return kind


class SoundLevelNotifier(_Notifier):
    """Alert on loud sound (model 'sound_level_threshold')."""

    def __init__(self, settings, **kwargs):
        super().__init__(settings['consecutive_recordings'], settings['fraction_threshold'],
                         settings['min_notification_interval'], **kwargs)
        self.min_contrast = settings['min_sound_contrast']

    def add(self, contrast, probabilities=None):
        self.history.append(contrast >= self.min_contrast)

    def decide(self):
        return 'sound' if self.fraction(bool) >= self.fraction_threshold else None


class InferenceNotifier(_Notifier):
    """Alert on crying and/or babbling recognised by a network."""

    def __init__(self, settings, **kwargs):
        super().__init__(settings['consecutive_recordings'], settings['fraction_threshold'],
                         settings['min_notification_interval'], **kwargs)
        self.probability_threshold = settings['probability_threshold'] / 100
        self.on_crying = settings['notify_on_crying']
        self.on_babbling = settings['notify_on_babbling']
        self.require_both = settings['notify_and_or'] == 'and'

    def add(self, contrast, probabilities):
        self.history.append(classify(probabilities, self.probability_threshold))

    def decide(self):
        threshold = self.fraction_threshold
        crying = self.on_crying and self.fraction(lambda s: s == {CRYING}) >= threshold
        babbling = self.on_babbling and self.fraction(lambda s: s == {BABBLING}) >= threshold
        if crying and babbling:
            return 'crying_and_babbling'
        if self.on_crying and self.on_babbling:
            if self.require_both:
                return None
            if crying:
                return 'crying'
            if babbling:
                return 'babbling'
            if self.fraction(lambda s: s <= UNSURE) >= threshold:
                return 'crying_or_babbling'
            return None
        if crying:
            return 'crying'
        if babbling:
            return 'babbling'
        return None


def create_notifier(settings, **kwargs):
    if settings['model'] == 'sound_level_threshold':
        return SoundLevelNotifier(settings, **kwargs)
    return InferenceNotifier(settings, **kwargs)
