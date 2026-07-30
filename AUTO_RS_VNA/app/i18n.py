from __future__ import annotations

import json
from pathlib import Path


class Translator:
    def __init__(self, base_dir: Path, language: str = "pl") -> None:
        self.base_dir = base_dir
        self.language = language
        self.words: dict[str, str] = {}
        self.load(language)

    def load(self, language: str) -> None:
        path = self.base_dir / "languages" / f"{language}.json"
        if not path.exists():
            path = self.base_dir / "languages" / "pl.json"
            language = "pl"
        self.words = json.loads(path.read_text(encoding="utf-8"))
        self.language = language

    def __call__(self, key: str, fallback: str | None = None) -> str:
        return self.words.get(key, fallback if fallback is not None else key)
