"""어려운 통화 정답 라벨 (어려운 통화 설계 docs/superpowers/specs/2026-11-11-hard-calls-design.md §4).
생성 라벨 + Opus 독립 라벨(분류기와 같은 라벨 정의·출력, 녹취·방향만) → 엇갈린 통화와 일치 표본을 사람이 출처를 가린 채
검토 → 최종 라벨 JSON(ground_truth/hard_call_labels.json — make data가 적재). 골드를 보므로 evaluation/에서만 돌고 기록·
호출 로그도 evaluation/에 쓴다(app.sqlite에 쓰지 않는다 — CLAUDE.md 절대 규칙).
uv run --env-file .env python -m evaluation.hard_labels label   (과금, Batch, 이어 돎 — make hard-label)
uv run python -m evaluation.hard_labels review                   (사람 — make hard-review, 끝나면 최종 JSON)
"""

import argparse
import hashlib
import json
import sqlite3
import time
from collections import Counter, defaultdict
from pathlib import Path

from backend import classify, prompts
from backend.classify import REASONING_MAX_TOKENS
from evaluation.metrics import HARD_AGREED_SAMPLE
from generator.paths import output_paths
from shared.call_labels import (
    COUNTERPART_KO,
    FLAG_KO,
    FLAGS,
    ITEMS,
    LABEL_SCHEMA,
    PURPOSE_KO,
    same_value,
)
from shared.llm import Gateway, JsonlLog, load_config

ROOT = Path(__file__).resolve().parents[1]
ALIAS = "hard_label"  # config/llm.yaml — Opus, 폴백 없음
LABEL_PROMPT = (
    "v2"  # 분류기 라벨 정의(prompts/classify/v2/labels.md) — 이 셋 때문에 고치지 않는다(설계 §2)
)
EFFORT = "medium"
TASK = "hard-label"
RECORDS = ROOT / "evaluation" / "hard_labels.jsonl"
LLM_LOG = (
    ROOT / "evaluation" / "hard_labels_llm.jsonl"
)  # 정답 비교 기록 옆 — ax_llm_calls에 쓰지 않는다
BATCH_STATE = (
    ROOT / "evaluation" / "hard_labels_batch.json"
)  # 제출한 배치 id — 끊겨도 다시 제출하지 않게(커밋 안 함)
ITEM_KO = {"primary_purpose": "용건", "counterpart": "통화 상대", "urgency": "급함"} | FLAG_KO
NEITHER = "둘 다 아님"


def connect(source: Path, truth: Path) -> sqlite3.Connection:
    """읽기 전용 — 생성·정답 DB에 쓰지 않는다(최종 라벨은 JSON으로)"""
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS truth", (f"file:{truth}?mode=ro",))
    return conn


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def targets(conn) -> list[dict]:
    """생성 라벨 그대로인 hc- 통화 — 녹취·방향·생성 라벨(최종 라벨을 적재한 뒤의 행은 기록의 생성 라벨을 쓴다)"""
    rows = conn.execute(
        "SELECT c.call_uid, c.direction, c.transcript, l.accepted_purposes, h.hard_type, h.half, "
        + ", ".join(f"l.{i}" for i in ITEMS)
        + " FROM ax_calls c JOIN truth.truth_call_labels l USING (call_uid)"
        " JOIN truth.truth_hard_calls h USING (call_uid)"
        " WHERE h.label_source = 'generated' AND c.transcript IS NOT NULL ORDER BY c.call_uid"
    ).fetchall()
    return [
        {
            "call_uid": r["call_uid"],
            "direction": r["direction"],
            "transcript": r["transcript"],
            "hard_type": r["hard_type"],
            "half": r["half"],
            "generated": {i: r[i] for i in ITEMS}
            | {"accepted_purposes": json.loads(r["accepted_purposes"])},
        }
        for r in rows
    ]


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def save(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(r, ensure_ascii=False) + "\n"
            for r in sorted(records, key=lambda r: r["call_uid"])
        ),
        encoding="utf-8",
    )


def _request(t: dict) -> dict:
    """분류기 라벨 호출과 같은 입력(녹취·방향) — 생성 라벨·종류·시나리오는 넣지 않는다"""
    return {
        "custom_id": t["call_uid"],
        "system": prompts.load("classify", LABEL_PROMPT)["labels"],
        "messages": [
            {"role": "user", "content": classify.user_message(t["transcript"], t["direction"])}
        ],
        "schema": LABEL_SCHEMA,
        "effort": EFFORT,
        "max_tokens": REASONING_MAX_TOKENS,
    }


def _opus(data) -> dict | None:
    if not classify.valid_labels(data):
        return None
    return {i: int(data[i]) if i in FLAGS else data[i] for i in ITEMS}


