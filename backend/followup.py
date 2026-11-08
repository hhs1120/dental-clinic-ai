"""① 후속 에이전트 (기능 정의 §4 ①, 설계 docs/superpowers/specs/2026-11-08-followup-autoclose-design.md "PR ②").
할 일 있는 통화마다 환자 기록·예약·안내 문서를 조회해 처리안(종류 1개 + 요지 + 근거)을 낸다. 실행은 하네스(Runner),
끝은 반드시 submit_followup. 근거의 숫자(날짜·금액·횟수)가 도구 결과나 녹취록에 없으면 한 번 다시 돌리고, 또 없으면
실패로 남긴다. 처리안 실행(전화·문자·예약)은 사람 몫이고 쓰기 도구는 없다."""

import argparse
import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from backend import calls, db, prompts
from backend.harness.loop import AgentSpec, Runner
from backend.harness.store import RunStore, SqliteLlmLog
from backend.harness.tools import Tool, ToolRegistry
from backend.query_tools import query_tools
from shared.call_labels import COUNTERPART_KO, FLAG_KO, PURPOSE_KO, URGENCY_KO
from shared.clinic import Clinic, load_clinic
from shared.followup import ACTIONS
from shared.llm import Gateway, load_config

ROOT = Path(__file__).resolve().parents[1]
AGENT, ALIAS, TASK = "call_followup", "followup", "followup"
PROMPT_VERSION = "v1"
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


def input_text(call: dict, cls: dict) -> str:
    """에이전트 입력 — 통화(방향·녹취록)와 활성 분류 결과, 차트 번호(통화 화면과 같은 발신번호 매칭 값)"""
    lab = cls["labels"]
    flags = [FLAG_KO[f] for f in FLAG_KO if lab.get(f)]
    todo = cls.get("todo") or []
    lines = [
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


def _numbers(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"\d+", text.replace(",", ""))}


def verify_facts(summary: str, facts: list[str], sources: list[str]) -> bool:
    """처리안의 숫자가 모두 도구 결과나 녹취록에 있으면 참(④ 후검증과 같은 방식 — 지어낸 날짜·금액을 막는다)"""
    have = set().union(*(_numbers(s) for s in sources)) if sources else set()
    return _numbers(" ".join([summary, *facts])) <= have


def active(conn, call_uid: str, include_pool: bool = False) -> tuple[dict, dict] | None:
    """(통화, 활성 분류) — 앱은 판정 풀 밖 통화만(CALLS_CTE). 평가 실행만 include_pool로 판정 풀 통화도"""
    a, _ = calls.active_versions(conn)
    sql = (
        "SELECT a.call_uid, a.direction, a.transcript, p.chart_no FROM source.ax_calls a"
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
    """제출 인자와 이 실행의 도구 결과(후검증 근거)"""
    args, results = None, []
    for s in store.steps(run_id):
        if s["kind"] != "tool":
            continue
        if s["tool"] == SUBMIT and not s["is_error"]:
            args = s["args"]
        elif not s["is_error"]:
            results.append(json.dumps(s["result"], ensure_ascii=False, default=str))
    return args, results


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
    reg, names = registry(clinic or load_clinic(), searcher)
    store = RunStore(conn)
    runner = Runner(spec(names), gw, store, reg)
    status, reason, args, run_id = "failed", None, None, None
    for _ in range(ATTEMPTS):  # 후검증 실패만 다시 돈다 — 제출이 없으면 다시 돌려도 같은 길
        run_id = runner.start(call_uid, input_text(call, cls)).run_id
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


def targets(conn: sqlite3.Connection, today: date) -> list[str]:
    """오늘 할 일에 오른 통화 중 그 분류 행의 처리안이 없는 것"""
    board = calls.today_board(conn, today)
    seen = {}
    for col in ("internal", "contact", "done"):
        for i in board[col]:
            seen[i["call_uid"]] = i["classification_id"]
    have = {r[0] for r in conn.execute("SELECT classification_id FROM ax_call_followups")}
    return sorted(u for u, cid in seen.items() if cid not in have)


def _searcher(conn, clinic):
    """검색 화면과 같은 모델 — 키·색인이 없으면 문서 검색 없이 돈다"""
    try:
        from backend.api import _models
        from backend.rag_search import Searcher

        return Searcher(conn, clinic, *_models())
    except Exception as e:  # noqa: BLE001
        print(f"문서 검색 없이 진행: {e}")
        return None


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m backend.followup")
    p.add_argument("command", choices=("run",))
    p.parse_args(argv)
    conn = db.connect(ROOT / "data" / "app.sqlite", ROOT / "data" / "source.sqlite")
    clinic = load_clinic()
    gw = Gateway(load_config(), SqliteLlmLog(conn), "runtime")
    searcher = _searcher(conn, clinic)
    todo = targets(conn, clinic.today)
    print(f"처리안 대상 {len(todo)}통화")
    counts = {"done": 0, "failed": 0}
    for uid in todo:
        r = run_for_call(conn, gw, uid, clinic, searcher)
        if r:
            counts[r["status"]] += 1
    print(f"처리안 {counts}")


if __name__ == "__main__":
    main()
