"""① 후속 에이전트 (기능 정의 §4 ①, 설계 docs/superpowers/specs/2026-11-08-followup-autoclose-design.md "PR ②").
할 일 있는 통화마다 환자 기록·예약·안내 문서를 조회해 처리안(종류 1개 + 요지 + 근거)을 낸다. 실행은 하네스(Runner),
끝은 반드시 submit_followup. 근거의 숫자(날짜·금액·횟수)가 도구 결과나 녹취록에 없으면 한 번 다시 돌리고, 또 없으면
실패로 남긴다. 처리안 실행(전화·문자·예약)은 사람 몫이고 쓰기 도구는 없다."""

import argparse
import json
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from backend import calls, db, prompts
from backend.harness.loop import AgentSpec, Runner
from backend.harness.store import RunLocked, RunStore, SqliteLlmLog
from backend.harness.tools import Tool, ToolRegistry
from backend.query_tools import query_tools
from backend.verify import verify_facts
from shared.call_labels import COUNTERPART_KO, FLAG_KO, PURPOSE_KO, URGENCY_KO
from shared.clinic import Clinic, load_clinic
from shared.followup import ACTIONS
from shared.llm import Gateway, load_config

ROOT = Path(__file__).resolve().parents[1]
AGENT, ALIAS, TASK = "call_followup", "followup", "followup"
PROMPT_VERSION = "v3"  # v2: 입력에 통화 일시·오늘 날짜 · v3: 원장님께 전달 추가(8가지)
# 평가의 질문당 도구 호출 상한과 같게(평가 설계 §3.2 — tests/backend/test_followup.py가 비교)
FOLLOWUP_MAX_TOOL_CALLS = 6
FOLLOWUP_MAX_USD = 0.5  # 실행당 비용 상한 — 넘으면 사람에게 넘긴다
SUBMIT = "submit_followup"
ATTEMPTS = 2  # 후검증 실패면 한 번 더


class SubmitArgs(BaseModel):
    action: Literal[ACTIONS]  # type: ignore[valid-type]
    summary: str = Field(min_length=1, max_length=120)
    facts: list[str] = Field(default_factory=list, max_length=6)


def registry(clinic: Clinic, searcher=None) -> tuple[ToolRegistry, tuple[str, ...]]:
    """조회 도구(환자·예약) + 문서 검색(searcher가 있을 때) + 끝내는 도구"""
    reg = ToolRegistry()
    names = []
    for t in query_tools(clinic):
        if t.name in ("query_patient", "query_schedule"):
            reg.register(t)
            names.append(t.name)
    if searcher is not None:
        from backend.rag_search import search_tool  # 검색 모델이 있을 때만

        t = search_tool(searcher)
        reg.register(t)
        names.append(t.name)
    reg.register(
        Tool(
            SUBMIT,
            "처리안을 낸다 — 처리 종류 1개, 한 줄 요지, 조회한 사실(근거). 호출하면 실행이 끝난다.",
            SubmitArgs,
            lambda ctx, args: args,
            ends_run=lambda args: "submitted",
        )
    )
    names.append(SUBMIT)
    return reg, tuple(names)


def spec(names: tuple[str, ...]) -> AgentSpec:
    return AgentSpec(
        name=AGENT,
        alias=ALIAS,
        task=TASK,
        prompt_version=PROMPT_VERSION,
        system=lambda v: prompts.load(TASK, v)["system"],
        tools=names,
        max_tool_calls=FOLLOWUP_MAX_TOOL_CALLS,
        max_usd=FOLLOWUP_MAX_USD,
    )


def input_text(call: dict, cls: dict, today: date | None = None) -> str:
    """에이전트 입력 — 통화(일시·방향·녹취록)와 활성 분류 결과, 차트 번호(통화 화면과 같은 발신번호 매칭 값),
    오늘 날짜(조회 도구가 오늘 기준이라 연도·남은 예약 계산이 맞게)"""
    lab = cls["labels"]
    flags = [FLAG_KO[f] for f in FLAG_KO if lab.get(f)]
    todo = cls.get("todo") or []
    lines = [
        f"오늘: {(today or load_clinic().today).isoformat()}",
        f"통화 일시: {(call.get('at') or '')[:16]}",
        f"차트 번호: {call.get('chart_no') or '차트 없음'}",
        f"방향: {call['direction']}",
        f"용건: {PURPOSE_KO.get(lab['primary_purpose'], lab['primary_purpose'])}"
        f" · 상대: {COUNTERPART_KO.get(lab['counterpart'], lab['counterpart'])}"
        f" · 급함: {URGENCY_KO.get(lab['urgency'], lab['urgency'])}"
        + (f" · {'·'.join(flags)}" if flags else ""),
        f"요약: {cls.get('summary') or ''}",
        "할 일:",
        *[f"- {t['text'] if isinstance(t, dict) else t}" for t in todo],
        "",
        "녹취록:",
        call["transcript"],
    ]
    return "\n".join(lines)


