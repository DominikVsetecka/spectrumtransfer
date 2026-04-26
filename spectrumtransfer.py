#!/usr/bin/env python3
"""
Static spectrum-matching EQ transfer tool.

Features:
- Build an EQ delta curve from two WAV files (desired - target).
- Build an EQ delta curve from two Audacity-exported spectrum text files.
- Apply a saved curve to a target WAV.
- Export an Audacity Filter Curve EQ preset text snippet.
"""

from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Tuple

import numpy as np
from scipy.io import wavfile
from scipy.ndimage import gaussian_filter1d
from scipy.signal import fftconvolve, istft, stft


EPS = 1e-12


@dataclass
class Curve:
    freqs_hz: np.ndarray
    gain_db: np.ndarray

    def sorted(self) -> "Curve":
        idx = np.argsort(self.freqs_hz)
        return Curve(self.freqs_hz[idx], self.gain_db[idx])

    def interp(self, freqs_hz: np.ndarray) -> np.ndarray:
        src = self.sorted()
        return np.interp(freqs_hz, src.freqs_hz, src.gain_db, left=src.gain_db[0], right=src.gain_db[-1])


@dataclass
class ReverbMetrics:
    rt60_s: float
    tail_direct_ratio: float


@dataclass
class ReverbProfile:
    fullband: ReverbMetrics
    band_rt60_s: np.ndarray
    band_tail_direct_ratio: np.ndarray


@dataclass
class ReverbTransfer:
    action: str
    rt60_s: float
    wet_mix: float
    dereverb_amount: float
    reason: str


def _audio_to_float32(data: np.ndarray) -> np.ndarray:
    if np.issubdtype(data.dtype, np.floating):
        out = data.astype(np.float32, copy=False)
        return np.clip(out, -1.0, 1.0)

    if data.dtype == np.int16:
        return data.astype(np.float32) / 32768.0
    if data.dtype == np.int32:
        return data.astype(np.float32) / 2147483648.0
    if data.dtype == np.uint8:
        return (data.astype(np.float32) - 128.0) / 128.0

    max_abs = np.max(np.abs(data)) if data.size else 1.0
    if max_abs < EPS:
        max_abs = 1.0
    return (data.astype(np.float32) / max_abs).astype(np.float32)


def read_wav(path: Path) -> Tuple[int, np.ndarray]:
    sr, data = wavfile.read(path)
    x = _audio_to_float32(data)
    if x.ndim == 1:
        x = x[:, np.newaxis]
    return sr, x


def write_wav(path: Path, sr: int, data: np.ndarray) -> None:
    y = np.clip(data, -1.0, 1.0)
    int16 = np.round(y * 32767.0).astype(np.int16)
    wavfile.write(path, sr, int16)


def to_mono(x: np.ndarray) -> np.ndarray:
    if x.ndim == 1:
        return x
    return np.mean(x, axis=1)


def average_spectrum_db(mono_audio: np.ndarray, sr: int, n_fft: int, hop: int) -> Tuple[np.ndarray, np.ndarray]:
    noverlap = n_fft - hop
    freqs, _, zxx = stft(
        mono_audio,
        fs=sr,
        window="hann",
        nperseg=n_fft,
        noverlap=noverlap,
        boundary="zeros",
        padded=True,
    )
    power = np.mean(np.abs(zxx) ** 2, axis=1) + EPS
    spectrum_db = 10.0 * np.log10(power)
    return freqs, spectrum_db


def smooth_curve_log_freq(freqs_hz: np.ndarray, gain_db: np.ndarray, smooth_octaves: float) -> np.ndarray:
    if smooth_octaves <= 0:
        return gain_db.copy()

    # Skip DC for log mapping.
    pos = freqs_hz > 0
    if np.count_nonzero(pos) < 3:
        return gain_db.copy()

    f = freqs_hz[pos]
    g = gain_db[pos]

    log_f = np.log2(f)
    grid = np.linspace(log_f.min(), log_f.max(), len(log_f))
    g_grid = np.interp(grid, log_f, g)

    delta = grid[1] - grid[0] if len(grid) > 1 else 1.0
    sigma_bins = max(0.5, smooth_octaves / max(delta, EPS))
    g_smooth = gaussian_filter1d(g_grid, sigma=sigma_bins, mode="nearest")

    out = gain_db.copy()
    out[pos] = np.interp(log_f, grid, g_smooth)
    return out


def clamp_curve(gain_db: np.ndarray, max_cut_db: float, max_boost_db: float) -> np.ndarray:
    lo = min(max_cut_db, max_boost_db)
    hi = max(max_cut_db, max_boost_db)
    return np.clip(gain_db, lo, hi)


def build_high_cut_curve(
    freqs_hz: np.ndarray,
    cutoff_hz: float,
    gain_db: float,
    q: float,
) -> np.ndarray:
    if gain_db == 0.0:
        return np.zeros_like(freqs_hz, dtype=np.float64)

    safe_freqs = np.maximum(freqs_hz.astype(np.float64), 1.0)
    cutoff_hz = float(max(20.0, cutoff_hz))
    q = float(max(0.1, q))

    log_f = np.log2(safe_freqs)
    log_cutoff = np.log2(cutoff_hz)
    width_oct = max(0.03, 0.30 / q)
    slope = 1.0 / (1.0 + np.exp(-(log_f - log_cutoff) / width_oct))
    return gain_db * slope


def build_notch_curve(
    freqs_hz: np.ndarray,
    center_hz: float,
    gain_db: float,
    q: float,
) -> np.ndarray:
    if gain_db == 0.0:
        return np.zeros_like(freqs_hz, dtype=np.float64)

    safe_freqs = np.maximum(freqs_hz.astype(np.float64), 1.0)
    center_hz = float(max(20.0, center_hz))
    q = float(max(0.1, q))

    log_f = np.log2(safe_freqs)
    log_center = np.log2(center_hz)
    sigma_oct = max(0.02, 0.50 / q)
    return gain_db * np.exp(-0.5 * ((log_f - log_center) / sigma_oct) ** 2)


def add_whine_reduction_to_curve(
    curve: Curve,
    high_cut_enabled: bool,
    high_cut_freq_hz: float,
    high_cut_gain_db: float,
    high_cut_q: float,
    notch_freqs_hz: Iterable[float],
    notch_gain_db: float,
    notch_q: float,
) -> Curve:
    extra = np.zeros_like(curve.gain_db, dtype=np.float64)

    if high_cut_enabled:
        extra += build_high_cut_curve(
            freqs_hz=curve.freqs_hz,
            cutoff_hz=high_cut_freq_hz,
            gain_db=high_cut_gain_db,
            q=high_cut_q,
        )

    for freq_hz in notch_freqs_hz:
        if freq_hz <= 0:
            continue
        extra += build_notch_curve(
            freqs_hz=curve.freqs_hz,
            center_hz=freq_hz,
            gain_db=notch_gain_db,
            q=notch_q,
        )

    if not np.any(np.abs(extra) > 1e-9):
        return curve

    return Curve(curve.freqs_hz.copy(), (curve.gain_db.astype(np.float64) + extra).astype(np.float64))


def build_flat_curve() -> Curve:
    return Curve(
        freqs_hz=np.asarray([1.0, 40000.0], dtype=np.float64),
        gain_db=np.asarray([0.0, 0.0], dtype=np.float64),
    )


def build_voice_clarity_curve(profile: str) -> Curve:
    profile = str(profile).lower().strip()
    if profile == "clear":
        points = [
            (60.0, 0.0),
            (180.0, 0.0),
            (280.0, -1.5),
            (420.0, -1.2),
            (900.0, 0.0),
            (2200.0, 0.6),
            (3500.0, 2.5),
            (5200.0, 1.4),
            (8000.0, 0.8),
            (11500.0, 1.5),
            (16000.0, 0.8),
            (22000.0, 0.0),
            (40000.0, 0.0),
        ]
    elif profile == "gentle":
        points = [
            (60.0, 0.0),
            (180.0, 0.0),
            (280.0, -1.0),
            (420.0, -0.8),
            (900.0, 0.0),
            (2200.0, 0.4),
            (3200.0, 1.5),
            (5000.0, 0.9),
            (8000.0, 0.5),
            (11000.0, 1.0),
            (16000.0, 0.5),
            (22000.0, 0.0),
            (40000.0, 0.0),
        ]
    else:
        return build_flat_curve()

    freqs, gains = zip(*points)
    return Curve(np.asarray(freqs, dtype=np.float64), np.asarray(gains, dtype=np.float64))


def build_second_step_eq_curve() -> Curve:
    points = [
        (60.0, 0.0),
        (100.0, 0.0),
        (125.0, 0.0),
        (160.0, -2.6),
        (200.0, -3.2),
        (250.0, 0.0),
        (315.0, 0.0),
        (500.0, 0.0),
        (1000.0, 0.0),
        (2000.0, 0.0),
        (5000.0, 0.0),
        (6300.0, 0.0),
        (8000.0, 3.2),
        (10000.0, 6.0),
        (12000.0, 3.0),
        (16000.0, -5.0),
        (20000.0, -12.0),
        (40000.0, -12.0),
    ]
    freqs, gains = zip(*points)
    return Curve(np.asarray(freqs, dtype=np.float64), np.asarray(gains, dtype=np.float64))