def label(gw, conn, path: Path, state_path: Path = BATCH_STATE, poll_seconds: int = 60) -> dict:
    """기록에 없거나 녹취가 바뀐 hc-를 Opus Batch로 라벨한다. 배치 id·보낸 녹취 해시를 state_path에 남겨 끊겨도 이어 받는다.
    결과 없는 건·스키마 밖 답은 기록하지 않아 다음 실행에서 다시 돈다"""
    now = {t["call_uid"]: t for t in targets(conn)}
    records = {r["call_uid"]: r for r in load(path)}
    if state_path.exists():
        st = json.loads(state_path.read_text(encoding="utf-8"))
        bid, sent = st["batch_id"], st["sent"]
    else:
        todo = [
            t
            for u, t in now.items()
            if u not in records or records[u]["transcript_sha"] != _sha(t["transcript"])
        ]
        if not todo:
            return {"done": 0, "failed": 0, "cost_usd": 0.0}
        bid = gw.submit_batch(
            ALIAS, [_request(t) for t in todo], task=TASK, prompt_version=LABEL_PROMPT
        )
        sent = {t["call_uid"]: _sha(t["transcript"]) for t in todo}
        state_path.write_text(json.dumps({"batch_id": bid, "sent": sent}), encoding="utf-8")
    while gw.batch_status(bid, ALIAS) != "ended":
        time.sleep(poll_seconds)
    results = gw.batch_results(
        bid, ALIAS, task=TASK, prompt_version=LABEL_PROMPT, schema=LABEL_SCHEMA
    )
    done = 0
    for uid, r in results.items():
        t = now.get(uid)
        opus = _opus(r.data) if r is not None and r.ok else None
        if t is None or opus is None or sent.get(uid) != _sha(t["transcript"]):
            continue
        records[uid] = {
            "call_uid": uid,
            "transcript_sha": sent[uid],
            "hard_type": t["hard_type"],
            "half": t["half"],
            "generated": t["generated"],
            "opus": opus,
        }
        done += 1
    save(path, list(records.values()))
    state_path.unlink()
    cost = sum(r.cost_usd for r in results.values() if r is not None)
    return {"done": done, "failed": len(sent) - done, "cost_usd": round(cost, 4)}


def compare(records: list[dict]) -> dict[str, list[str]]:
    """{call_uid: 엇갈린 항목} — 급함은 urgent 여부만(shared.call_labels.same_value)"""
    out = {}
    for r in records:
        diff = [i for i in ITEMS if not same_value(i, r["generated"][i], r["opus"][i])]
        if diff:
            out[r["call_uid"]] = diff
    return out


def sample(records: list[dict], n: int = HARD_AGREED_SAMPLE) -> list[str]:
    """일치해서 확정된 통화의 고정 표본 — call_uid 해시순 앞 n(설계 §4)"""
    diff = compare(records)
    agreed = [r["call_uid"] for r in records if r["call_uid"] not in diff]
    return sorted(agreed, key=_sha)[:n]


def _side(uid: str, item: str) -> str:
    """생성 라벨을 가·나 중 어디에 둘지 — 통화·항목 해시(출처를 가린다)"""
    return "가" if hashlib.sha256(f"{uid}:{item}".encode()).digest()[0] % 2 == 0 else "나"


def _show(item: str, value) -> str:
    if item == "urgency":
        return "급함" if value == "urgent" else "급하지 않음"
    if item == "primary_purpose":
        return f"{PURPOSE_KO[value]} ({value})"
    if item == "counterpart":
        return f"{COUNTERPART_KO[value]} ({value})"
    return "예" if value else "아니오"


def _options(item: str) -> list:
    if item == "primary_purpose":
        return list(PURPOSE_KO)
    if item == "counterpart":
        return list(COUNTERPART_KO)
    if item == "urgency":
        return ["urgent", "normal"]
    return [1, 0]


def _ask(read, write, prompt: str, keys) -> str:
    """keys = 받을 답 목록 또는 답을 검사하는 함수"""
    ok = keys if callable(keys) else keys.__contains__
    while True:
        key = read(prompt).strip()
        if ok(key):
            return key
        write("  다시 고르세요")


def _pick_value(read, write, item: str):
    opts = _options(item)
    for k, v in enumerate(opts, start=1):
        write(f"    {k} {_show(item, v)}")
    return opts[
        int(_ask(read, write, "  값 번호 > ", [str(k) for k in range(1, len(opts) + 1)])) - 1
    ]


