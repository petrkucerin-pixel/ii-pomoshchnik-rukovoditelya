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
            segments, _ = self._load().transcribe(io.BytesIO(audio), language="ru", beam_size=1, vad_filter=True)
            return " ".join(s.text.strip() for s in segments).strip()


def available() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except ImportError:
        return False
