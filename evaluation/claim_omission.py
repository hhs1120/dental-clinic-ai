"""⑤-2b 추가 처치 누락 평가 (설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §2b, 평가 설계 §5 ⑤-2).
실데이터 누락(truth_claim_errors kind omitted_extra)은 모양이 거의 하나라, 실데이터에 없는 모양의 평가 전용 합성 사례를
따로 만든다 — 사례의 모양·청구 줄·정답은 코드가 정하고(case_specs) LLM(별칭 gen)은 차트 서술 한 단락만 쓰며, 코드 검사
(check_note)를 통과한 문장만 남긴다. 원본 emr_*·truth_*는 그대로(데이터 고정). 정답이 붙은 입력이라 호출·실행 기록은
evaluation/ 파일에만 쓴다(app.sqlite에 쓰지 않는다 — CLAUDE.md 절대 규칙).
uv run --env-file .env python -m evaluation.claim_omission cases   (과금, 이어 돎 — make claim-omission-cases)"""

import argparse
import hashlib
import json
import random
import re
from pathlib import Path

import yaml

from generator.master import CHART_FORBIDDEN_WORDS, PROCEDURE_ALIASES, PROCEDURES
from shared.llm import Gateway, JsonlLog, load_config

ROOT = Path(__file__).resolve().parents[1]
CASES = ROOT / "evaluation" / "claim_omission_cases.jsonl"
LLM_LOG = ROOT / "evaluation" / "claim_omission_llm.jsonl"  # ax_llm_calls에 쓰지 않는다
WRITE_ALIAS, WRITE_TASK = "gen", "claim-omission-case"
WRITE_ATTEMPTS = 3
WRITE_PROMPT = ROOT / "prompts" / "gen_claim_omission.md"
WRITE_SCHEMA = {
    "type": "object",
    "properties": {"note": {"type": "string"}},
    "required": ["note"],
    "additionalProperties": False,
}

# 모양(설계 §2b) — 누락 있음은 실데이터에 없는 모양, 디코이는 그날 하지 않았거나 급여가 아닌 처치
SHAPES_HIT = ("plain", "everyday_word", "separated", "tooth_first", "child_sealant")
SHAPES_DECOY = (
    "planned",
    "declined",
    "elsewhere",
    "past_visit",
    "claimed_again",
    "adult_sealant",
    "uninsured",
)
PER_HIT, PER_DECOY = 4, 3
SHAPE_TEXT = {
    "plain": "청구된 진료에 이어 그날 추가 처치를 함께 시행했다고 한 문장으로 적는다",
    "everyday_word": "추가 처치를 시술 이름 대신 '때움'·'충전' 같은 일상어로 적는다('아말감'이라는 말을 쓰지 않는다)",
    "separated": "추가 처치 문장을 청구된 진료 문장과 떨어뜨려 적는다(사이에 소견·환자 말 등 다른 문장, 추가 처치 문장에는 청구된 시술 이름을 쓰지 않는다)",
    "tooth_first": "추가 처치 문장을 치아 번호로 시작한다(예: 'NN번은 …도 함께 시행함')",
    "child_sealant": "소아 구강검진 뒤 그날 어금니에 실란트(홈메우기)를 시행했다고 적는다",
    "planned": "추가 처치는 그날 하지 않고 다음 내원 때 하기로 '예정'이라고만 적는다",
    "declined": "추가 처치를 권했으나 환자가 거절하거나 미뤘다고 적는다(그날 하지 않음)",
    "elsewhere": "그 치아의 처치는 다른 병원(다른 치과)에서 받았다고 환자가 말한 것으로 적는다(그날 하지 않음)",
    "past_visit": "그 치아의 처치는 지난 내원(이전 진료) 때 했다고 적는다(그날 하지 않음)",
    "claimed_again": "청구된 시술을 청구된 치아에 시행한 사실만 다른 표현으로 한 번 더 적는다(다른 처치 없음)",
    "adult_sealant": "성인 환자에게 그날 실란트(홈메우기)를 함께 시행했다고 적는다",
    "uninsured": "그날 다른 치아에 레진 충전을 함께 시행했다고 적는다",
}
# 치아 단위 처치에 쓰는 영구치(어금니·작은어금니 위주), 실란트는 첫 큰어금니
TEETH = tuple(f"{q}{n}" for q in "1234" for n in "4567")
SEALANT_TEETH = ("16", "26", "36", "46")
CLAIMED_ADULT = ("U0001", "U0008", "U0011", "U0018")  # 스케일링·신경치료·단순발치·잇몸치료
CHILD_EXAM = "U0002"  # 구강검진
_PROC = {p["code"]: p for p in PROCEDURES}
_CODE = {p["name"]: p["code"] for p in PROCEDURES}
_MARKERS = {
    "everyday_word": ("때움", "때웠", "충전"),
    "planned": ("예정", "다음"),
    "declined": ("거절", "원치", "보류", "미루"),
    "elsewhere": ("다른 병원", "타 병원", "다른 치과"),
    "past_visit": ("지난", "이전"),
    "uninsured": ("레진",),
    "child_sealant": ("실란트", "홈메우기", "홈 메우기"),
    "adult_sealant": ("실란트", "홈메우기", "홈 메우기"),
}