def active(conn, call_uid: str, include_pool: bool = False) -> tuple[dict, dict] | None:
    """(통화, 활성 분류) — 앱은 판정 풀 밖 통화만(CALLS_CTE). 평가 실행만 include_pool로 판정 풀 통화도"""
    a, _ = calls.active_versions(conn)
    sql = (
        "SELECT a.call_uid, a.direction, a.transcript, a.started_at AS at, p.chart_no"
        " FROM source.ax_calls a"
        " LEFT JOIN source.emr_patients p ON p.id = a.patient_id WHERE a.call_uid = ?"
        if include_pool
        else calls.CALLS_CTE + " SELECT * FROM calls WHERE call_uid = ?"
    )
    call = conn.execute(sql, (call_uid,)).fetchone()
    k = conn.execute(
        "SELECT id, status, labels, summary, todo FROM ax_call_classifications"
        " WHERE call_uid = ? AND model_alias = 'classify' AND prompt_version = ?",
        (call_uid, a),
    ).fetchone()
    if call is None or k is None or k["status"] != "ok" or not k["labels"]:
        return None
    cls = {
        "id": k["id"],
        "labels": json.loads(k["labels"]),
        "summary": k["summary"],
        "todo": json.loads(k["todo"] or "[]"),
    }
    return dict(call), cls


def _submitted(store: RunStore, run_id: int) -> tuple[dict | None, list[str]]:
    """제출 인자와 후검증 근거 — 입력 글(녹취록·통화 일시·오늘), 도구 인자와 결과"""
    args, sources = None, []
    for s in store.steps(run_id):
        if s["kind"] == "input":
            sources.append(str(s["result"]))
        if s["kind"] != "tool" or s["is_error"]:
            continue
        if s["tool"] == SUBMIT:
            args = s["args"]
        else:
            sources.append(json.dumps(s["args"], ensure_ascii=False, default=str))
            sources.append(json.dumps(s["result"], ensure_ascii=False, default=str))
    return args, sources


def judge_run(
    store: RunStore, run_id: int, transcript: str
) -> tuple[str, dict | None, str | None]:
    """실행 하나 → (done | unverified | failed, 제출 인자, 이유). 평가 실행(라운드 배치)도 같이 쓴다"""
    args, results = _submitted(store, run_id)
    if args is None:
        return "failed", None, store.run(run_id)["reason"] or "처리안을 내지 않음"
    if verify_facts(args["summary"], args["facts"], [transcript, *results]):
        return "done", args, None
    return "unverified", args, "후검증 실패 — 근거의 숫자가 조회 결과·녹취록에 없음"


def run_for_call(
    conn: sqlite3.Connection, gw, call_uid: str, clinic: Clinic | None = None, searcher=None
) -> dict | None:
    """활성 분류가 ok이고 할 일이 있는 통화에 처리안 — 그 분류 행에 이미 있으면 아무것도 하지 않는다"""
    found = active(conn, call_uid)
    if found is None or not found[1]["labels"].get("has_todo"):
        return None
    call, cls = found
    if conn.execute(
        "SELECT 1 FROM ax_call_followups WHERE classification_id = ?", (cls["id"],)
    ).fetchone():
        return None
    conn.commit()  # Runner.start가 거절되면 미커밋 쓰기가 되돌아가므로
    clinic = clinic or load_clinic()
    reg, names = registry(clinic, searcher)
    store = RunStore(conn)
    runner = Runner(spec(names), gw, store, reg)
    if not _close_stuck(store, runner, call_uid):
        return None  # 다른 프로세스가 지금 돌리는 중
    status, reason, args, run_id = "failed", None, None, None
    for _ in range(ATTEMPTS):  # 후검증 실패만 다시 돈다 — 제출이 없으면 다시 돌려도 같은 길
        run_id = runner.start(call_uid, input_text(call, cls, clinic.today)).run_id
        status, args, reason = judge_run(store, run_id, call["transcript"])
        if status != "unverified":
            break
    if status == "unverified":
        status = "failed"
    row = {
        "call_uid": call_uid,
        "classification_id": cls["id"],
        "action": args["action"] if status == "done" else None,
        "summary": args["summary"] if status == "done" else None,
        "facts": json.dumps(args["facts"] if status == "done" else [], ensure_ascii=False),
        "run_id": run_id,
        "status": status,
        "reason": reason,
        "created_at": datetime.now().isoformat(sep=" ", timespec="seconds"),
    }
    conn.execute(
        f"INSERT INTO ax_call_followups ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
        list(row.values()),
    )
    conn.commit()
    return row


