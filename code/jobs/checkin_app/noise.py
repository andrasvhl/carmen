"""Talk's noise gate (Nov 9): street noise must never become a turn.

owner decision, Nov 9, running with AirPods: "Listening always jumps on, although I didn't say anything.
And it's interrupting you." The phone's detector is energy-only, so traffic and wind reached the
server as clips, and Whisper turned them into words ("so basically", "Ich bin ein Sprech"). Two
checks run on every clip of the iPhone app's Talk before it can become a message:

  1. Whisper's own confidence (verbose_json): a clip is noise when every segment has
     no_speech_prob over NO_SPEECH and avg_logprob under LOGPROB, the rule Whisper itself uses
     to skip silence.
  2. Silero VAD (onnx, CPU) on the WAV: a clip with under MIN_SPEECH_MS of speech is noise.

Short real answers ("ja", "yes", "ok") are kept by a looser limit right after Claude asked a
question or while an approval waits for a yes or no. A dropped clip is never a turn: the app
listens again without a word. The design: the task "Fix Carmen Talk bugs" § Design.

  CARMEN_VAD_MODEL   path of silero_vad.onnx (default STATE_DIR/models/silero_vad.onnx).
                     Missing model or onnxruntime: the VAD check is skipped, Whisper's still runs.
"""
from __future__ import annotations

import io
import os
import wave
from pathlib import Path

from runner import config
from runner.job import log

NO_SPEECH = 0.6
NO_SPEECH_LOOSE = 0.8
LOGPROB = -1.0
MIN_SPEECH_MS = 300
MIN_SPEECH_MS_LOOSE = 120
VAD_THRESHOLD = 0.5
SR = 16000
FRAME = 512             # Silero v5 at 16 kHz: 32 ms frames
CONTEXT = 64            # and the last 64 samples of the frame before


def model_path() -> Path:
    return Path(os.environ.get("CARMEN_VAD_MODEL") or config.STATE_DIR / "models" / "silero_vad.onnx")


_session: dict = {}


def _vad():
    """The onnx session, loaded once; None when the model or onnxruntime is missing."""
    if "s" not in _session:
        try:
            import onnxruntime as ort
            opts = ort.SessionOptions()
            opts.intra_op_num_threads = opts.inter_op_num_threads = 1
            _session["s"] = ort.InferenceSession(str(model_path()), sess_options=opts,
                                                 providers=["CPUExecutionProvider"])
        except Exception as exc:
            log(f"checkin_app noise: no VAD ({type(exc).__name__}: {str(exc)[:120]}), Whisper check only")
            _session["s"] = None
    return _session["s"]


def pcm16k(wav: bytes):
    """A WAV's samples as float32 mono at 16 kHz, or None when it is not a 16-bit PCM WAV."""
    import numpy as np
    try:
        with wave.open(io.BytesIO(wav)) as w:
            ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
            raw = w.readframes(n)
    except (wave.Error, EOFError, OSError):
        return None
    if width != 2 or not rate:
        return None
    x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    if ch > 1:
        x = x[::ch]
    if rate != SR:
        x = np.interp(np.arange(0, len(x), rate / SR), np.arange(len(x)), x).astype(np.float32)
    return x


def speech_ms(wav: bytes) -> int | None:
    """Milliseconds of speech Silero hears in the clip; None when it cannot judge (no model, not a WAV)."""
    s = _vad()
    if s is None:
        return None
    x = pcm16k(wav)
    if x is None:
        return None
    import numpy as np
    state = np.zeros((2, 1, 128), dtype=np.float32)
    ctx = np.zeros(CONTEXT, dtype=np.float32)
    sr = np.array(SR, dtype=np.int64)
    frames = 0
    for i in range(0, len(x) - FRAME + 1, FRAME):
        chunk = x[i:i + FRAME]
        out, state = s.run(None, {"input": np.concatenate([ctx, chunk])[None, :], "state": state, "sr": sr})
        ctx = chunk[-CONTEXT:]
        if float(out[0][0]) >= VAD_THRESHOLD:
            frames += 1
    return frames * FRAME * 1000 // SR


def whisper_noise(segments: list[dict] | None, loose: bool = False) -> bool:
    """True when Whisper itself says no one spoke: every segment unlikely speech and low confidence."""
    if not segments:
        return False
    limit = NO_SPEECH_LOOSE if loose else NO_SPEECH
    try:
        return all(float(g.get("no_speech_prob", 0)) > limit and float(g.get("avg_logprob", 0)) < LOGPROB
                   for g in segments)
    except (TypeError, ValueError):
        return False


def asked(st: dict) -> bool:
    """Claude just asked something (its last message ends in a question), or an approval waits."""
    if st.get("approval"):
        return True
    for m in reversed(st.get("messages") or []):
        if m.get("role") == "you":
            return False
        if m.get("role") == "claude" and not m.get("hidden"):
            t = (m.get("text") or "").strip()
            from .siri import split_end
            return split_end(t)[0].rstrip(" *_)\"'»“”").endswith("?")
    return False


def drop_reason(segments: list[dict] | None, speech: int | None, loose: bool) -> str:
    """Why a clip is noise ("" when it is speech)."""
    if whisper_noise(segments, loose):
        return "whisper_no_speech"
    if speech is not None and speech < (MIN_SPEECH_MS_LOOSE if loose else MIN_SPEECH_MS):
        return f"vad_{speech}ms"
    return ""
