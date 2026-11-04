"""문체가 다른 dev 평가 셋 — dev 75건 × 스타일 1~3을 Sonnet으로 바꿔 쓴다(학습 금지, AI 실험 기록 8).
학습용(Haiku)과 다른 모델로 써서 학습 때 못 본 문체가 되게 한다. test는 SQL에서 뺀다.
거르기는 분류기 대신 원본·바꾼 녹취록 비교(사실·주제 보존)로 — 특정 분류기에 유리한 문항만 남지 않게.
버려지면 같은 통화·같은 스타일로 2번까지 다시 쓰고, 그래도 안 되면 칸을 비운다.
uv run --env-file .env python evaluation/finetune/aug/make_eval.py [limit]
"""

import json
import sqlite3
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
from augment import ROOT, STYLES, SYSTEM, UNKNOWN_NOTE, format_ok  # noqa: E402

from generator.master import CALL_LABEL_WORDS  # noqa: E402
from shared.llm import Gateway, JsonlLog, load_config  # noqa: E402

MODEL = "claude-sonnet-5-5"
RETRIES = 2
COST_STOP = 5.0  # 남은 크레딧 안에서 멈춘다
# 1차 생성에서 순서 바꾸기가 응급 통화에 없는 진료 요청을, 취소 통화에서 불만을 앞세웠다(AI 실험 기록 8) — 다시 쓰는 칸부터 붙인다
EXTRA_RULES = """
- 원본에 없는 요청을 만들지 않는다. 상대가 예약·진료를 직접 요청하지 않았으면 다시 쓴 것에서도 요청하지 않는다(직원이 내원을 안내하는 흐름은 그대로)
- 상대가 처음 꺼낸 용건이 계속 처음이다. 순서를 바꿀 때도 첫 용건 앞에 다른 용건(불만·증상·질문)을 두지 않는다. 이름·인사 순서만 바꿀 수 있다
- 말 고치기·되묻기는 원본에 나온 값으로만 한다. 틀린 값(다른 날짜·시각·나이·횟수·부위·기간)을 먼저 말했다가 고치는 식으로도 새 값을 만들지 않는다
- 질문을 "아니"로 거두는 것처럼 들리게 하지 않는다(비용·예약 질문은 분명히 남긴다)"""
CHECK_SYSTEM = """치과 통화 녹취록 원본(A)과 표현을 바꿔 쓴 것(B)을 비교합니다. 표현·말투·순서·군말의 차이는 괜찮습니다.
다음이 B에서 바뀌었거나 빠졌거나 새로 생겼는지만 봅니다.
- 사실: 이름, 날짜, 시각, 금액, 치아 위치, 증상, 시술, 정해진 일(예약 시각, 회신 약속, 서류 등)
- 주제: 증상 호소, 불만, 앞으로 받을 진료의 가격 질문, 새 예약 요청, 예약 변경·취소·확인, 서류 요청 등
- 누가 전화를 걸었는지, 누가 무엇을 하기로 하고 끝났는지
- 통화가 끊기거나 용건이 불분명한 원본이면, B도 그만큼 불분명한지
- 상대가 처음 꺼낸 용건이 B에서도 처음인지, 원본에 없는 예약·진료 요청이 B에서 생기지 않았는지
- 말 고치기·되묻기로 원본에 없는 값(날짜·시각·나이·횟수·부위·기간)이 잠깐이라도 나오지 않았는지"""
CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "same": {"type": "boolean", "description": "위 항목이 모두 그대로면 true"},
        "problems": {"type": "string", "description": "바뀐 점, 없으면 빈 문자열"},
    },
    "required": ["same", "problems"],
    "additionalProperties": False,
}


def load_dev():
    conn = sqlite3.connect(f"file:{ROOT}/data/source.sqlite?mode=ro", uri=True)
    conn.execute(f"ATTACH 'file:{ROOT}/ground_truth/truth.sqlite?mode=ro' AS truth")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.call_uid, c.direction, c.transcript, l.* FROM ax_calls c"
        " JOIN truth.truth_call_labels l USING (call_uid)"
        " WHERE c.source = 'generated' AND l.split = 'dev' ORDER BY c.call_uid"
    ).fetchall()
    return [dict(r) for r in rows]


def main(limit=None):
    config = load_config()
    config["aliases"]["evalaug"] = [MODEL]
    log = JsonlLog(HERE / "eval_llm_log.jsonl")
    gw = Gateway(config, log, "runtime")
    spent = {"usd": 0.0}
    lock = threading.Lock()
    words = ", ".join(CALL_LABEL_WORDS)
    jobs = [(c, s) for c in load_dev() for s in ("s1", "s2", "s3")]
    jobs = jobs[: int(limit) if limit else None]
    out_path = HERE / "dev_eval.jsonl"
    done = set()
    if out_path.exists():
        done = {json.loads(x)["aug_id"] for x in out_path.read_text().splitlines()}

    def call(**kw):
        with lock:
            if spent["usd"] >= COST_STOP:
                raise RuntimeError(f"비용 ${spent['usd']:.2f} — 멈춤")
        r = gw.complete("evalaug", max_tokens=8000, **kw)
        with lock:
            spent["usd"] += r.cost_usd
        return r

    def one(job):
        c, style = job
        system = SYSTEM.format(words=words, style=STYLES[style]) + EXTRA_RULES
        if c["primary_purpose"] == "unknown":
            system += UNKNOWN_NOTE
        tries = []
        for _ in range(1 + RETRIES):
            r = call(
                task="evalaug-rewrite",
                system=system,
                effort="low",
                messages=[{"role": "user", "content": c["transcript"]}],
            )
            if not r.ok:
                tries.append(f"생성 실패 {r.error}")
                continue
            text = r.text.strip()
            bad = format_ok(c["transcript"], text)
            if bad:
                tries.append(bad)
                continue
            k = call(
                task="evalaug-check",
                system=CHECK_SYSTEM,
                schema=CHECK_SCHEMA,
                effort="low",
                messages=[{"role": "user", "content": f"A:\n{c['transcript']}\n\nB:\n{text}"}],
            )
            if not k.ok:
                tries.append(f"검사 실패 {k.error}")
                continue
            if not k.data["same"]:
                tries.append("보존 실패: " + k.data["problems"])
                continue
            return {
                "aug_id": f"{c['call_uid']}-{style}",
                "call_uid": c["call_uid"],
                "style": style,
                "direction": c["direction"],
                "transcript": text,
                "kept": True,
                "rejected": tries,
                "rules": 2,
            }
        return {
            "aug_id": f"{c['call_uid']}-{style}",
            "call_uid": c["call_uid"],
            "style": style,
            "direction": c["direction"],
            "transcript": None,
            "kept": False,
            "rejected": tries,
        }

    todo = [j for j in jobs if f"{j[0]['call_uid']}-{j[1]}" not in done]
    print(f"작업 {len(jobs)}건, 남은 {len(todo)}건", flush=True)
    with ThreadPoolExecutor(6) as ex, out_path.open("a") as f:
        for row in ex.map(one, todo):
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
    rows = [json.loads(x) for x in out_path.read_text().splitlines()]
    print(
        f"전체 {len(rows)}칸, 채움 {sum(r['kept'] for r in rows)}칸, "
        f"재시도 {sum(len(r['rejected']) for r in rows)}회, 이번 비용 ${spent['usd']:.2f}"
    )


if __name__ == "__main__":
    main(*sys.argv[1:])