def review(
    conn, path: Path, read=input, write=print, sample_size: int = HARD_AGREED_SAMPLE
) -> None:
    """사람 검토 — 엇갈린 통화는 항목마다 가·나(출처를 가림)·둘 다 아님(값을 직접 고름), 일치 표본은 맞음/틀림
    (틀리면 고칠 항목을 고름). 통화마다 저장한다"""
    records = load(path)
    by = {r["call_uid"]: r for r in records}
    transcripts = dict(conn.execute("SELECT call_uid, transcript FROM ax_calls"))
    diff = compare(records)
    picks = sample(records, sample_size)
    todo = [u for u in sorted(diff) if "human" not in by[u]] + [
        u for u in picks if "sample_ok" not in by[u]
    ]
    write(f"엇갈림 {len(diff)}통화 · 일치 표본 {len(picks)}통화 중 남은 {len(todo)}통화")
    for k, uid in enumerate(todo, start=1):
        r = by[uid]
        write(f"\n[{k}/{len(todo)}] {uid}\n")
        write(transcripts[uid])
        if uid in diff:
            human = {}
            for item in diff[uid]:
                gen_side = _side(uid, item)
                vals = {
                    gen_side: r["generated"][item],
                    ("나" if gen_side == "가" else "가"): r["opus"][item],
                }
                write(
                    f"\n{ITEM_KO[item]} — 가: {_show(item, vals['가'])} / 나: {_show(item, vals['나'])}"
                )
                key = _ask(read, write, f"  1 가 · 2 나 · 3 {NEITHER} > ", ["1", "2", "3"])
                human[item] = (
                    vals["가"]
                    if key == "1"
                    else vals["나"]
                    if key == "2"
                    else _pick_value(read, write, item)
                )
            r["human"] = human
        else:
            write(
                "\n라벨: "
                + " · ".join(f"{ITEM_KO[i]} {_show(i, r['generated'][i])}" for i in ITEMS)
            )
            ok = _ask(read, write, "  1 맞음 · 2 틀림 > ", ["1", "2"]) == "1"
            r["sample_ok"] = ok
            if not ok:
                fixes = {}
                for idx, item in enumerate(ITEMS, start=1):
                    write(f"    {idx} {ITEM_KO[item]}")
                chosen = _ask(read, write, "  고칠 항목 번호(쉼표) > ", _item_numbers)
                for idx in chosen.split(","):
                    item = ITEMS[int(idx) - 1]
                    fixes[item] = _pick_value(read, write, item)
                r["human"] = fixes
        save(path, records)


def _item_numbers(text: str) -> bool:
    """고칠 항목 번호 — 한 개 또는 쉼표로 이은 1..len(ITEMS)"""
    parts = [x.strip() for x in text.split(",")]
    return all(x.isdigit() and 1 <= int(x) <= len(ITEMS) for x in parts)


def final_labels(r: dict) -> dict:
    """생성 라벨 + 사람이 정한 항목. 주 목적이 바뀌면 허용 목적은 그 목적 하나. 급함을 '급하지 않음'으로 고르면 normal"""
    out = dict(r["generated"])
    for item, v in (r.get("human") or {}).items():
        out[item] = v
    if out["primary_purpose"] != r["generated"]["primary_purpose"]:
        out["accepted_purposes"] = [out["primary_purpose"]]
    return out


def finalize(conn, path: Path, out: Path, sample_size: int = HARD_AGREED_SAMPLE) -> dict:
    """모든 hc-에 Opus 라벨, 엇갈림·표본 검토가 끝났으면 최종 라벨 JSON을 쓰고 보고 수치를 돌려준다(설계 §4-6)"""
    records = load(path)
    have = {r["call_uid"] for r in records}
    missing = [t["call_uid"] for t in targets(conn) if t["call_uid"] not in have]
    diff = compare(records)
    picks = set(sample(records, sample_size))
    by = {r["call_uid"]: r for r in records}
    left = [u for u in diff if "human" not in by[u]] + [
        u for u in picks if "sample_ok" not in by[u]
    ]
    if missing or left:
        raise SystemExit(f"Opus 라벨 없음 {len(missing)}통화 · 검토 남음 {len(left)}통화")
    rows = [
        {
            "call_uid": r["call_uid"],
            "labels": final_labels(r),
            "label_source": "human" if r.get("human") else "agreed",
        }
        for r in sorted(records, key=lambda r: r["call_uid"])
    ]
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    item_agree = {
        i: sum(same_value(i, r["generated"][i], r["opus"][i]) for r in records) for i in ITEMS
    }
    by_type = defaultdict(lambda: [0, 0])
    for r in records:
        by_type[r["hard_type"]][0] += r["call_uid"] not in diff
        by_type[r["hard_type"]][1] += 1
    sides = Counter()
    for u, items in diff.items():
        for item in items:
            v = by[u]["human"][item]
            g, o = by[u]["generated"][item], by[u]["opus"][item]
            sides[
                "generated"
                if same_value(item, v, g)
                else "opus"
                if same_value(item, v, o)
                else "neither"
            ] += 1
    return {
        "n": len(records),
        "agreed_calls": len(records) - len(diff),
        "item_agree": item_agree,
        "by_type": {t: f"{a}/{n}" for t, (a, n) in sorted(by_type.items())},
        "human_sides": dict(sides),
        "sample_wrong": sum(not by[u]["sample_ok"] for u in picks),
    }


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("cmd", choices=["label", "review"])
    args = parser.parse_args(argv)
    paths = output_paths()
    conn = connect(paths["source"], paths["truth"])
    if args.cmd == "label":
        gw = Gateway(load_config(), JsonlLog(LLM_LOG), scope="generator")
        print(label(gw, conn, RECORDS))
        return
    review(conn, RECORDS)
    print(json.dumps(finalize(conn, RECORDS, paths["hard_labels"]), ensure_ascii=False, indent=1))
    print(f"최종 라벨 → {paths['hard_labels']} — 다음: make data")


if __name__ == "__main__":
    main()
