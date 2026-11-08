"""처리 정답 — 후속 처리안 채점 모수 통화에 Opus가 녹취록만 보고 처리 종류를 붙이고(Batch), 사람이 표본을 직접 고른다.
골드에는 할 일의 내용이 없어 코드로 파생하지 않는다(설계 docs/superpowers/specs/2026-11-08-followup-autoclose-design.md
"PR ② 처리 정답", 평가 설계 §5). 기록은 evaluation/call_actions.jsonl — 앱 DB·truth.sqlite에 쓰지 않는다."""

import argparse
import json
import time
from pathlib import Path

from backend import prompts
from backend.calls import active_versions
from backend.classify import REASONING_MAX_TOKENS
from evaluation.gold_audit import _hash_order, connect, load, save, transcript_sha
from evaluation.metrics import ACTION_AUDIT_SAMPLE
from evaluation.splits import silver_split
from shared.followup import ACTION_DESC, ACTION_KO, ACTIONS
from shared.llm import Gateway, JsonlLog, load_config

ROOT = Path(__file__).resolve().parents[1]
TASK, PROMPT_VERSION, ALIAS = "call_actions", "v1", "action_label"
ACTIONS_PATH = ROOT / "evaluation" / "call_actions.jsonl"
LLM_LOG = ROOT / "evaluation" / "call_actions_llm.jsonl"
BATCH_STATE = ROOT / "evaluation" / "call_actions_batch.json"  # 커밋 안 함

__all__ = ["load", "save"]


def eval_calls(conn) -> dict[str, list[str]]:
    """채점 모수 — 골드 has_todo와 활성 분류 has_todo가 모두 참인 통화. dev = dev 판정 풀, validation = silver 검증 구간"""
    a = active_versions(conn)[0]
    cls_todo = {
        u
        for u, lab in conn.execute(
            "SELECT call_uid, labels FROM ax_call_classifications"
            " WHERE model_alias = 'classify' AND prompt_version = ? AND status = 'ok'",
            (a,),
        )
        if lab and json.loads(lab).get("has_todo")
    }
    rows = conn.execute(
        "SELECT l.call_uid, l.split, l.has_todo, l.primary_purpose, l.emergency, l.complaint,"
        " c.in_eval_pool FROM truth.truth_call_labels l JOIN source.ax_calls c USING (call_uid)"
    ).fetchall()
    silver = [dict(r) for r in rows if r["split"] == "silver"]
    validation = {u for u, part in silver_split(silver).items() if part == "validation"}
    pick = lambda keep: sorted(  # noqa: E731
        r["call_uid"] for r in rows if r["has_todo"] and r["call_uid"] in cls_todo and keep(r)
    )
    return {
        "dev": pick(lambda r: r["split"] == "dev" and r["in_eval_pool"]),
        "validation": pick(lambda r: r["call_uid"] in validation),
    }


def label_request(transcript: str, direction: str) -> tuple[str, dict]:
    """(user 메시지, 스키마) — 녹취록과 방향만. 분류기 결과·골드 라벨은 넣지 않는다"""
    schema = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": list(ACTIONS)}},
        "required": ["action"],
        "additionalProperties": False,
    }
    return f"방향: {direction}\n\n녹취록:\n{transcript}", schema


def _calls(conn, uids) -> dict[str, dict]:
    q = ",".join("?" * len(uids))
    return {
        r["call_uid"]: dict(r)
        for r in conn.execute(
            f"SELECT call_uid, direction, transcript FROM source.ax_calls WHERE call_uid IN ({q})",
            list(uids),
        )
    }


