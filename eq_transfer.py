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
from typing import Iterable, Tuple

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


def estimate_reverb_metrics(mono_audio: np.ndarray, sr: int) -> ReverbMetrics:
    x = mono_audio.astype(np.float32, copy=False)
    peak = float(np.max(np.abs(x))) if len(x) else 0.0
    if peak > EPS:
        x = x / peak

    env, hop_s = _rms_envelope(x, sr=sr, frame_ms=20.0, hop_ms=5.0)
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

    desired = estimate_reverb_metrics(desired_mono, sr=sr)
    target = estimate_reverb_metrics(target_mono, sr=sr)

    rt_gap = desired.rt60_s - target.rt60_s
    ratio_gap = desired.tail_direct_ratio - target.tail_direct_ratio

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
        rt60 = float(np.clip(desired.rt60_s, 0.15, 3.0))
        return ReverbTransfer(
            action="add",
            rt60_s=rt60,
            wet_mix=wet,
            dereverb_amount=0.0,
            reason=(
                f"[reverb] add: wet={wet:.2f}, rt60={rt60:.2f}s "
                f"(desired rt60={desired.rt60_s:.2f}s, target rt60={target.rt60_s:.2f}s)."
            ),
        )

    if want_remove:
        rt_diff = max(0.0, target.rt60_s - desired.rt60_s)
        ratio_diff = max(0.0, target.tail_direct_ratio - desired.tail_direct_ratio)
        amount = 0.16 + ratio_diff * 0.28 + rt_diff * 0.18
        amount = float(np.clip(amount * strength, 0.10, 0.80))
        return ReverbTransfer(
            action="remove",
            rt60_s=desired.rt60_s,
            wet_mix=0.0,
            dereverb_amount=amount,
            reason=(
                f"[reverb] remove: amount={amount:.2f} "
                f"(desired rt60={desired.rt60_s:.2f}s, target rt60={target.rt60_s:.2f}s)."
            ),
        )

    return ReverbTransfer(
        action="none",
        rt60_s=desired.rt60_s,
        wet_mix=0.0,
        dereverb_amount=0.0,
        reason=(
            f"[reverb] skipped: no clear reverb mismatch "
            f"(desired rt60={desired.rt60_s:.2f}s, target rt60={target.rt60_s:.2f}s)."
        ),
    )


def build_synthetic_ir(sr: int, rt60_s: float, pre_delay_ms: float = 18.0) -> np.ndarray:
    pre = max(0, int(sr * (pre_delay_ms / 1000.0)))
    tail_n = int(np.clip(sr * max(rt60_s, 0.08) * 1.2, sr * 0.10, sr * 3.0))
    n = pre + tail_n

    rng = np.random.default_rng(0)
    noise = rng.standard_normal(tail_n).astype(np.float32)
    t = np.arange(tail_n, dtype=np.float32) / float(sr)
    decay = np.exp(-6.907755 * t / max(rt60_s, 0.08)).astype(np.float32)
    tail = noise * decay

    alpha = float(np.exp(-2.0 * np.pi * 6000.0 / float(sr)))
    lp = np.zeros_like(tail)
    prev = 0.0
    for i, v in enumerate(tail):
        prev = (1.0 - alpha) * float(v) + alpha * prev
        lp[i] = prev

    ir = np.zeros(n, dtype=np.float32)
    ir[pre:] = lp
    for delay_ms, gain in ((7.0, 0.35), (13.0, 0.24), (21.0, 0.16), (34.0, 0.10)):
        idx = min(n - 1, pre + int(sr * (delay_ms / 1000.0)))
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


def apply_synthetic_reverb(audio: np.ndarray, sr: int, rt60_s: float, wet_mix: float) -> np.ndarray:
    wet_mix = float(np.clip(wet_mix, 0.0, 1.0))
    if wet_mix <= 0.0:
        return audio

    ir = build_synthetic_ir(sr=sr, rt60_s=rt60_s)
    y = np.zeros_like(audio, dtype=np.float32)

    for ch in range(audio.shape[1]):
        wet = fftconvolve(audio[:, ch], ir, mode="full")[: audio.shape[0]].astype(np.float32)
        # Keep wet signal out of muddy lows and harsh highs.
        wet = _one_pole_highpass(wet, sr=sr, cutoff_hz=170.0)
        wet = _one_pole_lowpass(wet, sr=sr, cutoff_hz=7000.0)
        y[:, ch] = ((1.0 - wet_mix) * audio[:, ch]) + (wet_mix * wet)

    peak = float(np.max(np.abs(y))) if y.size else 0.0
    if peak > 1.0:
        y /= peak * 1.01
    return y


def reduce_late_reverb(audio: np.ndarray, sr: int, amount: float, n_fft: int = 2048, hop: int = 512) -> np.ndarray:
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


def apply_reverb_match_to_wav(
    desired_wav: Path,
    target_wav: Path,
    processed_wav: Path,
    strength: float,
    mode: str,
) -> ReverbTransfer:
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
    if transfer.action == "none":
        return transfer

    if transfer.action == "add":
        y = apply_synthetic_reverb(out_audio, sr=out_sr, rt60_s=transfer.rt60_s, wet_mix=transfer.wet_mix)
    else:
        y = reduce_late_reverb(out_audio, sr=out_sr, amount=transfer.dereverb_amount)
    write_wav(processed_wav, out_sr, y)
    return transfer


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
    match.add_argument(
        "--match-reverb",
        action="store_true",
        help="Try to match room/reverb feel from desired recording (heuristic).",
    )
    match.add_argument(
        "--reverb-mode",
        choices=["auto", "add", "remove"],
        default="auto",
        help="Reverb strategy when --match-reverb is enabled (default: auto).",
    )
    match.add_argument(
        "--reverb-strength",
        type=float,
        default=1.0,
        help="Strength multiplier for reverb processing (0..2, default 1).",
    )
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
    add_dynamics_args(apply_p)

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
        if args.match_reverb:
            transfer = apply_reverb_match_to_wav(
                desired_wav=args.desired_wav,
                target_wav=args.target_wav,
                processed_wav=args.out_wav,
                strength=args.reverb_strength,
                mode=args.reverb_mode,
            )
            print(transfer.reason)
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

    raise RuntimeError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
