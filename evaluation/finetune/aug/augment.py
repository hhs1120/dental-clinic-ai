"""파인튜닝용 녹취록 바꿔 쓰기 — silver만 읽는다(dev·test는 SQL에서 뺀다). 결과는 aug.jsonl(커밋 안 함).
전체 silver × 스타일 1~3, 적은 목적(불만·모르겠음·응급) × 스타일 4~5. 바꾼 녹취록은 Haiku 분류기(classify_v1)로 다시
분류해, 원본 분류가 맞힌 항목을 바꾼 쪽이 틀리면 kept=false. 급함은 urgent 여부만 본다(normal/low는 골드가 추첨 — AI 실험 기록 7).
uv run --env-file .env python evaluation/finetune/aug/augment.py [limit]
"""

import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from generator.master import (  # noqa: E402
    CALL_COUNTERPARTS,
    CALL_FLAGS,
    CALL_LABEL_WORDS,
    CALL_PURPOSE_SHARE,
    URGENCIES,
)
from shared.llm import Gateway, JsonlLog, load_config  # noqa: E402

HERE = Path(__file__).parent
MODEL = "claude-haiku-5-5"
RARE = ("complaint", "unknown", "emergency")
STYLES = {
    "s1": "말투를 바꾼다: 존댓말의 정도, 맞장구·짧은 대답(네, 아 네, 그렇죠), 문장 길이를 원본과 다르게",
    "s2": "말하는 순서를 바꾼다: 용건·이름·사정을 꺼내는 순서를 원본과 다르게(용건을 먼저 또는 나중에)",
    "s3": "실제 통화처럼 군말을 넣는다: 어, 음, 저기, 말 고치기, 한두 번 되묻기·잘못 들은 것 다시 말하기",
    "s4": "말투를 바꾸고(존댓말 정도·짧은 대답·문장 길이) 말하는 순서도 원본과 다르게",
    "s5": "말하는 순서를 바꾸고 군말(어, 음, 말 고치기)과 한 번의 되묻기를 넣는다",
}
SYSTEM = """치과 데스크 통화 녹취록을 다시 씁니다. 같은 통화를 다른 사람이 했을 때처럼 표현만 바꿉니다.

반드시 지킬 것:
- 사실은 하나도 바꾸지 않는다: 이름, 날짜, 시각, 금액, 치아 위치, 증상, 시술, 정해진 일(예약 시각, 회신 약속, 서류 등)
- 주제를 더하거나 빼지 않는다. 증상 호소·불만·가격 질문·예약 요청처럼 원본에 있는 말은 남기고, 없는 말은 만들지 않는다
- 누가 먼저 전화를 걸었는지, 통화가 어떻게 끝나는지는 그대로. 첫 줄은 원본 첫 줄과 같은 화자의 인사(전화 받은 쪽이 먼저 말한다)
- 한 줄에 발화 하나, 각 줄은 "직원: " 또는 "환자: "로 시작(원본의 화자 표시 그대로)
- 분량은 원본과 비슷하게(±20%)
- 다음 말은 쓰지 않는다: {words}
- 녹취록만 출력한다. 설명·제목·따옴표 없이

이번에 바꾸는 방식: {style}"""
UNKNOWN_NOTE = "\n- 이 통화는 끊기거나 용건이 분명하지 않은 통화다. 용건을 분명하게 만들거나 내용을 더하지 않는다"
ITEMS = ("primary_purpose", "counterpart", "urgency") + CALL_FLAGS
SCHEMA = {
    "type": "object",
    "properties": {
        "primary_purpose": {"type": "string", "enum": list(CALL_PURPOSE_SHARE)},
        "counterpart": {"type": "string", "enum": list(CALL_COUNTERPARTS)},
        "urgency": {"type": "string", "enum": list(URGENCIES)},
        **{f: {"type": "boolean"} for f in CALL_FLAGS},
    },
    "required": list(ITEMS),
    "additionalProperties": False,
}


def load_silver():
    conn = sqlite3.connect(f"file:{ROOT}/data/source.sqlite?mode=ro", uri=True)
    conn.execute(f"ATTACH 'file:{ROOT}/ground_truth/truth.sqlite?mode=ro' AS truth")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT c.call_uid, c.direction, c.transcript, l.* FROM ax_calls c"
        " JOIN truth.truth_call_labels l USING (call_uid)"
        " WHERE c.source = 'generated' AND l.split = 'silver' ORDER BY c.call_uid"
    ).fetchall()
    return [dict(r) for r in rows]