def _seed() -> int:
    cfg = yaml.safe_load((ROOT / "config" / "generator.yaml").read_text(encoding="utf-8"))
    return cfg["seed"]


def _line(code: str, tooth: str) -> dict:
    return {"procedure_code": code, "procedure": _PROC[code]["name"], "tooth": tooth}


def case_specs() -> list[dict]:
    """합성 사례 명세 41건 — 누락 모양마다 PER_HIT건, 디코이마다 PER_DECOY건. 시드 고정(config/generator.yaml seed와
    단계 이름으로 따로 만든 난수 — CLAUDE.md). claimed = 청구 줄(시술·치아·나이), extra = 차트에 적을 다른 처치,
    truth = 청구에서 빠진 급여 처치 [(시술 코드, 치아)]"""
    rng = random.Random(f"{_seed()}-claim_omission")
    out = []
    for shape in SHAPES_HIT + SHAPES_DECOY:
        for _ in range(PER_HIT if shape in SHAPES_HIT else PER_DECOY):
            if shape == "child_sealant":
                claimed = _line(CHILD_EXAM, "00") | {"age": rng.randint(6, 12)}
                extra = _line(_CODE["실란트"], rng.choice(SEALANT_TEETH))
            else:
                code = rng.choice(CLAIMED_ADULT)
                tooth = "00" if code in ("U0001", "U0018") else rng.choice(TEETH)
                claimed = _line(code, tooth) | {"age": rng.randint(25, 70)}
                name = {"adult_sealant": "실란트", "uninsured": "레진"}.get(shape, "아말감/GI")
                teeth = SEALANT_TEETH if name == "실란트" else TEETH
                extra = _line(_CODE[name], rng.choice([t for t in teeth if t != tooth]))
            if shape == "claimed_again":
                tooth = rng.choice(TEETH)
                claimed = _line(_CODE["아말감/GI"], tooth) | {"age": rng.randint(25, 70)}
                extra = _line(_CODE["아말감/GI"], tooth)
            truth = [(extra["procedure_code"], extra["tooth"])] if shape in SHAPES_HIT else []
            out.append(
                {
                    "id": f"om-{len(out) + 1:02d}",
                    "shape": shape,
                    "claimed": claimed,
                    "extra": extra,
                    "truth": truth,
                }
            )
    return out


def split_of(case_id: str) -> str:
    """사례 id 해시로 반반 — 프롬프트는 dev로만 고친다"""
    return "dev" if hashlib.sha256(case_id.encode()).digest()[0] % 2 == 0 else "test"


