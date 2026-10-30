"""문서 §5.3 '분포 목표' 표·코드 이름 참조가 코드와 맞는지 — 설계 목표는 문서에서 읽히게 두고, 어긋나면 여기서 잡는다."""

import re
from pathlib import Path

import pytest
import yaml

from evaluation import metrics
from generator import calls, chart, documents, finance, llm_jobs, master, transcripts, verify
from generator.master import VISIT_MIX
from shared import llm

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "치과 AX - 가상 데이터 생성 설계.md"
CFG = yaml.safe_load((ROOT / "config" / "generator.yaml").read_text(encoding="utf-8"))

# 문서가 이름으로 가리키지만 아직 구현 전인 값 (해당 단계 구현 때 코드에 추가)
PENDING: set[str] = set()

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
    table = text[text.index("**경고**(") :].split("\n\n")[1]
    rows = [[c.strip() for c in line.strip("|").split("|")] for line in table.splitlines()]
    header, rows = rows[0], rows[2:]
    assert header[:3] == ["지표", "목표", "허용"], header
    return {r[0]: r for r in rows}


def _labeled_percents(cell: str) -> dict[str, float]:
    """'소아 15% · 청소년 8%' → {'소아': 0.15, '청소년': 0.08}, '70%' → {'': 0.70}"""
    return {
        label.strip(): float(n) / 100
        for label, n in re.findall(r"([^\d·%]*?)\s*(\d+(?:\.\d+)?)%", cell)
    }


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
        assert list(_labeled_percents(row[2]).values()) == pytest.approx([tolerance]), (
            f"{name} 허용: 문서 {row[2]}"
        )


def test_named_constants_exist():
    """설계 문서 전부와 CLAUDE.md가 `모듈.X`로 가리키는 이름은 코드에 실제로 있어야 한다 — 이름이 바뀌면 문서 참조가 낡는다.
    `messages.X`는 config 키(`config` `messages.balance`)라 여기서 보지 않는다"""
    modules = {
        "master": master,
        "verify": verify,
        "llm_jobs": llm_jobs,
        "documents": documents,
        "chart": chart,
        "calls": calls,
        "finance": finance,
        "metrics": metrics,
        "transcripts": transcripts,
    }
    pattern = re.compile(rf"`({'|'.join(modules)})\.(\w+)`")
    texts = [p.read_text(encoding="utf-8") for p in sorted((ROOT / "docs").glob("*.md"))]
    texts.append((ROOT / "CLAUDE.md").read_text(encoding="utf-8"))
    missing = sorted(
        {
            f"{mod}.{name}"
            for text in texts
            for mod, name in pattern.findall(text)
            if name not in PENDING and not hasattr(modules[mod], name)
        }
    )
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

    assert keys and not [k for k in keys if not exists(k)], sorted(
        k for k in keys if not exists(k)
    )


# 하위 키 중 다른 뜻의 백틱 토큰(문자 trigger·예약 상태 enum, 연령 구간·시술 age_group 등)과 이름이 겹쳐 단독으로는 보지 않는 것
_AMBIGUOUS_SUBKEYS = {
    "appt_confirm",
    "reminder",
    "noshow",
    "balance",
    "start",
    "end",
    "child",
    "teen",
    "adult",
    "senior",
}


def _config_key_names() -> set[str]:
    """config 최상위 키, 하위 키(`messages.balance` 형태), 이름이 겹치지 않는 하위 키 단독(`reply_rate`)"""
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
    tokens = [
        (m.start(), t)
        for m in re.finditer(r"`([^`]+)`", text)
        if "(" not in m.group(1)
        for t in re.findall(r"[\w.]+", m.group(1))
    ]
    bare = sorted(
        {t for pos, t in tokens if t in names and not any(a <= pos < b for a, b in covered)}
    )
    assert not bare, f"`config` 접두어 없이 쓴 config 키: {bare}"


