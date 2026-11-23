import numpy as np
import pytest

from babymonitor import db, notify_settings
from crydetect import __main__ as crydetect
from crydetect.detector import BABBLING, CRYING, UNSURE, classify, create_notifier
from crydetect.features import CLIP_SAMPLES


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def settings(**changes):
    return notify_settings.updated(notify_settings.DEFAULTS, changes)


CRY = [0.1, 0.8, 0.1]
BABBLE = [0.1, 0.1, 0.8]
QUIET = [0.95, 0.03, 0.02]
EITHER = [0.2, 0.45, 0.35]  # clearly not ambient, unclear which


def feed(notifier, *recordings):
    results = []
    for p in recordings:
        notifier.add(30.0, np.array(p))
        results.append(notifier.detect())
    return results


def test_classify():
    assert classify(np.array(CRY), 0.65) == {CRYING}
    assert classify(np.array(BABBLE), 0.65) == {BABBLING}
    assert classify(np.array(EITHER), 0.65) == UNSURE
    assert classify(np.array(QUIET), 0.65) == {0}


def test_crying_alert_needs_enough_recent_recordings():
    n = create_notifier(settings(consecutive_recordings=4, fraction_threshold=50), clock=Clock())
    assert feed(n, CRY, QUIET, QUIET) == [None] * 3  # history not full yet
    assert feed(n, QUIET) == [None]  # 1/4 crying
    assert feed(n, CRY) == [None]  # 1/4: the first CRY dropped out
    assert feed(n, CRY) == ['crying']  # 2/4


def test_alerts_are_spaced_by_min_interval():
    clock = Clock()
    n = create_notifier(settings(consecutive_recordings=2, min_notification_interval=180),
                        clock=clock)
    assert feed(n, CRY, CRY) == [None, 'crying']
    clock.now += 60
    assert feed(n, CRY, CRY) == [None, None]
    clock.now += 200
    assert feed(n, CRY) == ['crying']


def test_babbling_ignored_unless_enabled():
    n = create_notifier(settings(consecutive_recordings=2), clock=Clock())
    assert feed(n, BABBLE, BABBLE, EITHER, EITHER) == [None] * 4
    n = create_notifier(settings(consecutive_recordings=2, notify_on_babbling=True), clock=Clock())
    assert feed(n, BABBLE, BABBLE) == [None, 'babbling']


def test_or_counts_unsure_recordings():
    n = create_notifier(settings(consecutive_recordings=4, notify_on_babbling=True),
                        clock=Clock())
    # Upstream alerted here already after one unsure recording.
    assert feed(n, QUIET, QUIET, QUIET, EITHER) == [None] * 4
    assert feed(n, CRY) == ['crying_or_babbling']  # 2/4 crying, babbling or unsure


def test_and_needs_both():
    n = create_notifier(settings(consecutive_recordings=4, notify_on_babbling=True,
                                 notify_and_or='and'), clock=Clock())
    assert feed(n, CRY, CRY, CRY, CRY) == [None] * 4  # upstream alerted 'bad' here
    n = create_notifier(settings(consecutive_recordings=4, notify_on_babbling=True,
                                 notify_and_or='and'), clock=Clock())
    assert feed(n, CRY, BABBLE, CRY, BABBLE) == [None, None, None, 'crying_and_babbling']


def test_sound_level_notifier():
    n = create_notifier(settings(model='sound_level_threshold', consecutive_recordings=2,
                                 min_sound_contrast=15, fraction_threshold=60), clock=Clock())
    n.add(5.0)
    n.add(20.0)
    assert n.detect() is None
    n.add(20.0)
    assert n.detect() == 'sound'


def test_settings_validation():
    with pytest.raises(ValueError):
        notify_settings.updated(notify_settings.DEFAULTS, {'probability_threshold': 101})
    with pytest.raises(ValueError):
        notify_settings.updated(notify_settings.DEFAULTS, {'model': 'huge_network'})
    with pytest.raises(ValueError):
        notify_settings.updated(notify_settings.DEFAULTS, {'nonsense': 1})
    with pytest.raises(ValueError):
        notify_settings.updated(notify_settings.DEFAULTS, {'notify_on_crying': 1})
    # Bad stored values fall back to defaults rather than crashing the detector.
    assert notify_settings.merged({'interval': 'x', 'amplification': 5})['interval'] == 0
    assert notify_settings.merged({'amplification': 5})['amplification'] == 5.0


class FakeNetwork:
    def __init__(self, path):
        self.path = path
        self.calls = 0

    def __call__(self, feature):
        assert feature.shape == (64, 128)
        self.calls += 1
        return np.array(CRY)


def noise(level):
    return (level * np.random.default_rng(0).standard_normal(CLIP_SAMPLES)).astype(np.float32)


def test_pipeline_skips_network_for_quiet_clips():
    pipeline = crydetect.Pipeline(load_network=FakeNetwork, clock=Clock())
    pipeline.configure(settings(consecutive_recordings=1, min_sound_contrast=40))
    reading, alert = pipeline.process(noise(0.01))  # steady noise: no contrast
    assert reading['probabilities'] == {'ambient': 1.0, 'crying': 0.0, 'babbling': 0.0}
    assert alert is None
    assert pipeline.networks['large_network'].calls == 0


def test_pipeline_alerts_on_loud_crying():
    pipeline = crydetect.Pipeline(load_network=FakeNetwork, clock=Clock())
    pipeline.configure(settings(consecutive_recordings=1, min_sound_contrast=0))
    reading, alert = pipeline.process(noise(0.1))
    assert reading['probabilities']['crying'] == 0.8
    assert alert == 'crying'
    assert pipeline.networks['large_network'].path.name == 'crynet_large.onnx'


def test_pipeline_keeps_state_when_settings_unchanged():
    pipeline = crydetect.Pipeline(load_network=FakeNetwork, clock=Clock())
    pipeline.configure(settings())
    notifier = pipeline.notifier
    pipeline.configure(settings())
    assert pipeline.notifier is notifier
    pipeline.configure(settings(fraction_threshold=70))
    assert pipeline.notifier is not notifier


def test_real_network_classifies_noise_as_ambient():
    pytest.importorskip('onnxruntime')
    network = crydetect.Network(crydetect.MODELS_DIR / 'crynet_small.onnx')
    pipeline = crydetect.Pipeline(load_network=lambda path: network, clock=Clock())
    pipeline.configure(settings(model='small_network', min_sound_contrast=0))
    reading, _ = pipeline.process(noise(0.001))
    p = reading['probabilities']
    assert abs(sum(p.values()) - 1) < 0.01


def test_events_are_pruned(tmp_path):
    conn = db.connect(str(tmp_path / 'db.sqlite3'))
    conn.executescript(db.SCHEMA)
    for i in range(db.MAX_NOTIFY_EVENTS + 5):
        db.add_notify_event(conn, float(i), 'crying')
    events = db.notify_events_after(conn, 0)
    assert len(events) == db.MAX_NOTIFY_EVENTS
    assert events[-1]['id'] == db.last_notify_event_id(conn) == db.MAX_NOTIFY_EVENTS + 5
