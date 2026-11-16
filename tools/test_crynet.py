#!/usr/bin/env python3
"""Check that the upstream cry-detection models run under onnxruntime.

Without --device: loads the model, prints its input/output shapes and times
inference on a random input.

With --device (e.g. plughw:1,0): additionally runs the real Notify path from
upstream (arecord -> mel features -> standardization -> model) a few times
and prints the class probabilities for each ~2 s recording.
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / 'detection'))

DISPLAY_NAMES = {'ambient': 'ambient', 'bad': 'crying', 'good': 'babbling'}


def load_config():
    with open(ROOT / 'config' / 'config.json') as f:
        return json.load(f)['inference']


def predict(session, feature):
    x = feature[np.newaxis, np.newaxis, :, :].astype(np.float32)
    out = session.run(None, {session.get_inputs()[0].name: x})[0].squeeze()
    # The last layer is nn.LogSoftmax (detection/neural_net.py), i.e. natural-log
    # probabilities. Upstream control/listen.py applies 10** instead and
    # renormalises, which sharpens them (p -> p**2.3); we show the true values.
    probabilities = np.exp(out)
    return probabilities / probabilities.sum()


def format_probabilities(label_names, probabilities):
    return '  '.join(f'{DISPLAY_NAMES[label_names[i]]}={p:.3f}'
                     for i, p in enumerate(probabilities))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model',
                        default='large_network',
                        choices=['small_network', 'large_network'])
    parser.add_argument('--device',
                        help='ALSA capture device, e.g. plughw:1,0')
    parser.add_argument('--count',
                        type=int,
                        default=10,
                        help='number of live recordings to classify')
    parser.add_argument('--amplification',
                        type=float,
                        default=10,
                        help='upstream default for the network models is 10')
    args = parser.parse_args()

    inference = load_config()
    label_names = {idx: name for name, idx in inference['labels'].items()}
    n_mel_bands, window_count = inference['input_shape']

    model_path = ROOT / 'control' / inference['models'][args.model]
    session = ort.InferenceSession(str(model_path),
                                   providers=['CPUExecutionProvider'])
    print(f'onnxruntime {ort.__version__}, numpy {np.__version__}')
    print(f'Model: {model_path.name}')
    for i in session.get_inputs():
        print(f'  input  {i.name}: {i.shape} {i.type}')
    for o in session.get_outputs():
        print(f'  output {o.name}: {o.shape} {o.type}')

    dummy = np.random.default_rng(0).standard_normal(
        (n_mel_bands, window_count)).astype(np.float32)
    predict(session, dummy)  # warm-up
    n_runs = 20
    start = time.perf_counter()
    for _ in range(n_runs):
        probabilities = predict(session, dummy)
    elapsed_ms = (time.perf_counter() - start) / n_runs * 1e3
    print(f'Random input: {format_probabilities(label_names, probabilities)}')
    print(f'Inference time: {elapsed_ms:.1f} ms per run')

    if args.device is None:
        return

    import features

    extractor = features.AudioFeatureExtractor(
        n_mel_bands=n_mel_bands,
        feature_window_count=window_count,
        backend='python_speech_features',
        disable_io=True)
    provider = features.FeatureProvider(
        args.device,
        extractor,
        min_sound_contrast=-np.inf,  # always return a feature
        amplification=args.amplification,
        standardization_file=ROOT / 'control' / 'standardization.npz')

    print(f'\nRecording from {args.device}, '
          f'{extractor.feature_duration:.2f} s per clip. Ctrl+C to stop.')
    for _ in range(args.count):
        feature, bg_level, contrast, _ = provider()
        probabilities = predict(session, feature)
        print(f'bg={bg_level:6.1f} dB  contrast={contrast:5.1f} dB  '
              f'{format_probabilities(label_names, probabilities)}')


if __name__ == '__main__':
    main()