# §4.2 문서 표에서 방해 문서(정답이 없도록 만드는 문서) 종류 — 나머지는 core
_DISTRACTOR_KINDS = {"공지 옛 버전", "유사 주제 무답", "타 병원·일반"}


def test_rag_document_counts_match_config():
    """§4.2 문서 표의 종류별 수 합계가 `config` `documents`(core·distractor)와 같아야 한다"""
    text = _doc()
    table = text[text.index("### 4.2") :].split("| 종류 | 수 | 생성 |")[1].split("\n\n")[0]
    rows = [
        [c.strip() for c in line.strip("|").split("|")] for line in table.strip().splitlines()[1:]
    ]
    counts = {"core": 0, "distractor": 0}
    for kind, n, *_ in rows:
        counts["distractor" if kind in _DISTRACTOR_KINDS else "core"] += int(n)
    assert counts == CFG["documents"], f"§4.2 표 {counts} ≠ config {CFG['documents']}"


def test_decoy_row_matches_code():
    """§5.3 경고 표의 차트 디코이 행 — 목표 배수 = master.CHART_DECOY_RATIO, 허용 범위 = verify.CHART_DECOY_RANGE"""
    row = next(r for key, r in _target_rows().items() if key.startswith("차트 디코이 비율"))
    assert [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)배", row[1])] == [
        master.CHART_DECOY_RATIO
    ], row[1]
    assert (
        tuple(float(x) for x in re.findall(r"(\d+(?:\.\d+)?)", row[2])) == verify.CHART_DECOY_RANGE
    ), row[2]


# ── §4.1 통화 분포 (6단계) ──────────────────────────────────

_PURPOSE_LABEL = {
    "예약 잡기": "book",
    "예약 변경": "reschedule",
    "예약 확인": "confirm",
    "예약 취소": "cancel",
    "리콜 문의": "recall_q",
    "치료 문의": "treatment_q",
    "보험 문의": "insurance_q",
    "가격·비용 문의": "price_q",
    "수납·청구 문의": "billing_q",
    "서류·기록 요청": "document_req",
    "응급·통증": "emergency",
    "불만": "complaint",
    "기타": "other",
    "모르겠음·애매": "unknown",
}
_COUNTERPART_LABEL = {
    "환자": "patient",
    "보호자": "guardian",
    "처음 연락": "new_caller",
    "보험사": "insurer",
    "업체": "vendor",
    "기타": "other",
}
_NOISE_LABEL = {
    "주제 2개 섞임": "two_topics",
    "말 끊김·되묻기": "interrupted",
    "급함 애매한 통증": "ambiguous_pain",
    "보호자가 정보 헷갈림": "guardian_confused",
}
_OUTBOUND_LABEL = {
    "수납 문의": "billing_q",
    "치료 문의(경과 확인)": "treatment_q",
}


def _call_section() -> str:
    text = _doc()
    return text[text.index("### 4.1") : text.index("### 4.2")]


def _line(prefix: str) -> str:
    return next(line for line in _call_section().splitlines() if line.startswith(prefix))


def _pairs(text: str) -> dict[str, float]:
    """'환자 60 / 보호자 15' → {'환자': 60, '보호자': 15}"""
    return {
        m.group(1): float(m.group(2))
        for part in text.split("/")
        if (m := re.fullmatch(r"\s*(.+?)\s+(\d+(?:\.\d+)?)\s*", part))
    }


def test_call_purpose_table_matches_code():
    table = _call_section().split("| 목적 | % | | 목적 | % |")[1].split("\n\n")[0]
    got = {}
    for line in table.strip().splitlines()[1:]:
        cells = [c.strip() for c in line.strip("|").split("|")]
        got |= {
            _PURPOSE_LABEL[cells[0]]: float(cells[1]) / 100,
            _PURPOSE_LABEL[cells[3]]: float(cells[4]) / 100,
        }
    assert got == pytest.approx(master.CALL_PURPOSE_SHARE)


