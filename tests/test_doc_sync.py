"""문서 §5.3 '분포 목표' 표·코드 이름 참조가 코드와 맞는지 — 설계 목표는 문서에서 읽히게 두고, 어긋나면 여기서 잡는다."""
import re
from pathlib import Path

import pytest
import yaml

from generator import master, verify
from generator.master import VISIT_MIX

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "치과 AX - 가상 데이터 생성 설계.md"
CFG = yaml.safe_load((ROOT / "config" / "generator.yaml").read_text(encoding="utf-8"))

# 문서가 이름으로 가리키지만 아직 구현 전인 값 (해당 단계 구현 때 코드에 추가)
PENDING = {"ADJUST_CODES", "CHART_DECOY_RATIO"}

_MIX_LABEL = {"treatment": "진료", "hygiene": "위생", "consult": "상담"}

# 경고로 남은 지표만(리포트 항목은 허용치가 없어 비교할 값이 없다). 지표 이름(표 첫 칸의 앞부분) → ({라벨: 목표값}, 허용치). 라벨 ""은 값이 하나인 칸. 허용치 None = 허용 칸 없음
EXPECTED = {
    "내원 구성": ({_MIX_LABEL[t]: v for t, v in VISIT_MIX.items()}, verify.VISIT_MIX_TOLERANCE),
}


def _doc() -> str:
    return DOC.read_text(encoding="utf-8")


def _target_rows() -> dict[str, list[str]]:
    """'경고' 첫 표(분포 목표)의 행 → 칸 목록"""
    text = _doc()
    table = text[text.index("**경고**("):].split("\n\n")[1]
    rows = [[c.strip() for c in line.strip("|").split("|")] for line in table.splitlines()]
    header, rows = rows[0], rows[2:]
    assert header[:3] == ["지표", "목표", "허용"], header
    return {r[0]: r for r in rows}


def _labeled_percents(cell: str) -> dict[str, float]:
    """'소아 15% · 청소년 8%' → {'소아': 0.15, '청소년': 0.08}, '70%' → {'': 0.70}"""
    return {label.strip(): float(n) / 100 for label, n in re.findall(r"([^\d·%]*?)\s*(\d+(?:\.\d+)?)%", cell)}


@pytest.mark.parametrize("name", EXPECTED)
def test_target_table_matches_code(name):
    row = next((r for key, r in _target_rows().items() if key.startswith(name)), None)
    assert row, f"§5.3 분포 목표 표에 '{name}' 행이 없음"
    targets, tolerance = EXPECTED[name]
    got = _labeled_percents(row[1])
    assert got.keys() == targets.keys(), f"{name} 라벨: 문서 {list(got)} ≠ 코드 {list(targets)}"
    for label, value in targets.items():
        assert got[label] == pytest.approx(value), f"{name} {label}: 문서 {row[1]} ≠ 코드 {value}"
    if tolerance is not None:
        assert list(_labeled_percents(row[2]).values()) == pytest.approx([tolerance]), f"{name} 허용: 문서 {row[2]}"


def test_named_constants_exist():
    """문서가 `master.X`·`verify.X`로 가리키는 이름은 코드에 실제로 있어야 한다"""
    text = _doc()
    missing = [f"{mod}.{name}" for mod, name in re.findall(r"`(master|verify)\.(\w+)`", text)
               if name not in PENDING and not hasattr({"master": master, "verify": verify}[mod], name)]
    assert not missing, missing


def test_named_config_keys_exist():
    """문서가 `config` `키`로 가리키는 키는 config/generator.yaml에 있어야 한다"""
    keys = set()
    for group in re.findall(r"`config` ((?:`[\w.]+`[·\s]*)+)", _doc()):
        keys |= set(re.findall(r"`([\w.]+)`", group))
    def exists(key):
        node = CFG
        for part in key.split("."):
            if not isinstance(node, dict) or part not in node:
                return False
            node = node[part]
        return True
    assert keys and not [k for k in keys if not exists(k)], sorted(k for k in keys if not exists(k))


# 하위 키 중 다른 뜻의 백틱 토큰(문자 trigger·예약 상태 enum, 규모 이름, 연령 구간·시술 age_group 등)과 이름이 겹쳐 단독으로는 보지 않는 것
_AMBIGUOUS_SUBKEYS = {"appt_confirm", "reminder", "noshow", "balance", "start", "end", "small", "large",
                      "child", "teen", "adult", "senior"}


def _config_key_names() -> set[str]:
    """config 최상위 키, 하위 키(`messages.balance` 형태), 이름이 겹치지 않는 하위 키 단독(`supplementary_ratio`)"""
    names = set()
    for key, value in CFG.items():
        names.add(key)
        if isinstance(value, dict):
            names |= {f"{key}.{sub}" for sub in value}
            names |= set(value) - _AMBIGUOUS_SUBKEYS
    return names


def test_config_keys_always_prefixed():
    """문서에서 config 키는 항상 `config` `키` 형태로 쓴다 — 그래야 test_named_config_keys_exist가 이름 바꿈을 잡는다.
    코드 블록(```) 안은 백틱을 쓸 수 없어 제외"""
    text = re.sub(r"```.*?```", "", _doc(), flags=re.S)
    covered = [m.span() for m in re.finditer(r"`config` ((?:`[\w.]+`[·\s]*)+)", text)]
    names = _config_key_names()
    # 백틱 안의 이름(식 `1 − confirm_rate`도 이름 단위로). 괄호가 있는 코드 예시는 config 참조가 아니라 제외
    tokens = [(m.start(), t) for m in re.finditer(r"`([^`]+)`", text) if "(" not in m.group(1)
              for t in re.findall(r"[\w.]+", m.group(1))]
    bare = sorted({t for pos, t in tokens if t in names and not any(a <= pos < b for a, b in covered)})
    assert not bare, f"`config` 접두어 없이 쓴 config 키: {bare}"


# §4.2 문서 표에서 방해 문서(정답이 없도록 만드는 문서) 종류 — 나머지는 core
_DISTRACTOR_KINDS = {"공지 옛 버전", "유사 주제 무답", "타 병원·일반"}


def test_rag_document_counts_match_config():
    """§4.2 문서 표의 종류별 수 합계가 `config` `documents`(core·distractor)와 같아야 한다"""
    text = _doc()
    table = text[text.index("### 4.2"):].split("| 종류 | 수 | 생성 |")[1].split("\n\n")[0]
    rows = [[c.strip() for c in line.strip("|").split("|")] for line in table.strip().splitlines()[1:]]
    counts = {"core": 0, "distractor": 0}
    for kind, n, *_ in rows:
        counts["distractor" if kind in _DISTRACTOR_KINDS else "core"] += int(n)
    assert counts == CFG["documents"], f"§4.2 표 {counts} ≠ config {CFG['documents']}"