def build_delta_curve_from_audio(
    desired_wav: Path,
    target_wav: Path,
    n_fft: int,
    hop: int,
    smooth_octaves: float,
    min_freq: float,
    max_freq: float,
    max_cut_db: float,
    max_boost_db: float,
) -> Curve:
    desired_sr, desired_x = read_wav(desired_wav)
    target_sr, target_x = read_wav(target_wav)

    f_des, s_des = average_spectrum_db(to_mono(desired_x), desired_sr, n_fft, hop)
    f_tgt, s_tgt = average_spectrum_db(to_mono(target_x), target_sr, n_fft, hop)

    s_des_on_tgt = np.interp(f_tgt, f_des, s_des, left=s_des[0], right=s_des[-1])
    delta = s_des_on_tgt - s_tgt
    delta = smooth_curve_log_freq(f_tgt, delta, smooth_octaves=smooth_octaves)

    band_mask = (f_tgt >= min_freq) & (f_tgt <= max_freq)
    delta = np.where(band_mask, delta, 0.0)
    delta = clamp_curve(delta, max_cut_db=max_cut_db, max_boost_db=max_boost_db)
    return Curve(freqs_hz=f_tgt, gain_db=delta)


def parse_numeric_pairs(lines: Iterable[str]) -> Tuple[np.ndarray, np.ndarray]:
    freq = []
    vals = []
    num_re = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
    for line in lines:
        text = line.strip()
        if not text:
            continue
        nums = num_re.findall(text)
        if len(nums) < 2:
            continue
        try:
            f = float(nums[0])
            v = float(nums[1])
        except ValueError:
            continue
        if f <= 0 or not np.isfinite(f) or not np.isfinite(v):
            continue
        freq.append(f)
        vals.append(v)
    if len(freq) < 2:
        raise ValueError("Could not parse at least 2 numeric freq/value pairs from input.")
    return np.asarray(freq, dtype=np.float64), np.asarray(vals, dtype=np.float64)


def load_spectrum_txt(path: Path) -> Curve:
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        freqs, values = parse_numeric_pairs(f.readlines())
    curve = Curve(freqs, values).sorted()
    unique_f, unique_idx = np.unique(curve.freqs_hz, return_index=True)
    return Curve(unique_f, curve.gain_db[unique_idx])


def build_delta_curve_from_spectra(
    desired_spectrum: Path,
    target_spectrum: Path,
    smooth_octaves: float,
    min_freq: float,
    max_freq: float,
    max_cut_db: float,
    max_boost_db: float,
) -> Curve:
    desired = load_spectrum_txt(desired_spectrum)
    target = load_spectrum_txt(target_spectrum)

    desired_on_target = desired.interp(target.freqs_hz)
    delta = desired_on_target - target.gain_db
    delta = smooth_curve_log_freq(target.freqs_hz, delta, smooth_octaves=smooth_octaves)
    delta = np.where((target.freqs_hz >= min_freq) & (target.freqs_hz <= max_freq), delta, 0.0)
    delta = clamp_curve(delta, max_cut_db=max_cut_db, max_boost_db=max_boost_db)
    return Curve(target.freqs_hz, delta)


def save_curve_csv(path: Path, curve: Curve) -> None:
    c = curve.sorted()
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["frequency_hz", "gain_db"])
        for f_hz, g_db in zip(c.freqs_hz, c.gain_db):
            w.writerow([f"{f_hz:.6f}", f"{g_db:.6f}"])


def load_curve_csv(path: Path) -> Curve:
    freqs = []
    gains = []
    with path.open("r", newline="", encoding="utf-8", errors="ignore") as f:
        rdr = csv.DictReader(f)
        fieldnames = rdr.fieldnames or []
        if "frequency_hz" in fieldnames and "gain_db" in fieldnames:
            for row in rdr:
                try:
                    freqs.append(float(row["frequency_hz"]))
                    gains.append(float(row["gain_db"]))
                except (TypeError, ValueError):
                    continue
        else:
            f.seek(0)
            p_freq, p_gain = parse_numeric_pairs(f.readlines())
            freqs = p_freq.tolist()
            gains = p_gain.tolist()

    if len(freqs) < 2:
        raise ValueError(f"Curve file {path} has insufficient data.")
    c = Curve(np.asarray(freqs), np.asarray(gains)).sorted()
    unique_f, unique_idx = np.unique(c.freqs_hz, return_index=True)
    return Curve(unique_f, c.gain_db[unique_idx])


def _match_length(x: np.ndarray, n: int) -> np.ndarray:
    if len(x) == n:
        return x
    if len(x) > n:
        return x[:n]
    out = np.zeros(n, dtype=x.dtype)
    out[: len(x)] = x
    return out


def apply_curve_to_wav(
    target_wav: Path,
    out_wav: Path,
    curve: Curve,
    n_fft: int,
    hop: int,
    mix: float,
) -> None:
    sr, x = read_wav(target_wav)
    noverlap = n_fft - hop

    y = np.zeros_like(x)
    mix = float(np.clip(mix, 0.0, 1.0))

    # Precompute gains on STFT frequency bins.
    stft_freqs = np.fft.rfftfreq(n_fft, d=1.0 / sr)
    gain_db = curve.interp(stft_freqs)
    gain_lin = 10.0 ** (gain_db / 20.0)

    for ch in range(x.shape[1]):
        freqs, _, zxx = stft(
            x[:, ch],
            fs=sr,
            window="hann",
            nperseg=n_fft,
            noverlap=noverlap,
            boundary="zeros",
            padded=True,
        )
        if len(freqs) != len(gain_lin):
            g = np.interp(freqs, stft_freqs, gain_lin, left=gain_lin[0], right=gain_lin[-1])
        else:
            g = gain_lin
        z_mod = zxx * g[:, np.newaxis]

        _, ch_y = istft(
            z_mod,
            fs=sr,
            window="hann",
            nperseg=n_fft,
            noverlap=noverlap,
            input_onesided=True,
            boundary=True,
        )
        ch_y = _match_length(ch_y, x.shape[0])
        y[:, ch] = ((1.0 - mix) * x[:, ch]) + (mix * ch_y)

    write_wav(out_wav, sr, y)


def _db_to_lin(db: np.ndarray | float) -> np.ndarray | float:
    return 10.0 ** (np.asarray(db) / 20.0)


def _lin_to_dbfs(lin: np.ndarray) -> np.ndarray:
    return 20.0 * np.log10(np.maximum(lin, EPS))


def _peak_dbfs(audio: np.ndarray) -> float:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    return float(20.0 * np.log10(max(peak, EPS)))


def _apply_time_varying_gain_db(audio: np.ndarray, sr: int, hop_s: float, gain_db_frames: np.ndarray) -> np.ndarray:
    n = audio.shape[0]
    if n == 0 or len(gain_db_frames) == 0:
        return audio.astype(np.float32, copy=True)

    frame_t = np.arange(len(gain_db_frames), dtype=np.float64) * hop_s
    sample_t = np.arange(n, dtype=np.float64) / float(sr)
    gain_db = np.interp(
        sample_t,
        frame_t,
        gain_db_frames,
        left=float(gain_db_frames[0]),
        right=float(gain_db_frames[-1]),
    )
    gain_lin = _db_to_lin(gain_db).astype(np.float32)
    return (audio * gain_lin[:, np.newaxis]).astype(np.float32)


def apply_auto_level(
    audio: np.ndarray,
    sr: int,
    target_dbfs: float,
    floor_dbfs: float,
    max_boost_db: float,
    max_cut_db: float,
    attack_ms: float,
    release_ms: float,
) -> np.ndarray:
    mono = to_mono(audio).astype(np.float32, copy=False)
    env, hop_s = _rms_envelope(mono, sr=sr, frame_ms=80.0, hop_ms=20.0)
    levels_db = _lin_to_dbfs(env + EPS)

    desired_gain_db = target_dbfs - levels_db
    desired_gain_db = np.where(levels_db >= floor_dbfs, desired_gain_db, 0.0)
    desired_gain_db = np.clip(desired_gain_db, -abs(max_cut_db), abs(max_boost_db))

    tau_att = max(0.001, attack_ms / 1000.0)
    tau_rel = max(0.001, release_ms / 1000.0)
    alpha_att = float(np.exp(-hop_s / tau_att))
    alpha_rel = float(np.exp(-hop_s / tau_rel))

    smooth = np.zeros_like(desired_gain_db, dtype=np.float32)
    current = 0.0
    for i, target in enumerate(desired_gain_db):
        # More attenuation should react faster, gain-up should react slower.
        alpha = alpha_att if target < current else alpha_rel
        current = (alpha * current) + ((1.0 - alpha) * float(target))
        smooth[i] = current

    return _apply_time_varying_gain_db(audio, sr=sr, hop_s=hop_s, gain_db_frames=smooth)


def apply_rms_compressor(
    audio: np.ndarray,
    sr: int,
    threshold_dbfs: float,
    ratio: float,
    attack_ms: float,
    release_ms: float,
) -> np.ndarray:
    ratio = max(1.0, float(ratio))
    mono = to_mono(audio).astype(np.float32, copy=False)
    env, hop_s = _rms_envelope(mono, sr=sr, frame_ms=20.0, hop_ms=5.0)
    level_db = _lin_to_dbfs(env + EPS)

    over_db = np.maximum(0.0, level_db - threshold_dbfs)
    reduction_db = over_db * (1.0 - (1.0 / ratio))
    desired_gain_db = -reduction_db

    tau_att = max(0.001, attack_ms / 1000.0)
    tau_rel = max(0.001, release_ms / 1000.0)
    alpha_att = float(np.exp(-hop_s / tau_att))
    alpha_rel = float(np.exp(-hop_s / tau_rel))

    smooth = np.zeros_like(desired_gain_db, dtype=np.float32)
    current = 0.0
    for i, target in enumerate(desired_gain_db):
        # Compression gain reduction should clamp peaks quickly and recover more slowly.
        alpha = alpha_att if target < current else alpha_rel
        current = (alpha * current) + ((1.0 - alpha) * float(target))
        smooth[i] = current

    return _apply_time_varying_gain_db(audio, sr=sr, hop_s=hop_s, gain_db_frames=smooth)