def test_call_counterpart_and_noise_lines_match_code():
    names = re.match(r"\*\*상대\*\*: ([^(]+)\(", _line("**상대**")).group(1).split("/")
    assert (
        tuple(_COUNTERPART_LABEL[n.strip()] for n in names) == master.CALL_COUNTERPARTS
    )  # 비율은 정하지 않음
    noise = _pairs(_line("**노이즈**").split(":", 1)[1])
    assert {_NOISE_LABEL[k]: v / 100 for k, v in noise.items()} == pytest.approx(
        master.CALL_NOISE_SHARE
    )
    assert re.search(r"노이즈\*\*\((\d+)%\)", _line("**노이즈**")).group(1) == str(
        round(sum(master.CALL_NOISE_SHARE.values()) * 100)
    )


def test_call_direction_line_matches_code():
    line = _line("**방향**")
    got = {
        _OUTBOUND_LABEL[label.strip()]: float(n) / 100
        for label, n in re.findall(r"(?:—|\+)\s*([^+—]+?)\s+(\d+)%p", line)
    }
    assert got == pytest.approx(master.CALL_OUTBOUND_SHARE)
    assert re.search(r"outbound ~(\d+)%", line).group(1) == str(round(sum(got.values()) * 100))
    assert "절반은 복합 주제" in line and master.OUTBOUND_MULTI_SHARE == 0.5


def test_call_urgency_and_flag_lines_match_code():
    urgency = _line("**급함**")
    assert re.search(r"(\d+)% `urgent`", urgency).group(1) == str(
        round(master.PAIN_URGENT_SHARE * 100)
    )
    assert re.findall(r"`normal` (\d+) · `low` (\d+)", urgency) == [
        tuple(str(master.CALL_URGENCY_WEIGHTS[k]) for k in ("normal", "low"))
    ]
    flags = _line("**플래그**")
    assert (
        f"{round(master.APPT_CONFIRMED_SHARE * 100)}%"
        in flags.split("`appt_confirmed`")[1].split(".")[0]
    )
    todo = flags.split("`has_todo`")[1]
    assert re.findall(r"(\d+)%", todo) == [
        str(round(master.HAS_TODO_SHARE["confirm"] * 100)),
        str(round(master.HAS_TODO_DEFAULT * 100)),
    ]
    assert master.HAS_TODO_SHARE == {
        "confirm": master.HAS_TODO_SHARE["confirm"],
        "recall_q": master.HAS_TODO_SHARE["confirm"],
    }
    implied = dict(re.findall(r"`(\w+)`→(\w+)", flags))
    assert implied == master.IMPLIED_FLAG


@pytest.mark.parametrize(
    "name, tolerance",
    [
        ("통화 목적·노이즈", verify.CALL_MIX_TOLERANCE),
        ("통화 최근 가중", verify.CALL_RECENCY_TOLERANCE),
    ],
)
def test_call_mix_warning_row_matches_code(name, tolerance):
    row = next(r for key, r in _target_rows().items() if key.startswith(name))
    assert list(_labeled_percents(row[2].replace("%p", "%")).values()) == pytest.approx(
        [tolerance]
    )


def test_eval_pool_strata_numbers_match_code():
    """평가 설계 §2.1의 희귀 카테고리·과표집 비율·split당 최소"""
    text = (ROOT / "docs" / "치과 AX - 평가 설계.md").read_text(encoding="utf-8")
    section = text[text.index("### 2.1") : text.index("### 2.2")]
    assert f"**{round(master.EVAL_RARE_SHARE * 100)}%를 뽑는다**" in section
    assert f"split당 {master.EVAL_MIN_PER_SPLIT}건" in section
    rare = re.search(r"희귀 카테고리\(([^)]+)\)", section).group(1).split("·")
    assert {
        _PURPOSE_LABEL.get(r, {"응급": "emergency", "모르겠음": "unknown"}.get(r)) for r in rare
    } == set(master.EVAL_RARE_PURPOSES)


