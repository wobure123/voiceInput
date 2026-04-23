"""History manager — stores transcription records.

Stores each transcription as a JSON entry + optional WAV audio file.
Supports: save, load, delete, search, get page.
"""
import json
import time
import wave
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import Config
from core.log import logger

_TAG = "[History]"


@dataclass
class HistoryEntry:
    id: str
    text: str
    duration: float
    mode: str
    timestamp: float
    has_audio: bool = False

    @property
    def datetime_str(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp))

    @property
    def short_text(self) -> str:
        return self.text[:50] + ("..." if len(self.text) > 50 else "")


class HistoryManager:
    MAX_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB

    def __init__(self, config: Config):
        self._dir = Config.history_dir()
        self._enforce_size_limit()

    def save_entry(self, text: str, duration: float, mode: str,
                   audio_data: Optional[bytes] = None) -> HistoryEntry:
        entry_id = self._next_id()
        entry = HistoryEntry(
            id=entry_id,
            text=text,
            duration=round(duration, 1),
            mode=mode,
            timestamp=time.time(),
            has_audio=audio_data is not None,
        )

        meta_path = self._dir / f"{entry_id}.json"
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(asdict(entry), f, ensure_ascii=False, indent=2)

        if audio_data:
            wav_path = self._dir / f"{entry_id}.wav"
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(16000)
                wf.writeframes(audio_data)

        logger.info(f"{_TAG} Saved entry {entry_id} "
                    f"({duration:.1f}s, mode={mode}"
                    f"{', +wav' if audio_data else ''})")
        self._enforce_size_limit()
        return entry

    def get_entries(self, limit: int = 50, offset: int = 0) -> list[HistoryEntry]:
        files = sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        entries = []
        for path in files[offset:offset + limit]:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                entries.append(HistoryEntry(**data))
            except Exception:
                continue
        return entries

    def get_entry(self, entry_id: str) -> Optional[HistoryEntry]:
        path = self._dir / f"{entry_id}.json"
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return HistoryEntry(**json.load(f))
        except Exception:
            return None

    def delete_entry(self, entry_id: str):
        for suffix in (".json", ".wav"):
            path = self._dir / f"{entry_id}{suffix}"
            if path.exists():
                path.unlink()

    def delete_all(self):
        for path in self._dir.glob("*"):
            path.unlink()

    def search(self, query: str, limit: int = 50) -> list[HistoryEntry]:
        q = query.lower()
        results = []
        for entry in self.get_entries(limit=500):
            if q in entry.text.lower():
                results.append(entry)
                if len(results) >= limit:
                    break
        return results

    def total_count(self) -> int:
        return len(list(self._dir.glob("*.json")))

    def folder_size_kb(self) -> int:
        total = sum(f.stat().st_size for f in self._dir.rglob("*") if f.is_file())
        return total // 1024

    def _next_id(self) -> str:
        base = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        if not (self._dir / f"{base}.json").exists():
            return base
        for seq in range(2, 100):
            candidate = f"{base}_{seq}"
            if not (self._dir / f"{candidate}.json").exists():
                return candidate
        return f"{base}_{int(time.time() * 1000) % 10000}"

    def _enforce_size_limit(self):
        """Delete oldest entries until total size is under MAX_SIZE_BYTES."""
        files = sorted(self._dir.glob("*.json"), key=lambda p: p.stat().st_mtime)
        total = sum(f.stat().st_size for f in self._dir.rglob("*") if f.is_file())
        deleted = 0
        while total > self.MAX_SIZE_BYTES and files:
            oldest = files.pop(0)
            entry_id = oldest.stem
            entry_size = oldest.stat().st_size
            oldest.unlink()
            wav = self._dir / f"{entry_id}.wav"
            if wav.exists():
                entry_size += wav.stat().st_size
                wav.unlink()
            total -= entry_size
            deleted += 1
        if deleted:
            logger.info(f"{_TAG} Size limit: deleted {deleted} old entries, "
                        f"now {total // 1024} KB")
