"""분류·판정 일괄 실행 (분류기 설계 "실행"). 대상은 source.ax_calls의 녹취록 있는 생성 통화 중 그 버전 행이 ok가 아닌 것
(없거나 failed), call_uid 순. use_batch면 Anthropic 별칭은 Batch(단가 절반), Batch가 없는 프로바이더(Upstage)는 일반 호출.
Batch 결과에 빠진 건(처리 안 됨)은 저장하지 않아 다음 실행의 대상으로 남는다. 새로 추가한 통화는 그때 분류한다(backend/calls.py)
uv run --env-file .env python -m backend.classify_batch {classify|judge} --version v1 [--limit N] [--no-batch]
(judge는 로컬 Qwen이라 uv run --group finetune — config/llm.yaml judge, Qwen 판정기 설계)
[--shard K/N] — 일반 호출이 느릴 때 프로세스 N개가 대상을 나눠 돈다(몫은 생성 통화 전체의 call_uid 순 위치로 고정)
uv run python -m backend.classify_batch disagreements --a v1 --b v1
uv run --env-file .env python -m backend.classify_batch blind --a v1 --b v2 --version v1
"""

import argparse
import time
from pathlib import Path
from typing import Literal

from backend import blind, db, disagreements, judge_queue
from backend import classify as cl
from backend.calls import active_versions

ROOT = Path(__file__).resolve().parents[1]
TABLES = {"classify": "ax_call_classifications", "judge": "ax_model_judgments"}


def targets(
    conn, task: str, version: str, limit: int | None, shard: tuple[int, int] | None = None
) -> list:
    """별칭 이름은 작업 이름과 같다(classify·judge). shard=(k, n)이면 녹취록 있는 생성 통화 전체의 call_uid 순
    위치 % n == k인 것만 — 끝난 건과 상관없이 몫이 고정돼 늦게 띄우거나 다시 띄워도 겹치지 않는다"""
    sql = (
        "SELECT call_uid, direction, transcript, call_uid IN"
        f" (SELECT call_uid FROM {TABLES[task]}"
        "  WHERE model_alias = ? AND prompt_version = ? AND status = 'ok') AS done"
        " FROM source.ax_calls WHERE source = 'generated' AND transcript IS NOT NULL"
        " ORDER BY call_uid"
    )
    rows = [
        r
        for i, r in enumerate(conn.execute(sql, (task, version)))
        if not r["done"] and (shard is None or i % shard[1] == shard[0])
    ]
    return rows[:limit] if limit else rows


def _shard(text: str) -> tuple[int, int]:
    k, n = (int(x) for x in text.split("/"))
    if not 0 <= k < n:
        raise ValueError(f"--shard {text}: 0 <= K < N")
    return k, n


def _batchable(gw, alias: str) -> bool:
    first = gw.config["aliases"][alias][0]
    return gw.config["models"][first].get("provider", "anthropic") == "anthropic"


def _submit(gw, alias: str, version: str, reqs: dict) -> str:
    return gw.submit_batch(
        alias,
        [{"custom_id": uid, **r} for uid, r in reqs.items()],
        task=alias,
        prompt_version=version,
        max_tokens=cl.MAX_TOKENS,
    )


def _collect(gw, batch_id: str, alias: str, version: str, schema, poll_seconds: int) -> dict:
    while gw.batch_status(batch_id) != "ended":
        time.sleep(poll_seconds)
    return gw.batch_results(batch_id, alias, task=alias, prompt_version=version, schema=schema)


def run(
    conn,
    gw,
    task: Literal["classify", "judge"],
    version: str,
    *,
    use_batch: bool,
    limit: int | None = None,
    poll_seconds: int = 30,
    shard: tuple[int, int] | None = None,
) -> dict:
    """반환 {"done": ok로 저장, "failed": failed로 저장, "missing": Batch에서 빠져 저장 안 함}"""
    if task == "judge" and not cl.judge_model_ok(gw, version):
        raise SystemExit(
            f"판정기 {version}은 머리말 model이 지금 judge 별칭과 달라 돌리지 않는다 — 옛 기록은 읽기만"
        )
    rows = targets(conn, task, version, limit, shard)
    if task == "classify":
        parts = {"classify": cl.label_request, "summarize": cl.summary_request}
    else:
        parts = {"judge": cl.judge_request}
    reqs = {
        alias: {r["call_uid"]: make(r["transcript"], r["direction"], version) for r in rows}
        for alias, make in parts.items()
    }
    batched = {a for a in parts if use_batch and rows and _batchable(gw, a)}
    batch_ids = {a: _submit(gw, a, version, reqs[a]) for a in batched}
    results = {}
    for a, bid in batch_ids.items():
        schema = reqs[a][rows[0]["call_uid"]]["schema"]
        results[a] = _collect(gw, bid, a, version, schema, poll_seconds)
    out = {"done": 0, "failed": 0, "missing": 0}
    for r in rows:  # 일반 호출은 통화마다 저장 — 도중에 멈춰도 끝난 통화는 남는다
        uid = r["call_uid"]
        got = [
            results[a].get(uid)
            if a in batched
            else gw.complete(
                a, task=a, prompt_version=version, **{"max_tokens": cl.MAX_TOKENS, **reqs[a][uid]}
            )
            for a in parts
        ]
        if any(g is None for g in got):
            out["missing"] += 1
            continue
        if task == "classify":
            c = cl.classification_from(*got)
            cl.save_classification(conn, uid, version, c)
            ok = c.ok
        else:
            j = cl.judgment_from(got[0])
            cl.save_judgment(conn, uid, version, j)
            ok = j.ok
        out["done" if ok else "failed"] += 1
    return out


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m backend.classify_batch")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name in TABLES:
        s = sub.add_parser(name)
        s.add_argument("--version", required=True)
        s.add_argument("--limit", type=int)
        s.add_argument("--no-batch", action="store_true")
        s.add_argument("--shard", type=_shard)
    d = sub.add_parser("disagreements")
    d.add_argument("--a", required=True)
    d.add_argument("--b", required=True)
    bl = sub.add_parser(
        "blind"
    )  # LLM 쌍 비교(평가 설계 §2.4) — dev 블라인드 통화, 분류 버전 a vs b
    bl.add_argument("--a", required=True)
    bl.add_argument("--b", required=True)
    bl.add_argument("--version", required=True)  # blind_judge 프롬프트 버전
    args = p.parse_args(argv)
    conn = db.connect(ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite")
    db.init_schema(conn)
    if args.cmd == "disagreements":
        print(f"엇갈림 {disagreements.compute(conn, args.a, args.b)}건")
        print(
            f"판정 큐 추가 {judge_queue.refresh_if_active(conn, args.a, args.b)}건"
        )  # 앱 설계 §6
        if (args.a, args.b) == active_versions(conn):  # 판정기를 바꾼 뒤 옛 대기 건 정리
            print(f"판정 큐 정리 {judge_queue.prune(conn, args.a, args.b)}건")
        return
    from backend.harness.llm import make_gateway

    if args.cmd == "blind":
        from shared.clinic import load_clinic

        gw = make_gateway(conn)
        print(blind.run_pairs(conn, gw, load_clinic(), args.a, args.b, args.version))
        return
    out = run(
        conn,
        make_gateway(conn),
        args.cmd,
        args.version,
        use_batch=not args.no_batch,
        limit=args.limit,
        shard=args.shard,
    )
    print(out)


if __name__ == "__main__":
    main()