def _spec_hash(spec: dict) -> str:
    return hashlib.sha256(
        json.dumps(spec, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()[:16]


def _words(name: str) -> tuple[str, ...]:
    return (name, *PROCEDURE_ALIASES.get(name, []))


def _sentences(note: str) -> list[str]:
    return [s.strip() for s in re.split(r"[.\n]", note) if s.strip()]


def check_note(spec: dict, note: str) -> list[str]:
    """쓴 문장이 명세대로인지 — 문제 목록(빈 목록 = 통과). 정답 라벨은 코드가 정했으므로 문장이 그 라벨과 어긋나지 않는지만
    본다: 추가 처치 치아 번호·청구 시술 이름(또는 별칭)·모양 표지가 있고, 정답 어휘(CHART_FORBIDDEN_WORDS)가 없다"""
    shape, tooth = spec["shape"], spec["extra"]["tooth"]
    problems = []
    mark = f"{tooth}번"
    if mark not in note:
        problems.append(f"'{mark}'이 없다")
    if not any(w in note for w in _words(spec["claimed"]["procedure"])):
        problems.append(f"청구된 시술 '{spec['claimed']['procedure']}'이 없다")
    if shape in _MARKERS and not any(w in note for w in _MARKERS[shape]):
        problems.append(f"'{'·'.join(_MARKERS[shape])}' 중 하나가 있어야 한다")
    if shape == "everyday_word" and "아말감" in note:
        problems.append("'아말감'이라는 말을 쓰지 않는다")
    if shape in ("plain", "separated", "tooth_first") and "아말감" not in note:
        problems.append("'아말감'이 없다")
    with_tooth = [s for s in _sentences(note) if mark in s]
    if shape == "tooth_first" and not any(s.startswith(mark) for s in with_tooth):
        problems.append(f"추가 처치 문장이 '{mark}'으로 시작하지 않는다")
    if shape == "separated" and any(
        w in s for s in with_tooth for w in _words(spec["claimed"]["procedure"])
    ):
        problems.append("추가 처치 문장에 청구된 시술 이름이 있다")
    for w in CHART_FORBIDDEN_WORDS:
        if w in note:
            problems.append(f"'{w}'를 쓰지 않는다")
    return problems


def _ask(spec: dict, problems: list[str]) -> list[dict]:
    body = {
        "청구된 진료": {
            "시술": spec["claimed"]["procedure"],
            "치아": None if spec["claimed"]["tooth"] == "00" else spec["claimed"]["tooth"],
            "나이": spec["claimed"]["age"],
        },
        "차트에 적을 다른 처치": {
            "시술": spec["extra"]["procedure"],
            "치아": spec["extra"]["tooth"],
        },
        "모양": SHAPE_TEXT[spec["shape"]],
    }
    if problems:
        body["지난 답의 문제"] = problems
    return [{"role": "user", "content": json.dumps(body, ensure_ascii=False)}]


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def write_cases(gw, path: Path = CASES, *, specs: list[dict] | None = None) -> dict:
    """명세 해시가 같은 줄이 path에 있으면 건너뛰고(이어 돎), 아니면 문장을 쓰고 check_note를 통과할 때까지 최대
    WRITE_ATTEMPTS번(다시 쓸 때 문제 목록을 알려 준다). 통과한 사례만 한 줄씩 덧붙인다"""
    specs = case_specs() if specs is None else specs
    have = {(r["id"], r["spec_hash"]) for r in _read(path)}
    system = WRITE_PROMPT.read_text(encoding="utf-8")
    out = {"written": 0, "skipped": 0, "failed": []}
    for spec in specs:
        h = _spec_hash(spec)
        if (spec["id"], h) in have:
            out["skipped"] += 1
            continue
        problems, note = [], None
        for _ in range(WRITE_ATTEMPTS):
            r = gw.complete(
                WRITE_ALIAS,
                task=WRITE_TASK,
                system=system,
                messages=_ask(spec, problems),
                schema=WRITE_SCHEMA,
            )
            text = r.data.get("note", "") if r.ok and isinstance(r.data, dict) else ""
            problems = check_note(spec, text) if text else ["답이 비었다"]
            if not problems:
                note = text
                break
        if note is None:
            out["failed"].append(spec["id"])
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps({"id": spec["id"], "spec_hash": h, "note": note}, ensure_ascii=False)
                + "\n"
            )
        out["written"] += 1
    return out


def load_cases(path: Path = CASES, *, specs: list[dict] | None = None) -> list[dict]:
    """명세 + 쓴 문장 — 지금 명세 해시와 맞는 줄만(명세가 바뀌면 그 사례는 다시 쓴다)"""
    specs = case_specs() if specs is None else specs
    notes = {(r["id"], r["spec_hash"]): r["note"] for r in _read(path)}
    return [
        s | {"note": notes[(s["id"], _spec_hash(s))]}
        for s in specs
        if (s["id"], _spec_hash(s)) in notes
    ]


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m evaluation.claim_omission")
    p.add_argument("command", choices=("cases",))
    args = p.parse_args(argv)
    if args.command == "cases":
        gw = Gateway(load_config(), JsonlLog(LLM_LOG), scope="generator")
        print(write_cases(gw))


if __name__ == "__main__":
    main()
