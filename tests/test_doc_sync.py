"""문서 §5.3 '분포 목표' 표의 숫자가 코드 값과 같은지 — 설계 목표는 문서에서 읽히게 두고, 어긋나면 여기서 잡는다."""
import re
from pathlib import Path

import pytest
import yaml

from generator import verify
from generator.master import ACTIVE_SHARE, AGE_BANDS, NEW_PATIENT_SHARE, VISIT_MIX

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "치과 AX - 가상 데이터 생성 설계.md"
CFG = yaml.safe_load((ROOT / "config" / "generator.yaml").read_text(encoding="utf-8"))

# 지표 이름(표 첫 칸의 앞부분) → (목표값들, 허용치). 허용치 None = 표에 허용 칸 없음
EXPECTED = {
    "연령 구간 비중": ([share for *_, share in AGE_BANDS], verify.SHARE_TOLERANCE),
    "활동": ([ACTIVE_SHARE], verify.ACTIVE_TOLERANCE),
    "신환": ([NEW_PATIENT_SHARE], verify.NEW_PATIENT_TOLERANCE),
    "노쇼·당일 취소": ([CFG["noshow_rate"], CFG["sameday_cancel_rate"]], verify.SHARE_TOLERANCE),
    "변경": ([CFG["reschedule_rate"]], verify.SHARE_TOLERANCE),
    "미확인": ([1 - CFG["confirm_rate"]], verify.SHARE_TOLERANCE),
    "미수 발생": ([CFG["unpaid_rate"]], verify.SHARE_TOLERANCE),
    "내원 구성": (list(VISIT_MIX.values()), verify.VISIT_MIX_TOLERANCE),
    "하루 채움": ([verify.DAILY_MIN_FILL], None),
}


def _target_rows() -> dict[str, list[str]]:
    """'경고 — 분포 목표' 표의 행 → 칸 목록"""
    text = DOC.read_text(encoding="utf-8")
    table = text[text.index("**경고 — 분포 목표**"):].split("\n\n")[1]
    rows = [[c.strip() for c in line.strip("|").split("|")] for line in table.splitlines()]
    header, rows = rows[0], rows[2:]
    assert header[:3] == ["지표", "목표", "허용"], header
    return {r[0]: r for r in rows}


def _percents(cell: str) -> list[float]:
    return [float(n) / 100 for n in re.findall(r"\d+(?:\.\d+)?", cell)]


@pytest.mark.parametrize("name", EXPECTED)
def test_target_table_matches_code(name):
    rows = _target_rows()
    row = next((r for key, r in rows.items() if key.startswith(name)), None)
    assert row, f"§5.3 분포 목표 표에 '{name}' 행이 없음"
    targets, tolerance = EXPECTED[name]
    assert _percents(row[1]) == pytest.approx(targets), f"{name} 목표: 문서 {row[1]} ≠ 코드 {targets}"
    if tolerance is not None:
        assert _percents(row[2]) == pytest.approx([tolerance]), f"{name} 허용: 문서 {row[2]} ≠ 코드 {tolerance}"