def _close_stuck(store: RunStore, runner: Runner, call_uid: str) -> bool:
    """끊긴 실행(잠금 만료)은 실패로 닫는다 — 고유 색인 때문에 새 실행이 영영 못 생기지 않게.
    살아 있는 다른 실행이면 False"""
    row = store.conn.execute(
        "SELECT id FROM ax_agent_runs WHERE agent = ? AND target = ? AND status = 'running'",
        (AGENT, call_uid),
    ).fetchone()
    if row is None:
        return True
    try:
        with store.conn:
            store.take_over(row[0], runner.owner)
            store.finish(row[0], "failed", runner.owner, reason="끊긴 실행")
    except RunLocked:
        return False
    return True


def targets(conn: sqlite3.Connection, today: date) -> list[str]:
    """오늘 할 일에 오른 통화 중 그 분류 행의 처리안이 없는 것 — run_for_call과 같게 has_todo가 참인 분류만.
    할 일 목록은 있는데 has_todo가 거짓인 통화(엇갈림 todo_mismatch, 사람 확인 큐)는 세지 않는다 — 세면 매번
    대상으로 잡히고 run_for_call은 조용히 건너뛴다"""
    board = calls.today_board(conn, today)
    seen = {}
    for col in ("internal", "contact", "done"):
        for i in board[col]:
            seen[i["call_uid"]] = i["classification_id"]
    have = {r[0] for r in conn.execute("SELECT classification_id FROM ax_call_followups")}
    todo = {
        cid
        for cid, labels in conn.execute(
            f"SELECT id, labels FROM ax_call_classifications WHERE id IN ({','.join('?' * len(seen))})",
            list(seen.values()),
        )
        if json.loads(labels).get("has_todo")
    }
    return sorted(u for u, cid in seen.items() if cid not in have and cid in todo)


def drop_stale(conn: sqlite3.Connection, today: date) -> int:
    """오늘 할 일 통화 중 지금과 다른 프롬프트 버전으로 낸 처리안을 지운다(make followups REDO=1) — 지운 수.
    처리안은 분류 행마다 하나라 에이전트 버전만 바꾸면 다시 나오지 않는다(앱 설계 §1 `ax_call_followups`)"""
    board = calls.today_board(conn, today)
    cids = {i["classification_id"] for col in ("internal", "contact", "done") for i in board[col]}
    stale = [
        cid
        for cid, pv in conn.execute(
            "SELECT f.classification_id, r.prompt_version FROM ax_call_followups f"
            " LEFT JOIN ax_agent_runs r ON r.id = f.run_id"
        )
        if cid in cids and pv != PROMPT_VERSION
    ]
    conn.executemany(
        "DELETE FROM ax_call_followups WHERE classification_id = ?", [(c,) for c in stale]
    )
    conn.commit()
    return len(stale)


def _searcher(conn, clinic, quiet: bool = False):
    """검색 화면과 같은 모델 — 키·색인이 없으면 문서 검색 없이 돈다"""
    try:
        from backend.api import _models
        from backend.rag_search import Searcher

        if not conn.execute("SELECT 1 FROM ax_chunks LIMIT 1").fetchone():
            raise ValueError("색인 없음 — make index")
        return Searcher(conn, clinic, *_models())
    except Exception as e:  # noqa: BLE001
        if not quiet:
            print(f"문서 검색 없이 진행: {e}")
        return None


def app_searcher(conn):
    """앱 흐름(새 통화·다시 분류)도 평가·make followups와 같은 도구 구성으로"""
    return _searcher(conn, load_clinic(), quiet=True)


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m backend.followup")
    p.add_argument("command", choices=("run",))
    p.add_argument("--redo", action="store_true", help="옛 프롬프트 버전 처리안을 지우고 다시")
    args = p.parse_args(argv)
    conn = db.connect(ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite")
    clinic = load_clinic()
    if args.redo:
        print(f"옛 버전 처리안 {drop_stale(conn, clinic.today)}통화 지움")
    gw = Gateway(load_config(), SqliteLlmLog(conn), "runtime")
    searcher = _searcher(conn, clinic)
    todo = targets(conn, clinic.today)
    print(f"처리안 대상 {len(todo)}통화")
    counts = {"done": 0, "failed": 0}
    for uid in todo:
        try:
            r = run_for_call(conn, gw, uid, clinic, searcher)
        except Exception as e:  # noqa: BLE001 — 한 통화 때문에 나머지가 멈추지 않게
            conn.rollback()
            print(f"{uid}: {type(e).__name__}: {e}")
            continue
        if r:
            counts[r["status"]] += 1
    print(f"처리안 {counts}")


if __name__ == "__main__":
    main()
