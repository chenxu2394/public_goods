from __future__ import annotations

import hashlib
from typing import List, Tuple

from ..config import DEMO_PROFILES


def _stable_int(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:12], 16)


def _demo_profile_for_student(student_public_id: str) -> str:
    digits = "".join(ch for ch in student_public_id if ch.isdigit())
    index = int(digits) if digits else _stable_int(student_public_id)
    return DEMO_PROFILES[index % len(DEMO_PROFILES)]


def _demo_student_rows(student_count: int) -> List[Tuple[str, str]]:
    return [(f"DEMO{i:03d}", f"Demo Student {i:02d}") for i in range(1, student_count + 1)]
