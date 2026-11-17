"""② 자동 개선 — 개선 에이전트가 silver 재료의 오류로 ① 분류 프롬프트 후보를 쓰고, 프롬프트 관문
(evaluation/prompt_gate.py)이 거른다. 넘으면 대기 버전(ax_settings.pending_prompt_version), 전환은 사람이 재분류로.
원본: 평가 설계 §2.7, 설계 docs/superpowers/specs/2026-11-17-auto-improve-design.md.
골드를 보므로 evaluation/에서만 돌고, 에이전트 실행 기록·탈락 후보는 RUNS(커밋 안 함), 앱으로 넘기는 것은
REPORT(라벨 없는 집계)와 대기 버전 설정뿐이다. test·dev·hard 행은 읽지 않는다."""

import json
import re
import shutil
from pathlib import Path

from backend import prompts
from evaluation.claim_qa import COPY_WORDS, _grams
from evaluation.prompt_gate import IMPROVE_REPORT
from evaluation.splits import silver_split

ITEM = "has_todo"  # 첫 목표 항목(기능 정의 §9) — 고치는 곳은 labels.md의 이 규칙 줄 하나
RUNS = Path(
    "evaluation/runs/improve"
)  # 에이전트 실행 기록·LLM 로그·탈락 후보 — 골드와 엮여 커밋하지 않는다
REPORT = IMPROVE_REPORT
RULE_PREFIX = f"- {ITEM}:"
QUOTES = re.compile(
    r"[\"“”'‘’「」]"
)  # 예시 문장 삽입 — 규칙에는 따옴표 글을 넣지 않는다(평가 설계 §2.7 재료)
CALL_ID = re.compile(r"\b(?:c|hc)-\d+")


def material_errors(conn, gold_rows: list[dict], base: str) -> list[dict]:
    """silver 재료 구간 중 기준 버전 ok 분류의 has_todo가 골드와 다른 통화(call_uid 순). conn은 앱 DB에 source를
    붙인 연결, gold_rows는 truth_call_labels silver 행 전체(층화를 위해 검증 구간도 함께) — 다른 분할이 섞이면 멈춘다"""
    if any(r["split"] != "silver" for r in gold_rows):
        raise SystemExit("holdout_in_material")
    part = silver_split(gold_rows)
    gold = {r["call_uid"]: int(r[ITEM]) for r in gold_rows if part[r["call_uid"]] == "material"}
    out = []
    for u, labels, transcript, direction in conn.execute(
        "SELECT k.call_uid, k.labels, c.transcript, c.direction FROM ax_call_classifications k"
        " JOIN source.ax_calls c ON c.call_uid = k.call_uid"
        " WHERE k.model_alias = 'classify' AND k.prompt_version = ? AND k.status = 'ok'"
        " ORDER BY k.call_uid",
        (base,),
    ):
        if u not in gold:
            continue
        model = int(json.loads(labels)[ITEM])
        if model != gold[u]:
            out.append({"call_uid": u, "transcript": transcript, "direction": direction,
                        "model": model, "gold": gold[u]})  # fmt: skip
    return out


def rule_line(labels_md: str) -> str:
    """labels.md의 목표 항목 규칙 줄 — 없거나 둘 이상이면 ValueError"""
    lines = [x for x in labels_md.splitlines() if x.startswith(RULE_PREFIX)]
    if len(lines) != 1:
        raise ValueError(f"{RULE_PREFIX} 줄이 {len(lines)}개")
    return lines[0]


def candidate_name(base: str, root: Path = prompts.PROMPTS_DIR, runs: Path = RUNS) -> str:
    """<기준>-imp<n> — 앱 프롬프트 폴더와 탈락 후보 보관 폴더 어디에도 없는 가장 작은 n"""
    n = 1
    while (root / "classify" / f"{base}-imp{n}").exists() or (
        runs / "candidates" / f"{base}-imp{n}"
    ).exists():
        n += 1
    return f"{base}-imp{n}"


def write_candidate(base: str, name: str, new_rule: str, root: Path = prompts.PROMPTS_DIR) -> Path:
    """기준 버전 폴더를 복사하고 labels.md의 규칙 줄만 바꾼다(summary.md·모델은 그대로). 있는 폴더는 덮지 않는다"""
    src, dst = root / "classify" / base, root / "classify" / name
    if dst.exists():
        raise FileExistsError(dst)
    shutil.copytree(src, dst)
    md = (dst / "labels.md").read_text(encoding="utf-8")
    (dst / "labels.md").write_text(md.replace(rule_line(md), new_rule), encoding="utf-8")
    return dst


def _copied(text: str, material: list[dict]) -> bool:
    mine = _grams(text, COPY_WORDS)
    return any(mine & _grams(m["transcript"], COPY_WORDS) for m in material)


def check_candidate(base_md: str, cand_md: str, material: list[dict]) -> list[str]:
    """관문 전 코드 검사 — 못 넘은 이름: scope(규칙 줄 밖 변경), example(따옴표 예시 글), copy(재료 녹취와
    연속 COPY_WORDS어절 겹침, 평가 설계 §3.1 베끼기 기준)"""
    old, new = base_md.splitlines(), cand_md.splitlines()
    rule = rule_line(base_md)
    try:
        new_rule = rule_line(cand_md)
    except ValueError:
        return ["scope"]
    if [x for x in old if x != rule] != [x for x in new if x != new_rule]:
        return ["scope"]
    failed = []
    if QUOTES.search(new_rule):
        failed.append("example")
    if _copied(new_rule, material):
        failed.append("copy")
    return failed


def leaks(text: str, material: list[dict]) -> bool:
    """앱으로 넘기는 자유 글 칸 검사(평가 설계 §2.7 누출 차단) — 재료 녹취 베끼기나 통화 id"""
    return bool(CALL_ID.search(text)) or _copied(text, material)
