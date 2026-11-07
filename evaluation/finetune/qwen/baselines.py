"""Qwen 비교 기준선 — 지금 분류기 라벨 모델(Haiku v2, 별칭 classify)과 판정기(Solar v2, 별칭 judge). 분류기 설계 "Qwen 비교".
검증 구간·dev는 앱이 낸 라벨을 app.sqlite에서 읽기만 하고(?mode=ro), 문체 dev는 앱과 같은 요청을 일반 호출로 새로 돈다(과금).
실패·폴백(별칭 첫 모델이 아닌 모델의 답)·없음은 labels None — 채점에서 오답. 플래그는 0/1.
"""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.classify import MAX_TOKENS, judge_request, label_request, valid_labels
from shared.call_labels import FLAGS

VERSION = "v2"
MODELS = {  # 이름 → (앱 결과 표, 별칭)
    "haiku_v2": ("ax_call_classifications", "classify"),
    "solar_v2": ("ax_model_judgments", "judge"),
}
WORKERS = 4


def _flags01(labels: dict) -> dict:
    return {k: int(v) if k in FLAGS else v for k, v in labels.items()}


def from_app(app_db: Path, ids: set[str]) -> dict[str, dict[str, dict | None]]:
    """→ {모델 이름: {call_uid: labels | None}} — status='ok'이고 라벨 호출에 폴백이 없을 때만 라벨"""
    conn = sqlite3.connect(f"file:{app_db}?mode=ro", uri=True)
    out = {}
    for name, (table, _) in MODELS.items():
        got = dict(
            conn.execute(
                f"SELECT t.call_uid, t.labels FROM {table} t JOIN ax_llm_calls l"
                " ON l.id = t.llm_call_id"
                " WHERE t.prompt_version = ? AND t.status = 'ok' AND l.fallback_used = 0",
                (VERSION,),
            ).fetchall()
        )
        out[name] = {i: _flags01(json.loads(got[i])) if i in got else None for i in ids}
    conn.close()
    return out


def _call(gw, name: str, row: dict):
    """앱과 같은 요청 — judge v2는 머리말 effort로 max_tokens를 늘린다(backend.classify.judge와 같음)"""
    request = label_request if name == "haiku_v2" else judge_request
    req = {"max_tokens": MAX_TOKENS} | request(row["transcript"], row["direction"], VERSION)
    alias = MODELS[name][1]
    return gw.complete(alias, task=f"qwen-baseline-{alias}", prompt_version=VERSION, **req)


def latest(path: Path) -> dict[tuple[str, str], dict | None]:
    """(id, 모델) → 마지막 줄의 labels — 다시 시도한 줄이 앞 실패를 덮는다"""
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    return {(r["id"], r["model"]): r["labels"] for r in rows}


def run_style(rows: list[dict], out_path: Path, gw) -> None:
    """rows × 모델 둘을 일반 호출(동시 WORKERS) → out_path에 {id, set, model, labels, fallback, cost} 덧붙임.
    labels가 있는 (id, 모델)은 건너뛰고, 실패·폴백 건은 다시 돈다"""
    done = {k for k, v in latest(out_path).items() if v is not None}
    todo = [(r, m) for r in rows for m in MODELS if (r["id"], m) not in done]

    def one(job):
        row, name = job
        res = _call(gw, name, row)
        ok = res.ok and not res.fallback_used and valid_labels(res.data)
        return {"id": row["id"], "set": row.get("set"), "model": name,
                "labels": _flags01(res.data) if ok else None, "fallback": res.fallback_used,
                "cost": res.cost_usd, "error": res.error}  # fmt: skip

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(WORKERS) as ex, out_path.open("a") as f:
        for r in ex.map(one, todo):
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
            f.flush()
