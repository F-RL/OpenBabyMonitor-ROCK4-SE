"""Audio features for the cry networks, ported from upstream detection/features.py.

Upstream computed these with python_speech_features + scipy (backend
'python_speech_features'); this is the same computation in plain numpy, checked to
match it. The networks were trained on exactly these features, so the constants
must not change.
"""
from collections import deque

import numpy as np

SAMPLE_RATE = 8000
N_MEL_BANDS = 64
N_WINDOWS = 128
WINDOW_LENGTH = 256
WINDOW_SEPARATION = 128
FFT_LENGTH = 256
MIN_FREQUENCY = 125
MAX_FREQUENCY = 3600
LOG_OFFSET = 1e-9
# One network input: 16512 samples, 2.064 s.
CLIP_SAMPLES = FFT_LENGTH + (N_WINDOWS - 1) * WINDOW_SEPARATION


# --- From upstream detection/librosa_destilled.py (Slaney mel scale, as librosa) ---

def hz_to_mel(frequencies):
    frequencies = np.asanyarray(frequencies, dtype=float)
    f_sp = 200.0 / 3
    mels = frequencies / f_sp
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    log_t = frequencies >= min_log_hz
    return np.where(log_t, min_log_mel + np.log(np.maximum(frequencies, 1e-12) / min_log_hz) / logstep,
                    mels)


def mel_to_hz(mels):
    mels = np.asanyarray(mels, dtype=float)
    f_sp = 200.0 / 3
    min_log_hz = 1000.0
    min_log_mel = min_log_hz / f_sp
    logstep = np.log(6.4) / 27.0
    return np.where(mels >= min_log_mel, min_log_hz * np.exp(logstep * (mels - min_log_mel)),
                    f_sp * mels)


def fft_frequencies(sr, n_fft):
    return np.linspace(0, float(sr) / 2, int(1 + n_fft // 2), endpoint=True)


def mel_filters(sr, n_fft, n_mels, fmin, fmax):
    weights = np.zeros((n_mels, int(1 + n_fft // 2)), dtype=np.float32)
    fftfreqs = fft_frequencies(sr, n_fft)
    mel_f = mel_to_hz(np.linspace(hz_to_mel(fmin), hz_to_mel(fmax), n_mels + 2))
    fdiff = np.diff(mel_f)
    ramps = np.subtract.outer(mel_f, fftfreqs)
    for i in range(n_mels):
        lower = -ramps[i] / fdiff[i]
        upper = ramps[i + 2] / fdiff[i + 1]
        weights[i] = np.maximum(0, np.minimum(lower, upper))
    enorm = 2.0 / (mel_f[2:n_mels + 2] - mel_f[:n_mels])
    weights *= enorm[:, np.newaxis]
    return weights


def a_amplitude_weighting(frequencies):
    f_sq = np.asanyarray(frequencies)**2.0
    const = np.array([12200, 20.6, 107.7, 737.9])**2.0
    return const[0] * f_sq**2 / ((const[1] + f_sq) * np.sqrt(
        (const[2] + f_sq) * (const[3] + f_sq)) * (const[0] + f_sq))


def spectrogram_rms(spectrogram):
    frame_length = 2 * (spectrogram.shape[0] - 1)
    x = np.abs(spectrogram)**2
    x[0] *= 0.5
    if frame_length % 2 == 0:
        x[-1] *= 0.5
    return np.sqrt(2 * np.sum(x, axis=0) / frame_length**2)


def amplitude_to_db(amplitude):
    return 20 * np.log10(amplitude)


# --- Feature extraction ---

class FeatureExtractor:
    def __init__(self):
        # Symmetric Hann window, identical to scipy.signal.windows.hann(256).
        self.window = np.hanning(WINDOW_LENGTH)
        self.mel_filterbank = mel_filters(SAMPLE_RATE, FFT_LENGTH, N_MEL_BANDS, MIN_FREQUENCY,
                                          MAX_FREQUENCY)
        self.a_weights = a_amplitude_weighting(fft_frequencies(SAMPLE_RATE, FFT_LENGTH))

    def __call__(self, waveform):
        """Log-mel feature (64 x 128) and A-weighted loudness per window (128,)."""
        assert waveform.size == CLIP_SAMPLES
        starts = np.arange(N_WINDOWS) * WINDOW_SEPARATION
        frames = waveform[starts[:, np.newaxis] + np.arange(WINDOW_LENGTH)] * self.window
        spectrogram = np.abs(np.fft.rfft(frames, FFT_LENGTH)).T  # (129, 128)
        feature = np.log(self.mel_filterbank @ spectrogram + LOG_OFFSET)
        loudness = spectrogram_rms(self.a_weights[:, np.newaxis] * spectrogram)
        return feature, loudness


class LoudnessAnalyzer:
    """Background level (quietest 5% of windows, averaged over recent recordings) and
    how far the loudest 5% of the current recording rises above it, in dB."""

    REFERENCE_LEVEL = -67.8  # upstream's hard-coded calibration
    FRACTION = 0.05

    def __init__(self, background_offset=0.0, averaging_count=10):
        self.background_offset = background_offset
        self.backgrounds = deque(maxlen=averaging_count)

    def __call__(self, loudness):
        loudness = np.sort(loudness)
        n = int(self.FRACTION * loudness.size)
        foreground = amplitude_to_db(np.mean(loudness[-n:]))
        self.backgrounds.append(np.mean(loudness[:n]))
        background = amplitude_to_db(np.mean(self.backgrounds))
        contrast = foreground - background
        return background + self.background_offset - self.REFERENCE_LEVEL, contrast


class Standardizer:
    def __init__(self, path):
        with np.load(path) as data:
            self.mean = data['mean']
            self.standard_deviation = data['standard_deviation']

    def __call__(self, feature):
        return (feature - self.mean) / self.standard_deviation
