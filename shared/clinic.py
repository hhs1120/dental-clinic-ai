"""앱이 쓰는 기준 날짜 (앱 설계 §6). 원본은 config/generator.yaml — 앱은 생성기를 import하지 않으므로 값만 여기서 읽는다."""

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "generator.yaml"


@dataclass(frozen=True)
class Clinic:
    today: date  # 기준일 TODAY — 그날 개원 전 스냅샷
    range_start: date  # 신환 판정 기준 (가상 데이터 생성 설계 §2.2)
    eval_pool_size: int  # 판정 풀 크기 — 정렬 앞 절반이 dev (앱 설계 §6)


def load_clinic(path: Path = CONFIG_PATH) -> Clinic:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Clinic(cfg["today"], cfg["range"]["start"], cfg["eval_pool_size"])