def apply_peak_limiter(audio: np.ndarray, ceiling_dbfs: float) -> np.ndarray:
    ceiling_lin = float(_db_to_lin(float(ceiling_dbfs)))
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak <= ceiling_lin or peak <= EPS:
        return audio.astype(np.float32, copy=True)
    scale = ceiling_lin / peak
    return (audio * scale).astype(np.float32)


def apply_peak_normalize(audio: np.ndarray, target_dbfs: float) -> np.ndarray:
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    if peak <= EPS:
        return audio.astype(np.float32, copy=True)
    target_lin = float(_db_to_lin(float(target_dbfs)))
    return (audio * (target_lin / peak)).astype(np.float32)


def apply_peak_normalize_to_wav(processed_wav: Path, target_dbfs: float) -> str:
    sr, x = read_wav(processed_wav)
    before_peak = _peak_dbfs(x)
    y = apply_peak_normalize(x, target_dbfs=target_dbfs)
    after_peak = _peak_dbfs(y)
    write_wav(processed_wav, sr, y)
    return f"[peak-normalize] applied: target={target_dbfs:.1f} dBFS, peak {before_peak:.1f} -> {after_peak:.1f} dBFS."


def apply_peak_ceiling_to_wav(processed_wav: Path, ceiling_dbfs: float) -> str:
    sr, x = read_wav(processed_wav)
    before_peak = _peak_dbfs(x)
    y = apply_peak_limiter(x, ceiling_dbfs=ceiling_dbfs)
    after_peak = _peak_dbfs(y)
    write_wav(processed_wav, sr, y)
    return f"[peak-ceiling] applied: ceiling={ceiling_dbfs:.1f} dBFS, peak {before_peak:.1f} -> {after_peak:.1f} dBFS."


def apply_deesser(
    audio: np.ndarray,
    sr: int,
    low_hz: float,
    high_hz: float,
    threshold_dbfs: float,
    ratio: float,
    attack_ms: float,
    release_ms: float,
    strength: float,
) -> np.ndarray:
    strength = float(np.clip(strength, 0.0, 1.0))
    if strength <= 0.0:
        return audio.astype(np.float32, copy=True)

    low_hz = float(max(1500.0, low_hz))
    high_hz = float(max(low_hz + 500.0, high_hz))
    ratio = max(1.0, float(ratio))

    out = np.zeros_like(audio, dtype=np.float32)

    for ch in range(audio.shape[1]):
        x = audio[:, ch].astype(np.float32, copy=False)
        band = _one_pole_lowpass(_one_pole_highpass(x, sr=sr, cutoff_hz=low_hz), sr=sr, cutoff_hz=high_hz)

        env, hop_s = _rms_envelope(band, sr=sr, frame_ms=8.0, hop_ms=2.0)
        level_db = _lin_to_dbfs(env + EPS)
        over_db = np.maximum(0.0, level_db - threshold_dbfs)
        reduction_db = over_db * (1.0 - (1.0 / ratio)) * strength
        desired_gain_db = -reduction_db

        tau_att = max(0.001, attack_ms / 1000.0)
        tau_rel = max(0.001, release_ms / 1000.0)
        alpha_att = float(np.exp(-hop_s / tau_att))
        alpha_rel = float(np.exp(-hop_s / tau_rel))

        smooth = np.zeros_like(desired_gain_db, dtype=np.float32)
        current = 0.0
        for i, target in enumerate(desired_gain_db):
            alpha = alpha_att if target < current else alpha_rel
            current = (alpha * current) + ((1.0 - alpha) * float(target))
            smooth[i] = current

        processed_band = _apply_time_varying_gain_db(
            band[:, np.newaxis],
            sr=sr,
            hop_s=hop_s,
            gain_db_frames=smooth,
        )[:, 0]

        # Split/recombine: only attenuate sibilance band, keep body of voice intact.
        out[:, ch] = (x - band + processed_band).astype(np.float32)

    return out


def apply_deesser_to_wav(
    processed_wav: Path,
    low_hz: float,
    high_hz: float,
    threshold_dbfs: float,
    ratio: float,
    attack_ms: float,
    release_ms: float,
    strength: float,
) -> str:
    sr, x = read_wav(processed_wav)
    y = apply_deesser(
        audio=x,
        sr=sr,
        low_hz=low_hz,
        high_hz=high_hz,
        threshold_dbfs=threshold_dbfs,
        ratio=ratio,
        attack_ms=attack_ms,
        release_ms=release_ms,
        strength=strength,
    )
    write_wav(processed_wav, sr, y)
    return (
        "[deesser] applied: "
        f"band={low_hz:.0f}-{high_hz:.0f} Hz, threshold={threshold_dbfs:.1f} dBFS, "
        f"ratio={ratio:.2f}, strength={strength:.2f}."
    )


def apply_dynamics_chain(
    audio: np.ndarray,
    sr: int,
    target_dbfs: float,
    floor_dbfs: float,
    max_boost_db: float,
    max_cut_db: float,
    auto_attack_ms: float,
    auto_release_ms: float,
    compressor_threshold_dbfs: float,
    compressor_ratio: float,
    compressor_attack_ms: float,
    compressor_release_ms: float,
    limiter_ceiling_dbfs: float,
    strength: float,
) -> np.ndarray:
    strength = float(np.clip(strength, 0.0, 1.0))
    dry = audio.astype(np.float32, copy=True)

    y = apply_auto_level(
        audio=dry,
        sr=sr,
        target_dbfs=target_dbfs,
        floor_dbfs=floor_dbfs,
        max_boost_db=max_boost_db,
        max_cut_db=max_cut_db,
        attack_ms=auto_attack_ms,
        release_ms=auto_release_ms,
    )
    y = apply_rms_compressor(
        audio=y,
        sr=sr,
        threshold_dbfs=compressor_threshold_dbfs,
        ratio=compressor_ratio,
        attack_ms=compressor_attack_ms,
        release_ms=compressor_release_ms,
    )
    y = apply_peak_limiter(y, ceiling_dbfs=limiter_ceiling_dbfs)

    if strength < 1.0:
        y = ((1.0 - strength) * dry) + (strength * y)
    return y.astype(np.float32)


def apply_auto_level_to_wav(
    processed_wav: Path,
    target_dbfs: float,
    floor_dbfs: float,
    max_boost_db: float,
    max_cut_db: float,
    auto_attack_ms: float,
    auto_release_ms: float,
    compressor_threshold_dbfs: float,
    compressor_ratio: float,
    compressor_attack_ms: float,
    compressor_release_ms: float,
    limiter_ceiling_dbfs: float,
    strength: float,
) -> str:
    sr, x = read_wav(processed_wav)
    before_peak = _peak_dbfs(x)
    y = apply_dynamics_chain(
        audio=x,
        sr=sr,
        target_dbfs=target_dbfs,
        floor_dbfs=floor_dbfs,
        max_boost_db=max_boost_db,
        max_cut_db=max_cut_db,
        auto_attack_ms=auto_attack_ms,
        auto_release_ms=auto_release_ms,
        compressor_threshold_dbfs=compressor_threshold_dbfs,
        compressor_ratio=compressor_ratio,
        compressor_attack_ms=compressor_attack_ms,
        compressor_release_ms=compressor_release_ms,
        limiter_ceiling_dbfs=limiter_ceiling_dbfs,
        strength=strength,
    )
    after_peak = _peak_dbfs(y)
    write_wav(processed_wav, sr, y)
    return (
        "[autolevel] applied: "
        f"target={target_dbfs:.1f} dBFS, ceiling={limiter_ceiling_dbfs:.1f} dBFS, "
        f"peak {before_peak:.1f} -> {after_peak:.1f} dBFS."
    )


