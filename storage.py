import json
from pathlib import Path
from typing import Optional

_DB_FILE = Path(__file__).parent / "data" / "fingerprints.json"


def _load() -> dict:
    if not _DB_FILE.exists():
        return {}
    with open(_DB_FILE) as f:
        return json.load(f)


def _save(db: dict) -> None:
    _DB_FILE.parent.mkdir(exist_ok=True)
    with open(_DB_FILE, "w") as f:
        json.dump(db, f, indent=2)


def save_name(finger_id: int, name: str) -> None:
    db = _load()
    db[str(finger_id)] = name
    _save(db)


def get_name(finger_id: int) -> str:
    return _load().get(str(finger_id), f"ID #{finger_id}")


def delete_name(finger_id: int) -> None:
    db = _load()
    db.pop(str(finger_id), None)
    _save(db)


def list_all() -> dict:
    """Returns {finger_id (int): name (str)}."""
    return {int(k): v for k, v in _load().items()}


def next_available_id(used_ids: list) -> int:
    used = set(used_ids)
    for i in range(1, 128):
        if i not in used:
            return i
    raise RuntimeError("Sensor memory full — 127 fingerprints already stored.")
