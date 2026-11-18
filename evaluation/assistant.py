"""AI 비서 시나리오 실행·채점 (AI 비서 설계 §7·§7.1, 채점 함수는 evaluation/assistant_score.py).
시나리오(ground_truth/assistant_scenarios.json)마다 새 앱 DB(원본 source 읽기 전용, 예약 원함 조회에 필요한 활성
분류 행·설정만 앱 DB에서 읽어 옮김)에서 대본 턴을 `backend/assistant.py turn`에 차례로 넣는다. 초안이 생겨도 승인하지
않는다 — 끝 상태 = 초안. 같은 칸을 잡는 시나리오끼리 섞이지 않는다. 시나리오 DB는 evaluation/assistant_dbs/<split>/<id>/에 남는다(사람이 결과를 확인한 뒤 지움). 결과는 evaluation/assistant_runs.jsonl(dev)·
assistant_test_runs.jsonl(test)에 쌓고 같은 id·버전·별칭이면 건너뛴다(이어 돎). 앱 DB에는 쓰지 않는다.
LLM 호출 기록은 시나리오 DB가 아니라 실행 전체가 함께 쓰는 LLM_LOG(JsonlLog)에 남긴다 — 일일 상한이 시나리오마다
새로 시작하지 않고, 턴 비용은 그 턴 동안 기록된 호출 비용 합(③ 위임 호출 포함)이다(RunLog).
검색 색인은 시나리오 DB에 없어 ask_records는 "색인 없음"으로 답 단계에 넘어간다(도구 선택·인자만 채점한다).
기준선: lookup 유형 첫 턴을 ③ chat.chat에 넣은 도구 선택(보고만).

  python -m evaluation.assistant run --split dev [--tool-alias chat] [--reply-alias chat] [--baseline]  # 과금
  python -m evaluation.assistant score --split dev|test   # 저장된 줄로 채점(test는 사람, 평가 설계 §2.7)"""

import argparse
import json
import shutil
import sqlite3
import tempfile
from datetime import date
from pathlib import Path

from backend import assistant, chat, db
from evaluation.assistant_score import score_run, score_turn
from generator.paths import output_paths
from shared.clinic import load_clinic

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = ROOT / "ground_truth" / "assistant_scenarios.json"
RUNS = {
    "dev": ROOT / "evaluation" / "assistant_runs.jsonl",
    "test": ROOT / "evaluation" / "assistant_test_runs.jsonl",
}
KEEP_DBS = {  # 시나리오 DB를 남기는 곳(깃 밖) — 결과를 사람이 확인한 뒤 지운다
    "dev": ROOT / "evaluation" / "assistant_dbs" / "dev",
    "test": ROOT / "evaluation" / "assistant_dbs" / "test",
}
LLM_LOG = {  # ax_llm_calls에 쓰지 않는다 — 시나리오 DB는 확인 뒤 지운다
    "dev": ROOT / "evaluation" / "assistant_llm.jsonl",
    "test": ROOT / "evaluation" / "assistant_test_llm.jsonl",
}
PART = "데스크"
COPIED = (
    "ax_settings",
    "ax_call_classifications",
)  # 예약 원함 조회(unbooked_requests)가 읽는 앱 표


def load_scenarios(split: str) -> list[dict]:
    rows = json.loads(SCENARIOS.read_text(encoding="utf-8"))
    return [s for s in rows if s["split"] == split]