def _resample_linear(x: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    if src_sr == dst_sr or len(x) == 0:
        return x.astype(np.float32, copy=False)
    n_out = max(1, int(round(len(x) * float(dst_sr) / float(src_sr))))
    src_pos = np.linspace(0.0, len(x) - 1, num=len(x), dtype=np.float64)
    dst_pos = np.linspace(0.0, len(x) - 1, num=n_out, dtype=np.float64)
    y = np.interp(dst_pos, src_pos, x.astype(np.float64))
    return y.astype(np.float32)


def _rms_envelope(mono_audio: np.ndarray, sr: int, frame_ms: float = 20.0, hop_ms: float = 5.0) -> Tuple[np.ndarray, float]:
    frame = max(16, int(sr * (frame_ms / 1000.0)))
    hop = max(8, int(sr * (hop_ms / 1000.0)))

    if len(mono_audio) < frame:
        padded = np.zeros(frame, dtype=np.float32)
        padded[: len(mono_audio)] = mono_audio
        mono_audio = padded

    n_frames = 1 + (len(mono_audio) - frame) // hop
    env = np.zeros(n_frames, dtype=np.float32)
    for i in range(n_frames):
        s = i * hop
        seg = mono_audio[s : s + frame]
        env[i] = float(np.sqrt(np.mean(seg * seg) + EPS))

    return env, hop / float(sr)


def _detect_onsets(env: np.ndarray, hop_s: float) -> np.ndarray:
    if len(env) < 8:
        return np.array([], dtype=np.int64)

    rise = np.diff(env, prepend=env[0])
    pos = rise[rise > 0]
    if len(pos) == 0:
        return np.array([], dtype=np.int64)

    rise_thr = float(np.percentile(pos, 85))
    level_thr = float(np.percentile(env, 60))
    candidates = np.where((rise >= rise_thr) & (env >= level_thr))[0]
    if len(candidates) == 0:
        return np.array([], dtype=np.int64)

    min_dist = max(1, int(round(0.08 / max(hop_s, EPS))))
    picked = []
    last = -10**9
    for idx in candidates:
        if idx - last >= min_dist:
            picked.append(int(idx))
            last = int(idx)
        if len(picked) >= 200:
            break
    return np.asarray(picked, dtype=np.int64)


def _speech_activity_mask(env: np.ndarray) -> np.ndarray:
    if len(env) < 6:
        return np.ones(len(env), dtype=bool)

    env_db = _lin_to_dbfs(env + EPS)
    floor_db = float(np.percentile(env_db, 20))
    strong_thr = floor_db + 10.0
    weak_thr = floor_db + 6.0

    mask = np.zeros(len(env_db), dtype=bool)
    active = False
    for i, lv in enumerate(env_db):
        if not active and lv >= strong_thr:
            active = True
        elif active and lv < weak_thr:
            active = False
        mask[i] = active

    # Bridge tiny gaps to avoid choppy frame gating.
    gap = 0
    max_gap = 4
    for i in range(len(mask)):
        if mask[i]:
            if 0 < gap <= max_gap:
                mask[i - gap : i] = True
            gap = 0
        else:
            gap += 1

    if not np.any(mask):
        return env_db >= (floor_db + 3.0)
    return mask


def estimate_reverb_metrics(mono_audio: np.ndarray, sr: int) -> ReverbMetrics:
    x = mono_audio.astype(np.float32, copy=False)
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak > EPS:
        x = x / peak

    env, hop_s = _rms_envelope(x, sr=sr, frame_ms=20.0, hop_ms=5.0)
    speech_mask = _speech_activity_mask(env)
    onsets = _detect_onsets(env, hop_s=hop_s)

    direct_n = max(1, int(round(0.04 / max(hop_s, EPS))))
    tail_start = direct_n
    tail_end = max(tail_start + 1, int(round(0.45 / max(hop_s, EPS))))

    ratios = []
    rt60_values = []
    for onset in onsets:
        end_idx = onset + tail_end
        if end_idx >= len(env):
            continue
        if not np.any(speech_mask[onset : onset + direct_n]):
            continue
        speech_density = float(np.mean(speech_mask[onset:end_idx]))
        if speech_density < 0.18:
            continue

        direct = np.mean(env[onset : onset + direct_n] ** 2) + EPS
        tail = np.mean(env[onset + tail_start : end_idx] ** 2) + EPS
        ratios.append(tail / direct)

        decay_env = env[onset + tail_start : end_idx]
        if len(decay_env) >= 6:
            decay_db = 20.0 * np.log10(decay_env + EPS)
            t = np.arange(len(decay_db), dtype=np.float64) * hop_s
            slope, _ = np.polyfit(t, decay_db, 1)
            if slope < -1e-3:
                rt60_values.append(float(-60.0 / slope))

    if len(ratios) == 0:
        ratio = 0.08
    else:
        ratio = float(np.median(ratios))

    if len(rt60_values) == 0:
        rt60_s = 0.35
    else:
        rt60_s = float(np.median(rt60_values))

    rt60_s = float(np.clip(rt60_s, 0.12, 3.5))
    ratio = float(np.clip(ratio, 0.0, 2.0))
    return ReverbMetrics(rt60_s=rt60_s, tail_direct_ratio=ratio)


def estimate_reverb_profile(mono_audio: np.ndarray, sr: int) -> ReverbProfile:
    full = estimate_reverb_metrics(mono_audio, sr=sr)

    bands_hz = ((160.0, 1200.0), (1200.0, 4200.0), (4200.0, 10000.0))
    rt60 = np.zeros(len(bands_hz), dtype=np.float64)
    ratios = np.zeros(len(bands_hz), dtype=np.float64)

    for i, (lo, hi) in enumerate(bands_hz):
        band = _one_pole_lowpass(_one_pole_highpass(mono_audio, sr=sr, cutoff_hz=lo), sr=sr, cutoff_hz=hi)
        m = estimate_reverb_metrics(band, sr=sr)
        rt60[i] = m.rt60_s
        ratios[i] = m.tail_direct_ratio

    return ReverbProfile(
        fullband=full,
        band_rt60_s=rt60.astype(np.float32),
        band_tail_direct_ratio=ratios.astype(np.float32),
    )


def estimate_reverb_transfer(
    desired_mono: np.ndarray,
    target_mono: np.ndarray,
    sr: int,
    strength: float,
    mode: str,
) -> ReverbTransfer:
    strength = float(np.clip(strength, 0.0, 2.0))
    mode = str(mode).lower().strip()
    if mode not in {"auto", "add", "remove"}:
        mode = "auto"

    desired = estimate_reverb_profile(desired_mono, sr=sr)
    target = estimate_reverb_profile(target_mono, sr=sr)

    band_weights = np.asarray([0.25, 0.50, 0.25], dtype=np.float32)
    band_rt_gap = float(np.sum((desired.band_rt60_s - target.band_rt60_s) * band_weights))
    band_ratio_gap = float(np.sum((desired.band_tail_direct_ratio - target.band_tail_direct_ratio) * band_weights))

    full_rt_gap = desired.fullband.rt60_s - target.fullband.rt60_s
    full_ratio_gap = desired.fullband.tail_direct_ratio - target.fullband.tail_direct_ratio

    rt_gap = (0.6 * full_rt_gap) + (0.4 * band_rt_gap)
    ratio_gap = (0.6 * full_ratio_gap) + (0.4 * band_ratio_gap)

    add_needed = (rt_gap > 0.10) or (ratio_gap > 0.07)
    remove_needed = (rt_gap < -0.10) or (ratio_gap < -0.07)

    if mode == "add":
        want_add = add_needed
        want_remove = False
    elif mode == "remove":
        want_add = False
        want_remove = remove_needed
    else:
        want_add = add_needed and not remove_needed
        want_remove = remove_needed and not add_needed

    if want_add:
        # Conservative wet estimate to avoid "washed out" vocals.
        wet = 0.02 + max(0.0, ratio_gap) * 0.10 + max(0.0, rt_gap) * 0.05
        wet = float(np.clip(wet * strength, 0.015, 0.22))
        rt60 = float(np.clip(desired.fullband.rt60_s, 0.15, 3.0))
        return ReverbTransfer(
            action="add",
            rt60_s=rt60,
            wet_mix=wet,
            dereverb_amount=0.0,
            reason=(
                f"[reverb] add: wet={wet:.2f}, rt60={rt60:.2f}s "
                f"(desired rt60={desired.fullband.rt60_s:.2f}s, target rt60={target.fullband.rt60_s:.2f}s)."
            ),
        )

    if want_remove:
        rt_diff = max(0.0, target.fullband.rt60_s - desired.fullband.rt60_s)
        ratio_diff = max(
            0.0,
            target.fullband.tail_direct_ratio - desired.fullband.tail_direct_ratio,
        )
        amount = 0.16 + ratio_diff * 0.28 + rt_diff * 0.18
        amount = float(np.clip(amount * strength, 0.10, 0.80))
        return ReverbTransfer(
            action="remove",
            rt60_s=desired.fullband.rt60_s,
            wet_mix=0.0,
            dereverb_amount=amount,
            reason=(
                f"[reverb] remove: amount={amount:.2f} "
                f"(desired rt60={desired.fullband.rt60_s:.2f}s, target rt60={target.fullband.rt60_s:.2f}s)."
            ),
        )

    return ReverbTransfer(
        action="none",
        rt60_s=desired.fullband.rt60_s,
        wet_mix=0.0,
        dereverb_amount=0.0,
        reason=(
            f"[reverb] skipped: no clear reverb mismatch "
            f"(desired rt60={desired.fullband.rt60_s:.2f}s, target rt60={target.fullband.rt60_s:.2f}s)."
        ),
    )


def _estimate_tail_envelope_template(mono_audio: np.ndarray, sr: int, tail_n: int) -> np.ndarray:
    x = mono_audio.astype(np.float32, copy=False)
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak > EPS:
        x = x / peak

    env, hop_s = _rms_envelope(x, sr=sr, frame_ms=20.0, hop_ms=5.0)
    onsets = _detect_onsets(env, hop_s=hop_s)
    direct_n = max(1, int(round(0.03 * sr)))
    tail_len = max(direct_n + 64, tail_n)

    chunks = []
    for idx in onsets:
        s = int(round(idx * hop_s * sr))
        e = s + tail_len
        if s < 0 or e > len(x):
            continue
        seg = x[s:e]
        direct_rms = float(np.sqrt(np.mean(seg[:direct_n] * seg[:direct_n]) + EPS))
        if direct_rms < 1e-4:
            continue
        tail_abs = np.abs(seg[direct_n:]) / direct_rms
        src = np.linspace(0.0, 1.0, num=len(tail_abs), dtype=np.float64)
        dst = np.linspace(0.0, 1.0, num=tail_n, dtype=np.float64)
        chunks.append(np.interp(dst, src, tail_abs).astype(np.float32))
        if len(chunks) >= 120:
            break

    if not chunks:
        t = np.arange(tail_n, dtype=np.float32) / float(sr)
        return np.exp(-6.907755 * t / 0.40).astype(np.float32)

    median_env = np.median(np.stack(chunks, axis=0), axis=0).astype(np.float32)
    median_env = gaussian_filter1d(median_env, sigma=max(1.0, tail_n / 300.0), mode="nearest")
    median_env = np.maximum(median_env, 0.0)
    max_v = float(np.max(median_env)) if len(median_env) else 0.0
    if max_v > EPS:
        median_env /= max_v
    return median_env


def _estimate_tail_tone_cutoff(mono_audio: np.ndarray, sr: int) -> float:
    hi = _one_pole_highpass(mono_audio.astype(np.float32, copy=False), sr=sr, cutoff_hz=3500.0)
    lo = _one_pole_lowpass(mono_audio.astype(np.float32, copy=False), sr=sr, cutoff_hz=2200.0)
    hi_rms = float(np.sqrt(np.mean(hi * hi) + EPS))
    lo_rms = float(np.sqrt(np.mean(lo * lo) + EPS))
    ratio = hi_rms / max(lo_rms, EPS)
    cutoff = 5200.0 + (2200.0 * np.clip(ratio, 0.0, 1.8))
    return float(np.clip(cutoff, 4200.0, 9000.0))


def _estimate_early_reflections(mono_audio: np.ndarray, sr: int) -> Tuple[float, float, float]:
    x = _one_pole_highpass(mono_audio.astype(np.float32, copy=False), sr=sr, cutoff_hz=180.0)
    n = min(len(x), sr * 12)
    if n < sr // 4:
        return (7.0, 14.0, 24.0)
    xx = x[:n].astype(np.float64)
    corr = np.correlate(xx, xx, mode="full")
    corr = corr[n - 1 :]
    start = int(sr * 0.004)
    end = min(len(corr), int(sr * 0.045))
    if end - start < 8:
        return (7.0, 14.0, 24.0)
    local = corr[start:end]
    if np.max(local) <= 0:
        return (7.0, 14.0, 24.0)
    picks = np.argpartition(local, -3)[-3:]
    lags = np.sort(picks + start)
    return tuple(float(1000.0 * lag / sr) for lag in lags)


def build_matched_convolution_ir(
    desired_mono: np.ndarray,
    sr: int,
    rt60_s: float,
    pre_delay_ms: float = 14.0,
) -> np.ndarray:
    pre = max(0, int(sr * (pre_delay_ms / 1000.0)))
    tail_n = int(np.clip(sr * max(rt60_s, 0.10) * 1.25, sr * 0.12, sr * 3.2))
    n = pre + tail_n

    tail_template = _estimate_tail_envelope_template(desired_mono, sr=sr, tail_n=tail_n)
    t = np.arange(tail_n, dtype=np.float32) / float(sr)
    exp_decay = np.exp(-6.907755 * t / max(rt60_s, 0.10)).astype(np.float32)
    tail_env = (0.65 * tail_template) + (0.35 * exp_decay)
    tail_env = np.maximum(tail_env, 0.0)

    rng = np.random.default_rng(0)
    noise = rng.standard_normal(tail_n).astype(np.float32)
    noise = _one_pole_highpass(noise, sr=sr, cutoff_hz=140.0)
    cutoff = _estimate_tail_tone_cutoff(desired_mono, sr=sr)
    noise = _one_pole_lowpass(noise, sr=sr, cutoff_hz=cutoff)
    tail = noise * tail_env

    ir = np.zeros(n, dtype=np.float32)
    ir[0] = 1.0
    ir[pre:] += 0.60 * tail

    for delay_ms, gain in zip(_estimate_early_reflections(desired_mono, sr=sr), (0.22, 0.14, 0.09)):
        idx = min(n - 1, int(sr * (delay_ms / 1000.0)))
        ir[idx] += float(gain)

    norm = float(np.sqrt(np.sum(ir * ir) + EPS))
    ir = ir / norm
    return ir


def _one_pole_lowpass(x: np.ndarray, sr: int, cutoff_hz: float) -> np.ndarray:
    if cutoff_hz <= 0:
        return x.astype(np.float32, copy=True)
    alpha = float(np.exp(-2.0 * np.pi * cutoff_hz / float(sr)))
    y = np.empty_like(x, dtype=np.float32)
    prev = 0.0
    for i, v in enumerate(x):
        prev = (1.0 - alpha) * float(v) + alpha * prev
        y[i] = prev
    return y


def _one_pole_highpass(x: np.ndarray, sr: int, cutoff_hz: float) -> np.ndarray:
    if cutoff_hz <= 0:
        return x.astype(np.float32, copy=True)
    lp = _one_pole_lowpass(x, sr=sr, cutoff_hz=cutoff_hz)
    return (x - lp).astype(np.float32)


def apply_convolution_reverb(
    audio: np.ndarray,
    desired_mono: np.ndarray,
    sr: int,
    rt60_s: float,
    wet_mix: float,
) -> np.ndarray:
    wet_mix = float(np.clip(wet_mix, 0.0, 1.0))
    if wet_mix <= 0.0:
        return audio

    ir = build_matched_convolution_ir(desired_mono=desired_mono, sr=sr, rt60_s=rt60_s)
    y = np.zeros_like(audio, dtype=np.float32)

    for ch in range(audio.shape[1]):
        wet = fftconvolve(audio[:, ch], ir, mode="full")[: audio.shape[0]].astype(np.float32)
        y[:, ch] = ((1.0 - wet_mix) * audio[:, ch]) + (wet_mix * wet)

    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > 1.0:
        y /= peak * 1.01
    return y


def reduce_late_reverb(
    audio: np.ndarray,
    sr: int,
    amount: float,
    n_fft: int = 2048,
    hop: int = 512,
) -> np.ndarray:
    amount = float(np.clip(amount, 0.0, 1.0))
    if amount <= 0.0:
        return audio

    noverlap = n_fft - hop
    out = np.zeros_like(audio, dtype=np.float32)
    hop_s = hop / float(sr)
    tau_s = 0.22
    alpha = float(np.exp(-hop_s / max(tau_s, EPS)))

    floor_gain = float(np.clip(0.22 - (amount * 0.16), 0.05, 0.22))
    subtract = float(np.clip(0.75 * amount, 0.05, 0.75))

    for ch in range(audio.shape[1]):
        _, _, zxx = stft(
            audio[:, ch],
            fs=sr,
            window="hann",
            nperseg=n_fft,
            noverlap=noverlap,
            boundary="zeros",
            padded=True,
        )
        mag = np.abs(zxx)
        phase = np.angle(zxx)

        late = np.zeros_like(mag)
        for t in range(1, mag.shape[1]):
            late[:, t] = (alpha * late[:, t - 1]) + ((1.0 - alpha) * mag[:, t - 1])

        clean_mag = mag - (subtract * late)
        mask = np.clip(clean_mag / (mag + EPS), floor_gain, 1.0)
        z_clean = (mag * mask) * np.exp(1j * phase)

        _, y = istft(
            z_clean,
            fs=sr,
            window="hann",
            nperseg=n_fft,
            noverlap=noverlap,
            input_onesided=True,
            boundary=True,
        )
        y = _match_length(y, audio.shape[0]).astype(np.float32, copy=False)
        out[:, ch] = ((1.0 - amount) * audio[:, ch]) + (amount * y)

    peak = float(np.max(np.abs(out))) if out.size else 0.0
    if peak > 1.0:
        out /= peak * 1.01
    return out


def maybe_run_ml_dereverb(
    audio: np.ndarray,
    amount: float,
    model_path: Path | None,
) -> Tuple[np.ndarray, bool, str]:
    if model_path is None:
        return audio, False, "[reverb] ML dereverb skipped: no model path provided."
    if not model_path.exists():
        return audio, False, f"[reverb] ML dereverb skipped: model not found at {model_path}."

    try:
        import onnxruntime as ort  # type: ignore
    except Exception:
        return audio, False, "[reverb] ML dereverb skipped: onnxruntime not installed."

    try:
        sess = ort.InferenceSession(str(model_path), providers=["CPUExecutionProvider"])
        in_name = sess.get_inputs()[0].name
        out_name = sess.get_outputs()[0].name

        y = np.zeros_like(audio, dtype=np.float32)
        for ch in range(audio.shape[1]):
            x = audio[:, ch].astype(np.float32, copy=False)
            x_in = x[np.newaxis, np.newaxis, :]
            pred = sess.run([out_name], {in_name: x_in})[0]
            pred = np.asarray(pred).reshape(-1).astype(np.float32)
            pred = _match_length(pred, len(x)).astype(np.float32, copy=False)
            y[:, ch] = ((1.0 - amount) * x) + (amount * pred)

        peak = float(np.max(np.abs(y))) if y.size else 0.0
        if peak > 1.0:
            y /= peak * 1.01
        return y, True, f"[reverb] ML dereverb used: {model_path}"
    except Exception as exc:
        return audio, False, f"[reverb] ML dereverb skipped due to runtime error: {exc}"


def apply_reverb_match_to_wav(
    desired_wav: Path,
    target_wav: Path,
    processed_wav: Path,
    strength: float,
    mode: str,
    export_variants: bool = True,
    light_scale: float = 0.50,
    mid_scale: float = 1.00,
    prefer_ml_dereverb: bool = False,
    ml_dereverb_model: Path | None = None,
) -> Tuple[ReverbTransfer, Dict[str, Path]]:
    out_sr, out_audio = read_wav(processed_wav)
    desired_sr, desired_audio = read_wav(desired_wav)
    target_sr, target_audio = read_wav(target_wav)

    desired_mono = to_mono(desired_audio)
    target_mono = to_mono(target_audio)
    if desired_sr != out_sr:
        desired_mono = _resample_linear(desired_mono, src_sr=desired_sr, dst_sr=out_sr)
    if target_sr != out_sr:
        target_mono = _resample_linear(target_mono, src_sr=target_sr, dst_sr=out_sr)

    transfer = estimate_reverb_transfer(
        desired_mono=desired_mono,
        target_mono=target_mono,
        sr=out_sr,
        strength=strength,
        mode=mode,
    )
    suffix = processed_wav.suffix
    base_stem = processed_wav.stem
    variant_paths: Dict[str, Path] = {"mid": processed_wav}
    if export_variants:
        variant_paths = {
            "off": processed_wav.with_name(f"{base_stem}_room_off{suffix}"),
            "light": processed_wav.with_name(f"{base_stem}_room_light{suffix}"),
            "mid": processed_wav.with_name(f"{base_stem}_room_mid{suffix}"),
        }

    def _apply_room_variant(scale: float) -> np.ndarray:
        if transfer.action == "none":
            return out_audio.astype(np.float32, copy=True)
        if transfer.action == "add":
            wet = float(np.clip(transfer.wet_mix * scale, 0.0, 0.35))
            return apply_convolution_reverb(
                out_audio,
                desired_mono=desired_mono,
                sr=out_sr,
                rt60_s=transfer.rt60_s,
                wet_mix=wet,
            )
        amt = float(np.clip(transfer.dereverb_amount * scale, 0.0, 1.0))
        if prefer_ml_dereverb:
            y_ml, used, msg = maybe_run_ml_dereverb(
                audio=out_audio,
                amount=amt,
                model_path=ml_dereverb_model,
            )
            if used:
                print(msg)
                return y_ml
            print(msg)
        return reduce_late_reverb(out_audio, sr=out_sr, amount=amt)

    off_audio = out_audio.astype(np.float32, copy=True)
    light_audio = _apply_room_variant(scale=float(np.clip(light_scale, 0.0, 1.0)))
    mid_audio = _apply_room_variant(scale=float(np.clip(mid_scale, 0.0, 1.3)))

    if export_variants:
        write_wav(variant_paths["off"], out_sr, off_audio)
        write_wav(variant_paths["light"], out_sr, light_audio)
        write_wav(variant_paths["mid"], out_sr, mid_audio)
        write_wav(processed_wav, out_sr, mid_audio)
    else:
        write_wav(processed_wav, out_sr, mid_audio)
    return transfer, variant_paths


def _format_float(v: float) -> str:
    return f"{v:.6f}".rstrip("0").rstrip(".")


def audacity_filter_curve_text(
    curve: Curve,
    num_points: int,
    min_freq: float,
    max_freq: float,
    filter_length: int,
) -> str:
    # Keep <= 200 due Audacity hard limit.
    n = max(2, min(int(num_points), 200))
    f_min = max(1e-6, min_freq)
    f_max = max(f_min * 1.01, max_freq)
    freqs = np.geomspace(f_min, f_max, n)
    gains = curve.interp(freqs)

    parts = ["FilterCurve:"]
    for i, f_hz in enumerate(freqs):
        parts.append(f'f{i}="{_format_float(float(f_hz))}"')
    parts.append(f'FilterLength="{int(filter_length)}"')
    parts.append('InterpolateLin="0"')
    parts.append('InterpolationMethod="B-spline"')
    for i, g_db in enumerate(gains):
        parts.append(f'v{i}="{_format_float(float(g_db))}"')

    return " ".join(parts)


def save_text(path: Path, text: str) -> None:
    path.write_text(text.strip() + "\n", encoding="utf-8")


def build_common_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Transfer static EQ spectrum between vocal recordings."
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_whine_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--whine-high-cut",
            action="store_true",
            help="Add an extra upper-band cut for electrical whine reduction.",
        )
        sp.add_argument(
            "--whine-high-cut-freq-hz",
            type=float,
            default=17400.0,
            help="Center/transition frequency of the additional upper-band cut.",
        )
        sp.add_argument(
            "--whine-high-cut-gain-db",
            type=float,
            default=-30.0,
            help="Gain of the additional upper-band cut.",
        )
        sp.add_argument(
            "--whine-high-cut-q",
            type=float,
            default=1.2,
            help="Q / steepness of the additional upper-band cut.",
        )
        sp.add_argument(
            "--whine-notch-hz",
            type=float,
            nargs="*",
            default=[],
            help="Up to 3 optional notch frequencies for narrow electrical whine tones.",
        )
        sp.add_argument(
            "--whine-notch-gain-db",
            type=float,
            default=-18.0,
            help="Gain for optional whine notch cuts.",
        )
        sp.add_argument(
            "--whine-notch-q",
            type=float,
            default=8.0,
            help="Q for optional whine notch cuts.",
        )

    def add_common_curve_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--smooth-octaves", type=float, default=0.20, help="Log-frequency smoothing width in octaves.")
        sp.add_argument("--min-freq", type=float, default=60.0, help="Only apply matching from this frequency up.")
        sp.add_argument("--max-freq", type=float, default=16000.0, help="Only apply matching up to this frequency.")
        sp.add_argument("--max-cut-db", type=float, default=-12.0, help="Maximum attenuation clamp.")
        sp.add_argument("--max-boost-db", type=float, default=12.0, help="Maximum boost clamp.")
        sp.add_argument("--curve-csv", type=Path, required=True, help="Where to write derived curve CSV.")
        sp.add_argument("--audacity-preset", type=Path, help="Optional Audacity Filter Curve EQ preset text output.")
        sp.add_argument("--audacity-points", type=int, default=180, help="Audacity preset point count (max 200).")
        sp.add_argument("--audacity-filter-length", type=int, default=8191, help="FilterLength field for Audacity preset.")
        add_whine_args(sp)

    def add_dynamics_args(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--auto-level",
            action="store_true",
            help="Apply dynamic loudness correction (auto gain + compressor + limiter).",
        )
        sp.add_argument("--target-dbfs", type=float, default=-6.0, help="Auto-level loudness target in dBFS.")
        sp.add_argument("--level-floor-dbfs", type=float, default=-42.0, help="Do not add gain below this level.")
        sp.add_argument("--max-auto-boost-db", type=float, default=12.0, help="Maximum auto-gain boost.")
        sp.add_argument("--max-auto-cut-db", type=float, default=12.0, help="Maximum auto-gain attenuation.")
        sp.add_argument("--auto-attack-ms", type=float, default=35.0, help="Auto-gain attack (ms).")
        sp.add_argument("--auto-release-ms", type=float, default=450.0, help="Auto-gain release (ms).")
        sp.add_argument(
            "--compressor-threshold-dbfs",
            type=float,
            default=-12.0,
            help="Compressor threshold in dBFS.",
        )
        sp.add_argument("--compressor-ratio", type=float, default=2.2, help="Compressor ratio.")
        sp.add_argument("--compressor-attack-ms", type=float, default=12.0, help="Compressor attack (ms).")
        sp.add_argument("--compressor-release-ms", type=float, default=120.0, help="Compressor release (ms).")
        sp.add_argument("--limiter-ceiling-dbfs", type=float, default=-3.0, help="Peak limiter ceiling in dBFS.")
        sp.add_argument(
            "--dynamics-strength",
            type=float,
            default=1.0,
            help="Blend dynamics processing, from 0..1.",
        )
        sp.add_argument("--de-ess", action="store_true", help="Apply de-esser to reduce sibilance after EQ.")
        sp.add_argument("--de-ess-low-hz", type=float, default=4500.0, help="De-esser low crossover frequency.")
        sp.add_argument("--de-ess-high-hz", type=float, default=10000.0, help="De-esser high crossover frequency.")
        sp.add_argument("--de-ess-threshold-dbfs", type=float, default=-30.0, help="De-esser threshold in dBFS.")
        sp.add_argument("--de-ess-ratio", type=float, default=3.0, help="De-esser compression ratio.")
        sp.add_argument("--de-ess-attack-ms", type=float, default=5.0, help="De-esser attack (ms).")
        sp.add_argument("--de-ess-release-ms", type=float, default=90.0, help="De-esser release (ms).")
        sp.add_argument("--de-ess-strength", type=float, default=0.70, help="De-esser strength from 0..1.")

    match = sub.add_parser("match", help="Build curve from desired+target WAV and apply to target WAV.")
    match.add_argument("--desired-wav", type=Path, required=True)
    match.add_argument("--target-wav", type=Path, required=True)
    match.add_argument("--out-wav", type=Path, required=True, help="Processed WAV output.")
    match.add_argument("--n-fft", type=int, default=4096)
    match.add_argument("--hop", type=int, default=1024)
    match.add_argument("--mix", type=float, default=1.0, help="Dry/wet mix, 0..1")
    add_common_curve_args(match)
    add_dynamics_args(match)

    curve = sub.add_parser("curve", help="Build curve from two exported spectrum text files.")
    curve.add_argument("--desired-spectrum", type=Path, required=True)
    curve.add_argument("--target-spectrum", type=Path, required=True)
    add_common_curve_args(curve)

    apply_p = sub.add_parser("apply", help="Apply saved curve CSV to target WAV.")
    apply_p.add_argument("--curve-csv", type=Path, required=True)
    apply_p.add_argument("--target-wav", type=Path, required=True)
    apply_p.add_argument("--out-wav", type=Path, required=True)
    apply_p.add_argument("--n-fft", type=int, default=4096)
    apply_p.add_argument("--hop", type=int, default=1024)
    apply_p.add_argument("--mix", type=float, default=1.0, help="Dry/wet mix, 0..1")
    add_whine_args(apply_p)
    add_dynamics_args(apply_p)

    whine = sub.add_parser("whine", help="Apply only electrical whine reduction to a target WAV.")
    whine.add_argument("--target-wav", type=Path, required=True)
    whine.add_argument("--out-wav", type=Path, required=True)
    whine.add_argument("--n-fft", type=int, default=4096)
    whine.add_argument("--hop", type=int, default=1024)
    whine.add_argument("--mix", type=float, default=1.0, help="Dry/wet mix, 0..1")
    add_whine_args(whine)

    fix = sub.add_parser("fix", help="Apply whine reduction, auto-level, and optional de-esser to a target WAV.")
    fix.add_argument("--target-wav", type=Path, required=True)
    fix.add_argument("--out-wav", type=Path, required=True)
    fix.add_argument("--n-fft", type=int, default=4096)
    fix.add_argument("--hop", type=int, default=1024)
    fix.add_argument("--mix", type=float, default=1.0, help="Whine reduction dry/wet mix, 0..1")
    add_whine_args(fix)
    add_dynamics_args(fix)

    pipeline = sub.add_parser("pipeline", help="Apply selected audio processing steps in launcher order.")
    pipeline.add_argument("--target-wav", type=Path, required=True)
    pipeline.add_argument("--out-wav", type=Path, required=True)
    pipeline.add_argument("--n-fft", type=int, default=4096)
    pipeline.add_argument("--hop", type=int, default=1024)
    pipeline.add_argument("--mix", type=float, default=1.0, help="Dry/wet mix for EQ/whine steps, 0..1")
    add_dynamics_args(pipeline)
    add_whine_args(pipeline)
    pipeline.add_argument("--spectrum-reference-wav", type=Path, help="Optional desired/reference WAV for spectrum matching.")
    pipeline.add_argument("--spectrum-curve-csv", type=Path, help="Optional saved curve CSV to apply.")
    pipeline.add_argument("--out-curve-csv", type=Path, help="Optional path for the generated spectrum curve CSV.")
    pipeline.add_argument("--audacity-preset", type=Path, help="Optional Audacity Filter Curve EQ preset text output.")
    pipeline.add_argument("--audacity-points", type=int, default=180, help="Audacity preset point count (max 200).")
    pipeline.add_argument("--audacity-filter-length", type=int, default=8191, help="FilterLength field for Audacity preset.")
    pipeline.add_argument("--smooth-octaves", type=float, default=0.20, help="Log-frequency smoothing width in octaves.")
    pipeline.add_argument("--min-freq", type=float, default=60.0, help="Only apply matching from this frequency up.")
    pipeline.add_argument("--max-freq", type=float, default=16000.0, help="Only apply matching up to this frequency.")
    pipeline.add_argument("--max-cut-db", type=float, default=-12.0, help="Maximum attenuation clamp.")
    pipeline.add_argument("--max-boost-db", type=float, default=12.0, help="Maximum boost clamp.")
    pipeline.add_argument(
        "--voice-clarity",
        choices=("off", "gentle", "clear"),
        default="off",
        help="Optional voice clarity EQ preset applied after de-essing.",
    )
    pipeline.add_argument(
        "--second-step-eq",
        action="store_true",
        help="Apply the optional second-step speech EQ after Voice Clarity.",
    )
    pipeline.add_argument("--peak-normalize", action="store_true", help="Normalize whole-file peak to target dBFS.")
    pipeline.add_argument("--peak-normalize-dbfs", type=float, default=-6.0, help="Peak normalize target in dBFS.")
    pipeline.add_argument("--peak-ceiling", action="store_true", help="Limit final peak to ceiling dBFS.")
    pipeline.add_argument("--peak-ceiling-dbfs", type=float, default=-6.0, help="Peak ceiling in dBFS.")

    return p