def norm(item, v):
    """비교용 값 — 급함은 urgent 여부만"""
    if item == "urgency":
        return v == "urgent"
    return bool(v) if item in CALL_FLAGS else v


def format_ok(orig: str, text: str) -> str | None:
    lines = [x for x in text.strip().splitlines() if x.strip()]
    if not lines or any(not x.startswith(("직원: ", "환자: ")) for x in lines):
        return "화자 표시"
    if lines[0].split(":")[0] != orig.strip().split(":")[0]:
        return "첫 화자"
    if any(w in text for w in CALL_LABEL_WORDS):
        return "라벨 어휘"
    ratio = len(text) / len(orig)
    if not 0.7 <= ratio <= 1.4:
        return f"분량 {ratio:.2f}"
    return None


def main(limit=None):
    config = load_config()
    config["aliases"]["aug"] = [MODEL]
    gw = Gateway(config, JsonlLog(HERE / "llm_log.jsonl"), "runtime")
    classify_system = (HERE / "classify_v1.md").read_text()
    base = {
        r["call_uid"]: r["labels"]
        for r in map(
            json.loads, (HERE / "v1_claude-haiku-5-5_silver.jsonl").read_text().splitlines()
        )
    }
    calls = load_silver()
    jobs = [(c, s) for c in calls for s in ("s1", "s2", "s3")]
    jobs += [(c, s) for c in calls if c["primary_purpose"] in RARE for s in ("s4", "s5")]
    jobs = jobs[: int(limit) if limit else None]
    out_path = HERE / "aug.jsonl"
    done = set()
    if out_path.exists():
        done = {json.loads(x)["aug_id"] for x in out_path.read_text().splitlines()}
    words = ", ".join(CALL_LABEL_WORDS)

    def one(job):
        c, style = job
        aug_id = f"{c['call_uid']}-{style}"
        system = SYSTEM.format(words=words, style=STYLES[style])
        if c["primary_purpose"] == "unknown":
            system += UNKNOWN_NOTE
        row = {
            "aug_id": aug_id,
            "call_uid": c["call_uid"],
            "style": style,
            "direction": c["direction"],
            "transcript": None,
            "kept": False,
            "why": None,
        }
        r = gw.complete(
            "aug",
            task="aug-rewrite",
            system=system,
            max_tokens=4000,
            messages=[{"role": "user", "content": c["transcript"]}],
        )
        if not r.ok:
            row["why"] = f"생성 실패 {r.error}"
            return row
        text = r.text.strip()
        row["transcript"] = text
        bad = format_ok(c["transcript"], text)
        if bad:
            row["why"] = bad
            return row
        k = gw.complete(
            "aug",
            task="aug-check",
            system=classify_system,
            schema=SCHEMA,
            max_tokens=4000,
            messages=[{"role": "user", "content": f"방향: {c['direction']}\n\n{text}"}],
        )
        if not k.ok:
            row["why"] = f"검사 실패 {k.error}"
            return row
        orig = base.get(c["call_uid"])
        lost = [
            i
            for i in ITEMS
            if orig and norm(i, orig[i]) == norm(i, c[i]) and norm(i, k.data[i]) != norm(i, c[i])
        ]
        row["check"] = k.data
        row["why"] = ("놓친 항목 " + ",".join(lost)) if lost else None
        row["kept"] = not lost
        return row

    todo = [j for j in jobs if f"{j[0]['call_uid']}-{j[1]}" not in done]
    print(f"작업 {len(jobs)}건, 남은 {len(todo)}건", flush=True)
    with ThreadPoolExecutor(8) as ex, out_path.open("a") as f:
        for i, row in enumerate(ex.map(one, todo)):
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            if i % 200 == 0:
                print(i, flush=True)
    rows = [json.loads(x) for x in out_path.read_text().splitlines()]
    cost = sum(
        json.loads(x)["cost_usd"] for x in (HERE / "llm_log.jsonl").read_text().splitlines()
    )
    print(f"전체 {len(rows)}건, 남김 {sum(r['kept'] for r in rows)}건, 비용 ${cost:.2f}")


if __name__ == "__main__":
    main(*sys.argv[1:])
