"""Распознавание голосовых руководителя (TASK_16): Whisper на нашем сервере, голос не уходит со стенда.

Модель загружается один раз, при первом голосовом, и живёт в памяти процесса (~0,5 ГБ для `small`).
Telegram присылает голос в OGG/Opus — faster-whisper читает его сам, внешний ffmpeg не нужен.
"""
from __future__ import annotations

import io
import logging
import os
import threading

log = logging.getLogger(__name__)


class LocalWhisper:
    def __init__(self, model: str | None = None, *, threads: int = 2):
        self.name = model or os.environ.get("WHISPER_MODEL", "small")
        self.threads = threads
        self._model = None
        self._lock = threading.Lock()          # одна модель на процесс; два голосовых подряд — по очереди

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.name, device="cpu", compute_type="int8", cpu_threads=self.threads)
        return self._model

    def __call__(self, audio: bytes) -> str:
        with self._lock:
            segments, _ = self._load().transcribe(decode(audio), language="ru", beam_size=1, vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()


def decode(audio: bytes, rate: int = 16000):
    """OGG/Opus, WAV и др. → моно 16 кГц float32. Свой разбор: faster-whisper 1.2 зовёт av.open с параметром,
    которого нет в av 19 (проверено на стенде 27.11), а сборки старого av для сервера нет."""
    import av
    import numpy as np
    chunks = []
    with av.open(io.BytesIO(audio), mode="r") as c:
        resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
        for frame in c.decode(audio=0):
            chunks += [f.to_ndarray() for f in resampler.resample(frame)]
        chunks += [f.to_ndarray() for f in resampler.resample(None)]
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks, axis=1).flatten().astype(np.float32) / 32768.0


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False