def load_runs(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def _key(row: dict) -> tuple:
    return (row["id"], row["version"], row["tool_alias"], row["reply_alias"])


def run(
    scenarios: list[dict],
    *,
    out: Path,
    version: str,
    tool_alias: str,
    reply_alias: str,
    run_one,
) -> dict:
    """줄이 없는 시나리오만 run_one(시나리오) → {turns: [...]}로 돌려 한 줄씩 덧붙인다(이어 돎)"""
    meta = {"version": version, "tool_alias": tool_alias, "reply_alias": reply_alias}
    # 오류로 끊긴 줄은 끝난 것으로 치지 않는다 — 이어 돌 때 다시 돌린다(같은 id는 나중 줄이 채점에 쓰임)
    done = {_key(r) for r in load_runs(out) if not any("error" in t for t in r["turns"])}
    n = skipped = 0
    for sc in scenarios:
        if _key({"id": sc["id"], **meta}) in done:
            skipped += 1
            continue
        row = {"id": sc["id"], "type": sc["type"], **meta, **run_one(sc)}
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        n += 1
    return {"done": n, "skipped": skipped}


def scenario_db(tmp: Path, app_db: Path, source_db: Path) -> sqlite3.Connection:
    """시나리오 하나를 돌릴 새 앱 DB — 스키마만 만들고 활성 분류 설정·행(과 그 LLM 호출 행)을 앱 DB에서 읽어 옮긴다"""
    conn = db.connect(tmp / "app.sqlite", source_db)
    db.init_schema(conn)
    conn.execute("ATTACH DATABASE ? AS real", (f"{Path(app_db).resolve().as_uri()}?mode=ro",))
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute(
        "INSERT INTO ax_llm_calls SELECT * FROM real.ax_llm_calls WHERE id IN"
        " (SELECT llm_call_id FROM real.ax_call_classifications UNION"
        "  SELECT summary_llm_call_id FROM real.ax_call_classifications"
        "  WHERE summary_llm_call_id IS NOT NULL)"
    )
    for table in COPIED:
        conn.execute(f"INSERT INTO {table} SELECT * FROM real.{table}")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("DETACH DATABASE real")
    return conn


def end_state(conn, conversation_id: int, turn: int) -> dict | None:
    """그 턴에 만든 마지막 초안 → {draft: {chart_no, date, start, staff_role}} (없으면 None)"""
    row = conn.execute(
        "SELECT p.chart_no, w.scheduled_at, s.role FROM ax_emr_writes w"
        " JOIN source.emr_patients p ON p.id = w.patient_id"
        " JOIN source.emr_staff s ON s.id = w.staff_id"
        " WHERE w.conversation_id = ? AND w.turn = ? AND w.status = 'draft' ORDER BY w.id DESC",
        (conversation_id, turn),
    ).fetchone()
    if row is None:
        return None
    day, _, start = row["scheduled_at"].replace("T", " ").partition(" ")
    return {
        "draft": {
            "chart_no": row["chart_no"],
            "date": day,
            "start": start[:5],
            "staff_role": row["role"],
        }
    }


class RunLog:
    """실행 전체가 함께 쓰는 호출 기록 — 안쪽 기록(JsonlLog)에 그대로 넘기고(일일 상한 spent도 거기서) 쓴 비용 합을
    total에 센다. 턴 비용 = 그 턴 앞뒤 total의 차(답 단계·③ 위임 호출까지 그 턴에 기록된 모든 호출)"""

    def __init__(self, inner):
        self.inner, self.total = inner, 0.0

    def write(self, record: dict):
        self.total += record.get("cost_usd") or 0.0
        return self.inner.write(record)

    def spent(self, day: date, scope: str) -> float:
        return self.inner.spent(day, scope)


def run_scenario(
    sc: dict, *, app_db: Path, source_db: Path, gw, clinic, keep: Path | None = None
) -> dict:
    """한 시나리오의 대본 턴을 차례로 — 한 턴이 예외로 끝나면 거기서 멈춘다(채점은 실패로 셈).
    gw는 실행 전체가 함께 쓰는 게이트웨이(기록 = RunLog). keep이 있으면 시나리오 DB를 keep/<id>/에 남긴다
    (같은 id면 새로 덮어씀), 없으면 임시 폴더째 버린다"""
    if keep is not None:
        shutil.rmtree(keep / sc["id"], ignore_errors=True)
        (keep / sc["id"]).mkdir(parents=True)
    with tempfile.TemporaryDirectory() as tmp:
        conn = scenario_db(keep / sc["id"] if keep is not None else Path(tmp), app_db, source_db)
        try:
            cid, turns = None, []
            for t in sc["turns"]:
                before = gw.log.total
                try:
                    out = assistant.turn(
                        conn,
                        gw,
                        assistant.NO_INDEX_SEARCHER,
                        clinic,
                        conversation_id=cid,
                        text=t["user"],
                        part=PART,
                    )
                except Exception as e:
                    cost = round(gw.log.total - before, 6)
                    turns.append(
                        {"tool_calls": [], "end_state": None, "cost_usd": cost, "error": repr(e)}
                    )
                    break
                cid = out["conversation_id"]
                turns.append(
                    {
                        "tool_calls": out["tool_calls"],
                        "end_state": end_state(conn, cid, out["seq"] - 1),
                        "status": out["status"],
                        "text": out["text"],  # 채점에는 안 쓰고 사람이 실패를 볼 때
                        "cards": out["cards"],
                        "cost_usd": round(gw.log.total - before, 6),
                    }
                )
            return {"turns": turns}
        finally:
            conn.close()


class _NoSearch:
    """기준선 — 도구 선택만 보므로 검색은 빈 결과"""

    replaced_by: dict = {}

    def search(self, *a, **kw):
        return {}

    def drain_usage(self):
        return []


def baseline(scenarios: list[dict], *, app_db: Path, source_db: Path, gw, clinic) -> dict:
    """lookup 유형 첫 턴을 ③ chat.chat에 넣은 도구 선택 — {n, tools_ok}(보고만. gold 도구 집합이 불린 도구에 듦)"""
    ok = n = 0
    for sc in scenarios:
        if sc["type"] != "lookup":
            continue
        with tempfile.TemporaryDirectory() as tmp:
            conn = scenario_db(Path(tmp), app_db, source_db)
            try:
                got = chat.chat(
                    conn,
                    gw,
                    _NoSearch(),
                    clinic,
                    sc["turns"][0]["user"],
                    is_eval=True,
                )
            finally:
                conn.close()
        n += 1
        ok += score_turn(sc["turns"][0], got["tool_calls"], None)["tools_ok"]
    return {"n": n, "tools_ok": ok / n if n else 0.0}


def score(split: str, tool_alias: str | None = None, reply_alias: str | None = None) -> dict:
    """저장된 줄 중 지금 버전·별칭(없으면 기본)으로 채점 — {version, tool_alias, reply_alias} + score_run 결과"""
    version = f"{assistant.PROMPT_VERSION}/{assistant.REPLY_VERSION}"
    tool_alias = tool_alias or assistant.TOOL_ALIAS
    reply_alias = reply_alias or assistant.REPLY_ALIAS
    meta = {"version": version, "tool_alias": tool_alias, "reply_alias": reply_alias}
    rows = [r for r in load_runs(RUNS[split]) if all(r[k] == v for k, v in meta.items())]
    return meta | score_run(load_scenarios(split), rows)


def render(r: dict) -> list[str]:
    """평가 리포트 절 — 시나리오 채점(AI 비서 설계 §7.1)"""

    def pct(x):
        return f"{x * 100:.0f}%"

    out = [
        "## AI 비서 시나리오",
        "",
        f"버전 {r['version']} · 도구 {r['tool_alias']} · 답 {r['reply_alias']} · 시나리오 {r['n']}개",
        "",
        f"- 도구 선택 {pct(r['tool_selection'])} · 인자 {pct(r['arg_accuracy'])}"
        f" · 과제 성공 {pct(r['task_success'])} · 안전하지 않은 쓰기 {r['unsafe_writes']}건"
        f" (막힌 쓰기 시도 {r['blocked_write_attempts']}건)",
        f"- 턴당 비용 ${r['cost_per_turn']:.4f} · 턴당 도구 호출 {r['tool_calls_per_turn']:.1f}",
    ]
    for t, v in r["by_type"].items():
        out.append(
            f"  - {t} {v['n']}개: 과제 성공 {pct(v['task_success'])}"
            f" · 도구 선택 {pct(v['tool_selection'])}"
        )
    return [*out, ""]


def make_gateway(split: str):
    """앱 게이트웨이와 같은 시간 제한·재시도(backend/harness/llm.py), 기록만 LLM_LOG(RunLog)"""
    import anthropic

    from backend.harness.llm import APP_RETRIES, APP_TIMEOUT
    from shared.llm import Gateway, JsonlLog, load_config

    client = anthropic.Anthropic(timeout=APP_TIMEOUT, max_retries=APP_RETRIES)
    return Gateway(load_config(), RunLog(JsonlLog(LLM_LOG[split])), "runtime", client)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m evaluation.assistant")
    p.add_argument("command", choices=("run", "score"))
    p.add_argument("--split", choices=("dev", "test"), required=True)
    p.add_argument("--tool-alias", default=assistant.TOOL_ALIAS)
    p.add_argument("--reply-alias", default=assistant.REPLY_ALIAS)
    p.add_argument("--baseline", action="store_true", help="lookup 첫 턴 ③ 도구 선택도 보고")
    args = p.parse_args(argv)
    if args.command == "run":
        assistant.TOOL_ALIAS, assistant.REPLY_ALIAS = args.tool_alias, args.reply_alias
        paths = output_paths()
        kw = {
            "app_db": paths["app"],
            "source_db": paths["source"],
            "gw": make_gateway(args.split),
            "clinic": load_clinic(),
        }
        scenarios = load_scenarios(args.split)
        res = run(
            scenarios,
            out=RUNS[args.split],
            version=f"{assistant.PROMPT_VERSION}/{assistant.REPLY_VERSION}",
            tool_alias=args.tool_alias,
            reply_alias=args.reply_alias,
            run_one=lambda sc: run_scenario(sc, **kw, keep=KEEP_DBS[args.split]),
        )
        print(f"시나리오 {res['done']}개 실행, {res['skipped']}개 건너뜀")
        if args.baseline:
            b = baseline(scenarios, **kw)
            print(f"기준선(③ chat, lookup 첫 턴 {b['n']}개) 도구 선택 {b['tools_ok'] * 100:.0f}%")
    print("\n".join(render(score(args.split, args.tool_alias, args.reply_alias))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