def test_daily_usd_limit_matches_doc():
    """일일 비용 상한 — 금액의 원본은 기능 정의 §7, config/llm.yaml daily_usd_limit이 같아야 한다"""
    doc = (ROOT / "docs" / "치과 AX - 기능 정의.md").read_text(encoding="utf-8")
    runtime, generator = re.search(r"하드리밋 런타임 \$(\d+)/일, 생성기 \$(\d+)/일", doc).groups()
    assert llm.load_config()["daily_usd_limit"] == {
        "generator": int(generator),
        "runtime": int(runtime),
    }


# §4.2 문서 표 종류 → 목록 표(generator/doc_catalog.py)의 (kind, type). type None = 종류 안의 모든 type
_DOC_ROW = {
    "직원 매뉴얼": ("core", "manual"),
    "FAQ": ("core", "faq"),
    "보험 안내": ("core", "insurance_guide"),
    "청구 기준": ("core", "claim_standard"),
    "원내 공지": ("core", "notice"),
    "공지 옛 버전": ("old_version", None),
    "치과 상식": ("core", "education"),
    "유사 주제 무답": ("similar", None),
    "타 병원·일반": ("external", None),
}


def test_prep_sheet_days_match_feature_doc():
    """준비표 날짜 탭 '최근 N일 + 오늘' — 기능 정의 ④의 숫자 = verify.PREP_SHEET_DAYS(잔액 있음 리포트의 범위)"""
    doc = (ROOT / "docs" / "치과 AX - 기능 정의.md").read_text(encoding="utf-8")
    assert re.findall(r"최근 (\d+)일 \+ 오늘", doc) == [str(verify.PREP_SHEET_DAYS)]


def test_rag_document_rows_match_catalog():
    """§4.2 문서 표의 종류별 수 = 목록 표의 그 종류 문서 수"""
    from generator.doc_catalog import CATALOG

    text = _doc()
    table = text[text.index("### 4.2") :].split("| 종류 | 수 | 생성 |")[1].split("\n\n")[0]
    rows = {
        r[0]: int(r[1])
        for r in (
            [c.strip() for c in line.strip("|").split("|")]
            for line in table.strip().splitlines()[1:]
        )
    }
    assert rows.keys() == _DOC_ROW.keys()
    for label, (kind, type_) in _DOC_ROW.items():
        n = sum(1 for d in CATALOG if d.kind == kind and (type_ is None or d.type == type_))
        assert rows[label] == n, f"{label}: 문서 {rows[label]} ≠ 목록 {n}"


# ── 평가 설계 §3.1 RAG 질문 (verify rag_qa 검사) ──────────────

EVAL_DOC = ROOT / "docs" / "치과 AX - 평가 설계.md"
_QA_TYPE_LABEL = {
    "매뉴얼형": "manual",
    "공지형": "notice",
    "사례형": "case",
    "데이터형": "data",
    "차트형": "chart",
    "거절형": "refusal",
}


def test_rag_qa_rules_match_code():
    """평가 설계 §3.1 유형 표의 수 = verify.QA_TYPE_COUNTS, 연속 어절 수·차트 정답 1~5건 = verify 상수"""
    text = EVAL_DOC.read_text(encoding="utf-8")
    table = text[text.index("### 3.1") :].split("| 유형 | 수 |")[1].split("\n\n")[0]
    rows = [
        [c.strip() for c in line.strip("|").split("|")] for line in table.strip().splitlines()[2:]
    ]
    assert {_QA_TYPE_LABEL[r[0]]: int(r[1]) for r in rows} == verify.QA_TYPE_COUNTS
    assert f"연속 {verify.QA_COPY_RUN}어절" in text
    assert "정답 집합이 {}~{}건".format(*verify.QA_CHART_ANSWERS) in text