def maybe_write_audacity_preset(
    path: Path | None,
    curve: Curve,
    points: int,
    min_freq: float,
    max_freq: float,
    filter_length: int,
) -> None:
    if path is None:
        return
    txt = audacity_filter_curve_text(
        curve=curve,
        num_points=points,
        min_freq=min_freq,
        max_freq=max_freq,
        filter_length=filter_length,
    )
    save_text(path, txt)


def main() -> int:
    parser = build_common_parser()
    args = parser.parse_args()

    if args.command == "match":
        curve = build_delta_curve_from_audio(
            desired_wav=args.desired_wav,
            target_wav=args.target_wav,
            n_fft=args.n_fft,
            hop=args.hop,
            smooth_octaves=args.smooth_octaves,
            min_freq=args.min_freq,
            max_freq=args.max_freq,
            max_cut_db=args.max_cut_db,
            max_boost_db=args.max_boost_db,
        )
        curve = add_whine_reduction_to_curve(
            curve=curve,
            high_cut_enabled=args.whine_high_cut,
            high_cut_freq_hz=args.whine_high_cut_freq_hz,
            high_cut_gain_db=args.whine_high_cut_gain_db,
            high_cut_q=args.whine_high_cut_q,
            notch_freqs_hz=args.whine_notch_hz[:3],
            notch_gain_db=args.whine_notch_gain_db,
            notch_q=args.whine_notch_q,
        )
        save_curve_csv(args.curve_csv, curve)
        apply_curve_to_wav(
            target_wav=args.target_wav,
            out_wav=args.out_wav,
            curve=curve,
            n_fft=args.n_fft,
            hop=args.hop,
            mix=args.mix,
        )
        maybe_write_audacity_preset(
            path=args.audacity_preset,
            curve=curve,
            points=args.audacity_points,
            min_freq=args.min_freq,
            max_freq=args.max_freq,
            filter_length=args.audacity_filter_length,
        )
        post_paths = [args.out_wav]
        if args.de_ess:
            for p in post_paths:
                msg = apply_deesser_to_wav(
                    processed_wav=p,
                    low_hz=args.de_ess_low_hz,
                    high_hz=args.de_ess_high_hz,
                    threshold_dbfs=args.de_ess_threshold_dbfs,
                    ratio=args.de_ess_ratio,
                    attack_ms=args.de_ess_attack_ms,
                    release_ms=args.de_ess_release_ms,
                    strength=args.de_ess_strength,
                )
                print(f"{msg} file={p}")
        if args.auto_level:
            for p in post_paths:
                msg = apply_auto_level_to_wav(
                    processed_wav=p,
                    target_dbfs=args.target_dbfs,
                    floor_dbfs=args.level_floor_dbfs,
                    max_boost_db=args.max_auto_boost_db,
                    max_cut_db=args.max_auto_cut_db,
                    auto_attack_ms=args.auto_attack_ms,
                    auto_release_ms=args.auto_release_ms,
                    compressor_threshold_dbfs=args.compressor_threshold_dbfs,
                    compressor_ratio=args.compressor_ratio,
                    compressor_attack_ms=args.compressor_attack_ms,
                    compressor_release_ms=args.compressor_release_ms,
                    limiter_ceiling_dbfs=args.limiter_ceiling_dbfs,
                    strength=args.dynamics_strength,
                )
                print(f"{msg} file={p}")
        return 0

    if args.command == "curve":
        curve = build_delta_curve_from_spectra(
            desired_spectrum=args.desired_spectrum,
            target_spectrum=args.target_spectrum,
            smooth_octaves=args.smooth_octaves,
            min_freq=args.min_freq,
            max_freq=args.max_freq,
            max_cut_db=args.max_cut_db,
            max_boost_db=args.max_boost_db,
        )
        curve = add_whine_reduction_to_curve(
            curve=curve,
            high_cut_enabled=args.whine_high_cut,
            high_cut_freq_hz=args.whine_high_cut_freq_hz,
            high_cut_gain_db=args.whine_high_cut_gain_db,
            high_cut_q=args.whine_high_cut_q,
            notch_freqs_hz=args.whine_notch_hz[:3],
            notch_gain_db=args.whine_notch_gain_db,
            notch_q=args.whine_notch_q,
        )
        save_curve_csv(args.curve_csv, curve)
        maybe_write_audacity_preset(
            path=args.audacity_preset,
            curve=curve,
            points=args.audacity_points,
            min_freq=args.min_freq,
            max_freq=args.max_freq,
            filter_length=args.audacity_filter_length,
        )
        return 0

    if args.command == "apply":
        curve = load_curve_csv(args.curve_csv)
        curve = add_whine_reduction_to_curve(
            curve=curve,
            high_cut_enabled=args.whine_high_cut,
            high_cut_freq_hz=args.whine_high_cut_freq_hz,
            high_cut_gain_db=args.whine_high_cut_gain_db,
            high_cut_q=args.whine_high_cut_q,
            notch_freqs_hz=args.whine_notch_hz[:3],
            notch_gain_db=args.whine_notch_gain_db,
            notch_q=args.whine_notch_q,
        )
        apply_curve_to_wav(
            target_wav=args.target_wav,
            out_wav=args.out_wav,
            curve=curve,
            n_fft=args.n_fft,
            hop=args.hop,
            mix=args.mix,
        )
        if args.de_ess:
            msg = apply_deesser_to_wav(
                processed_wav=args.out_wav,
                low_hz=args.de_ess_low_hz,
                high_hz=args.de_ess_high_hz,
                threshold_dbfs=args.de_ess_threshold_dbfs,
                ratio=args.de_ess_ratio,
                attack_ms=args.de_ess_attack_ms,
                release_ms=args.de_ess_release_ms,
                strength=args.de_ess_strength,
            )
            print(msg)
        if args.auto_level:
            msg = apply_auto_level_to_wav(
                processed_wav=args.out_wav,
                target_dbfs=args.target_dbfs,
                floor_dbfs=args.level_floor_dbfs,
                max_boost_db=args.max_auto_boost_db,
                max_cut_db=args.max_auto_cut_db,
                auto_attack_ms=args.auto_attack_ms,
                auto_release_ms=args.auto_release_ms,
                compressor_threshold_dbfs=args.compressor_threshold_dbfs,
                compressor_ratio=args.compressor_ratio,
                compressor_attack_ms=args.compressor_attack_ms,
                compressor_release_ms=args.compressor_release_ms,
                limiter_ceiling_dbfs=args.limiter_ceiling_dbfs,
                strength=args.dynamics_strength,
            )
            print(msg)
        return 0

    if args.command == "whine":
        curve = add_whine_reduction_to_curve(
            curve=build_flat_curve(),
            high_cut_enabled=args.whine_high_cut,
            high_cut_freq_hz=args.whine_high_cut_freq_hz,
            high_cut_gain_db=args.whine_high_cut_gain_db,
            high_cut_q=args.whine_high_cut_q,
            notch_freqs_hz=args.whine_notch_hz[:3],
            notch_gain_db=args.whine_notch_gain_db,
            notch_q=args.whine_notch_q,
        )
        apply_curve_to_wav(
            target_wav=args.target_wav,
            out_wav=args.out_wav,
            curve=curve,
            n_fft=args.n_fft,
            hop=args.hop,
            mix=args.mix,
        )
        return 0

    if args.command == "fix":
        sr, x = read_wav(args.target_wav)
        write_wav(args.out_wav, sr, x)
        if args.whine_high_cut or args.whine_notch_hz:
            curve = add_whine_reduction_to_curve(
                curve=build_flat_curve(),
                high_cut_enabled=args.whine_high_cut,
                high_cut_freq_hz=args.whine_high_cut_freq_hz,
                high_cut_gain_db=args.whine_high_cut_gain_db,
                high_cut_q=args.whine_high_cut_q,
                notch_freqs_hz=args.whine_notch_hz[:3],
                notch_gain_db=args.whine_notch_gain_db,
                notch_q=args.whine_notch_q,
            )
            apply_curve_to_wav(
                target_wav=args.out_wav,
                out_wav=args.out_wav,
                curve=curve,
                n_fft=args.n_fft,
                hop=args.hop,
                mix=args.mix,
            )
            print("[whine] applied.")
        if args.de_ess:
            msg = apply_deesser_to_wav(
                processed_wav=args.out_wav,
                low_hz=args.de_ess_low_hz,
                high_hz=args.de_ess_high_hz,
                threshold_dbfs=args.de_ess_threshold_dbfs,
                ratio=args.de_ess_ratio,
                attack_ms=args.de_ess_attack_ms,
                release_ms=args.de_ess_release_ms,
                strength=args.de_ess_strength,
            )
            print(msg)
        if args.auto_level:
            msg = apply_auto_level_to_wav(
                processed_wav=args.out_wav,
                target_dbfs=args.target_dbfs,
                floor_dbfs=args.level_floor_dbfs,
                max_boost_db=args.max_auto_boost_db,
                max_cut_db=args.max_auto_cut_db,
                auto_attack_ms=args.auto_attack_ms,
                auto_release_ms=args.auto_release_ms,
                compressor_threshold_dbfs=args.compressor_threshold_dbfs,
                compressor_ratio=args.compressor_ratio,
                compressor_attack_ms=args.compressor_attack_ms,
                compressor_release_ms=args.compressor_release_ms,
                limiter_ceiling_dbfs=args.limiter_ceiling_dbfs,
                strength=args.dynamics_strength,
            )
            print(msg)
        return 0

    if args.command == "pipeline":
        sr, x = read_wav(args.target_wav)
        write_wav(args.out_wav, sr, x)

        if args.auto_level:
            msg = apply_auto_level_to_wav(
                processed_wav=args.out_wav,
                target_dbfs=args.target_dbfs,
                floor_dbfs=args.level_floor_dbfs,
                max_boost_db=args.max_auto_boost_db,
                max_cut_db=args.max_auto_cut_db,
                auto_attack_ms=args.auto_attack_ms,
                auto_release_ms=args.auto_release_ms,
                compressor_threshold_dbfs=args.compressor_threshold_dbfs,
                compressor_ratio=args.compressor_ratio,
                compressor_attack_ms=args.compressor_attack_ms,
                compressor_release_ms=args.compressor_release_ms,
                limiter_ceiling_dbfs=args.limiter_ceiling_dbfs,
                strength=args.dynamics_strength,
            )
            print(msg)

        if args.spectrum_reference_wav and args.spectrum_curve_csv:
            raise ValueError("Use either --spectrum-reference-wav or --spectrum-curve-csv, not both.")

        if args.spectrum_reference_wav:
            curve = build_delta_curve_from_audio(
                desired_wav=args.spectrum_reference_wav,
                target_wav=args.out_wav,
                n_fft=args.n_fft,
                hop=args.hop,
                smooth_octaves=args.smooth_octaves,
                min_freq=args.min_freq,
                max_freq=args.max_freq,
                max_cut_db=args.max_cut_db,
                max_boost_db=args.max_boost_db,
            )
            if args.out_curve_csv:
                save_curve_csv(args.out_curve_csv, curve)
            maybe_write_audacity_preset(
                path=args.audacity_preset,
                curve=curve,
                points=args.audacity_points,
                min_freq=args.min_freq,
                max_freq=args.max_freq,
                filter_length=args.audacity_filter_length,
            )
            apply_curve_to_wav(
                target_wav=args.out_wav,
                out_wav=args.out_wav,
                curve=curve,
                n_fft=args.n_fft,
                hop=args.hop,
                mix=args.mix,
            )
            print("[spectrum] matched from reference WAV.")

        if args.spectrum_curve_csv:
            curve = load_curve_csv(args.spectrum_curve_csv)
            apply_curve_to_wav(
                target_wav=args.out_wav,
                out_wav=args.out_wav,
                curve=curve,
                n_fft=args.n_fft,
                hop=args.hop,
                mix=args.mix,
            )
            print("[spectrum] applied curve CSV.")

        if args.de_ess:
            msg = apply_deesser_to_wav(
                processed_wav=args.out_wav,
                low_hz=args.de_ess_low_hz,
                high_hz=args.de_ess_high_hz,
                threshold_dbfs=args.de_ess_threshold_dbfs,
                ratio=args.de_ess_ratio,
                attack_ms=args.de_ess_attack_ms,
                release_ms=args.de_ess_release_ms,
                strength=args.de_ess_strength,
            )
            print(msg)

        if args.voice_clarity != "off":
            curve = build_voice_clarity_curve(args.voice_clarity)
            apply_curve_to_wav(
                target_wav=args.out_wav,
                out_wav=args.out_wav,
                curve=curve,
                n_fft=args.n_fft,
                hop=args.hop,
                mix=args.mix,
            )
            print(f"[voice-clarity] applied: {args.voice_clarity}.")

        if args.second_step_eq:
            curve = build_second_step_eq_curve()
            apply_curve_to_wav(
                target_wav=args.out_wav,
                out_wav=args.out_wav,
                curve=curve,
                n_fft=args.n_fft,
                hop=args.hop,
                mix=args.mix,
            )
            print("[second-step-eq] applied.")

        if args.peak_normalize:
            msg = apply_peak_normalize_to_wav(
                processed_wav=args.out_wav,
                target_dbfs=args.peak_normalize_dbfs,
            )
            print(msg)

        if args.peak_ceiling:
            msg = apply_peak_ceiling_to_wav(
                processed_wav=args.out_wav,
                ceiling_dbfs=args.peak_ceiling_dbfs,
            )
            print(msg)

        if args.whine_high_cut or args.whine_notch_hz:
            curve = add_whine_reduction_to_curve(
                curve=build_flat_curve(),
                high_cut_enabled=args.whine_high_cut,
                high_cut_freq_hz=args.whine_high_cut_freq_hz,
                high_cut_gain_db=args.whine_high_cut_gain_db,
                high_cut_q=args.whine_high_cut_q,
                notch_freqs_hz=args.whine_notch_hz[:3],
                notch_gain_db=args.whine_notch_gain_db,
                notch_q=args.whine_notch_q,
            )
            apply_curve_to_wav(
                target_wav=args.out_wav,
                out_wav=args.out_wav,
                curve=curve,
                n_fft=args.n_fft,
                hop=args.hop,
                mix=args.mix,
            )
            print("[whine] applied.")

        return 0

    raise RuntimeError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