def label(gw, conn, path: Path, state_path: Path = BATCH_STATE, poll_seconds: int = 60) -> dict:
    """기록이 없거나 녹취록이 바뀐 모수 통화만 Batch로. 배치 id를 state_path에 남겨 끊겨도 다시 제출하지 않는다"""
    uids = [u for us in eval_calls(conn).values() for u in us]
    calls = _calls(conn, uids)
    records = {r["call_uid"]: r for r in load(path)}
    if state_path.exists():
        bid = json.loads(state_path.read_text(encoding="utf-8"))["batch_id"]
    else:
        todo = [
            u
            for u in uids
            if u not in records
            or records[u]["transcript_sha"] != transcript_sha(calls[u]["transcript"])
        ]
        if not todo:
            return {"done": 0, "failed": 0, "cost_usd": 0.0}
        system = prompts.load(TASK, PROMPT_VERSION)["system"]
        effort = prompts.effort(TASK, PROMPT_VERSION)
        reqs = []
        for u in todo:
            msg, schema = label_request(calls[u]["transcript"], calls[u]["direction"])
            reqs.append(
                {
                    "custom_id": u,
                    "system": system,
                    "schema": schema,
                    "messages": [{"role": "user", "content": msg}],
                    **({"effort": effort, "max_tokens": REASONING_MAX_TOKENS} if effort else {}),
                }
            )
        bid = gw.submit_batch(ALIAS, reqs, task="call-actions", prompt_version=PROMPT_VERSION)
        state_path.write_text(json.dumps({"batch_id": bid}), encoding="utf-8")
    while gw.batch_status(bid) != "ended":
        time.sleep(poll_seconds)
    results = gw.batch_results(
        bid, ALIAS, task="call-actions", prompt_version=PROMPT_VERSION, schema={"type": "object"}
    )
    done = 0
    for u, r in results.items():
        action = (r.data or {}).get("action") if r is not None and r.ok else None
        if u not in calls or action not in ACTIONS:
            continue
        records[u] = {
            "call_uid": u,
            "transcript_sha": transcript_sha(calls[u]["transcript"]),
            "llm_action": action,
            "human_action": None,
            "prompt_version": PROMPT_VERSION,
        }
        done += 1
    _save(path, list(records.values()))
    state_path.unlink()
    cost = sum(r.cost_usd or 0 for r in results.values() if r is not None)
    return {"done": done, "failed": len(results) - done, "cost_usd": round(cost, 4)}


def _save(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(
            json.dumps(r, ensure_ascii=False) + "\n"
            for r in sorted(records, key=lambda r: r["call_uid"])
        ),
        encoding="utf-8",
    )


def audit(conn, path: Path, read=input, write=print, n: int = ACTION_AUDIT_SAMPLE) -> None:
    """사람 표본 — 해시 순 앞 n통화를 한 통화씩. 라벨은 저장 전까지 보이지 않는다(앵커링 없게), 통화마다 저장"""
    records = load(path)
    picks = sorted(records, key=lambda r: _hash_order(r["call_uid"]))[:n]
    todo = [r for r in picks if r["human_action"] is None]
    calls = _calls(conn, [r["call_uid"] for r in todo]) if todo else {}
    write(f"표본 {len(picks)}통화 중 남은 {len(todo)}통화")
    for k, rec in enumerate(todo, len(picks) - len(todo) + 1):
        c = calls[rec["call_uid"]]
        write(f"\n[{k}/{len(picks)}] {rec['call_uid']} · {c['direction']}\n")
        write(c["transcript"])
        write("\n통화 뒤 직원이 해야 할 일에 가장 맞는 것:")
        for i, a in enumerate(ACTIONS, 1):
            write(f"  {i} {ACTION_KO[a]} — {ACTION_DESC[a]}")
        while True:
            key = read(f"  1~{len(ACTIONS)} > ").strip()
            if key.isdigit() and 1 <= int(key) <= len(ACTIONS):
                break
            write("  다시 고르세요")
        rec["human_action"] = ACTIONS[int(key) - 1]
        _save(path, records)
        write(f"저장 — 모델 라벨: {ACTION_KO[rec['llm_action']]}")


def truth(records: list[dict]) -> dict[str, str]:
    """정답 — 사람 답이 있으면 사람, 없으면 모델 라벨"""
    return {r["call_uid"]: r["human_action"] or r["llm_action"] for r in records}


def agreement(records: list[dict]) -> dict:
    """모델 라벨 vs 사람 — 사람이 고른 통화만, 종류는 사람 답 기준"""
    out = {"k": 0, "n": 0, "by_action": {}}
    for r in records:
        if not r["human_action"]:
            continue
        hit = r["llm_action"] == r["human_action"]
        b = out["by_action"].setdefault(r["human_action"], {"k": 0, "n": 0})
        b["n"] += 1
        b["k"] += hit
        out["n"] += 1
        out["k"] += hit
    return out


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m evaluation.call_actions")
    p.add_argument("command", choices=("label", "audit"))
    args = p.parse_args(argv)
    conn = connect(
        ROOT / "data" / "app.sqlite",
        ROOT / "data" / "source.sqlite",
        ROOT / "ground_truth" / "truth.sqlite",
    )
    if args.command == "audit":
        try:
            audit(conn, ACTIONS_PATH)
        except (EOFError, KeyboardInterrupt):
            print("\n중단 — 다시 실행하면 이어서 한다")
        return
    gw = Gateway(load_config(), JsonlLog(LLM_LOG), "runtime")
    print(f"처리 정답 라벨 {label(gw, conn, ACTIONS_PATH)}")


if __name__ == "__main__":
    main()
