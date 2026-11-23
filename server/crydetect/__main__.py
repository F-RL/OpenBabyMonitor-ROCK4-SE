"""Notify mode: listen to the microphone and raise alerts on crying (or loud sound).

Run by babymonitor-crydetect.service as `python -m crydetect --device plughw:1,0`.
Records continuously in ~2 s clips, classifies each one and writes the latest
reading and any alerts to the shared SQLite database (see babymonitor/db.py), where
the web service picks them up. Settings are re-read before every clip.
"""
import argparse
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

from babymonitor import db, notify_settings
from babymonitor.notify import STATUS_KEY

from . import push
from .detector import LABELS, create_notifier
from .features import CLIP_SAMPLES, SAMPLE_RATE, FeatureExtractor, LoudnessAnalyzer, Standardizer

log = logging.getLogger('crydetect')

MODELS_DIR = Path(__file__).resolve().parent / 'models'
MODEL_FILES = {'small_network': 'crynet_small.onnx', 'large_network': 'crynet_large.onnx'}
AMBIENT_PROBABILITIES = np.array([1.0, 0.0, 0.0])


class Recorder:
    """One long-running arecord process, read in clips (no gaps between them)."""

    BYTES_PER_SAMPLE = 4  # FLOAT_LE

    def __init__(self, device):
        self.process = subprocess.Popen(
            ['arecord', f'--device={device}', '--quiet', '--file-type=raw', '--format=FLOAT_LE',
             f'--rate={SAMPLE_RATE}', '--channels=1'],
            stdout=subprocess.PIPE)

    def read(self, n_samples=CLIP_SAMPLES):
        n_bytes = n_samples * self.BYTES_PER_SAMPLE
        data = self.process.stdout.read(n_bytes)
        if len(data) < n_bytes:
            raise RuntimeError(f'arecord stopped (exit code {self.process.poll()})')
        return np.frombuffer(data, dtype='<f4')

    def skip(self, seconds):
        remaining = int(seconds * SAMPLE_RATE)
        while remaining > 0:
            n = min(remaining, SAMPLE_RATE)
            self.read(n)
            remaining -= n

    def close(self):
        self.process.terminate()
        self.process.wait()


class Network:
    def __init__(self, path):
        import onnxruntime  # only needed on the device, not for the web service's tests

        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = 2  # leave the other cores alone
        self.session = onnxruntime.InferenceSession(str(path), options,
                                                    providers=['CPUExecutionProvider'])
        self.input_name = self.session.get_inputs()[0].name

    def __call__(self, feature):
        x = feature[np.newaxis, np.newaxis, :, :].astype(np.float32)
        log_probabilities = self.session.run(None, {self.input_name: x})[0].squeeze()
        # The last layer is LogSoftmax (natural log). Upstream used 10** here.
        probabilities = np.exp(log_probabilities)
        return probabilities / probabilities.sum()


class Pipeline:
    """Turns clips into readings and alerts according to the current settings."""

    def __init__(self, models_dir=MODELS_DIR, load_network=Network, clock=time.monotonic):
        self.models_dir = Path(models_dir)
        self.load_network = load_network
        self.clock = clock
        self.extractor = FeatureExtractor()
        self.standardizer = Standardizer(self.models_dir / 'standardization.npz')
        self.analyzer = LoudnessAnalyzer()
        self.networks = {}
        self.settings = None

    def configure(self, settings):
        if settings == self.settings:
            return
        log.info('Settings: %s', settings)
        self.settings = settings
        self.analyzer.background_offset = settings['background_loudness_level_offset']
        self.notifier = create_notifier(settings, clock=self.clock)
        model = settings['model']
        if model in MODEL_FILES and model not in self.networks:
            self.networks[model] = self.load_network(self.models_dir / MODEL_FILES[model])

    def process(self, waveform):
        """Return (reading, alert kind or None) for one clip."""
        settings = self.settings
        feature, loudness = self.extractor(waveform)
        background, contrast = self.analyzer(loudness)
        probabilities = None
        if settings['model'] in MODEL_FILES:
            if contrast < settings['min_sound_contrast']:
                probabilities = AMBIENT_PROBABILITIES  # too quiet to bother the network
            else:
                feature = feature + np.log(settings['amplification'])
                probabilities = self.networks[settings['model']](self.standardizer(feature))
        self.notifier.add(contrast, probabilities)
        reading = {
            'background': round(float(background), 1),
            'contrast': round(float(contrast), 1),
            'probabilities': None if probabilities is None else
            {name: round(float(p), 3) for name, p in zip(LABELS, probabilities)},
        }
        return reading, self.notifier.detect()


def run(device, database, models_dir):
    conn = db.connect(database)
    conn.executescript(db.SCHEMA)
    db.write_value(conn, 'runtime', STATUS_KEY, None)
    pipeline = Pipeline(models_dir)
    recorder = Recorder(device)
    push_config = push.config()
    log.info('Listening on %s; phone notifications %s', device,
             'on' if push_config['enabled'] else 'off')
    try:
        while True:
            settings = notify_settings.merged(
                db.read_value(conn, 'settings', notify_settings.SETTINGS_KEY))
            pipeline.configure(settings)
            reading, alert = pipeline.process(recorder.read())
            now = time.time()
            db.write_value(conn, 'runtime', STATUS_KEY,
                           dict(reading, time=now, model=settings['model']))
            if alert:
                log.info('Alert: %s (%s)', alert, reading)
                db.add_notify_event(conn, now, alert)
                if push_config['enabled']:
                    push.send_in_background(alert, push_config)
            if settings['interval']:
                recorder.skip(settings['interval'])
    finally:
        recorder.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--device', required=True, help='ALSA capture device, e.g. plughw:1,0')
    parser.add_argument('--database',
                        default=os.environ.get('BM_DATABASE',
                                               '/var/lib/babymonitor/babymonitor.sqlite3'))
    parser.add_argument('--models-dir', default=MODELS_DIR)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stderr)
    run(args.device, args.database, args.models_dir)


if __name__ == '__main__':
    main()
