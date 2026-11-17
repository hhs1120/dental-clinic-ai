"""AI 비서 에이전트 (AI 비서 설계 §4.1·§6·§7.1). 대화 1턴 = 실행 1개(ax_agent_runs, target = "대화 id:턴 번호").
한 턴은 도구 단계(하네스 실행, 끝내는 도구 done_tools) → ③ 위임(ask_records에 남긴 질문마다 chat.chat) → 답 단계
(게이트웨이 1회, 구조화 출력 {text, cards, cited_ids}, 같은 실행에 ref로 묶음)다. 두 단계의 별칭을 따로 받는다
(TOOL_ALIAS·REPLY_ALIAS — 다음 묶음에서 도구 단계만 로컬 모델로). 답의 숫자·인용이 이번 턴 도구 결과·입력 밖이면
이유를 붙여 답 단계를 한 번 다시 부르고, 또 어긋나면 고정 문구로 끝낸다(③과 같은 규칙). 이번 턴에 성공한 초안이
없는데 답이 초안·예약을 만든 것처럼 쓰거나(DRAFT_CLAIM_RE), 초안이 있어도 예약을 확정·승인한 것처럼 쓰면
(CONFIRM_CLAIM_RE) 같은 방식으로 다시 부른다. 카드는 이번 턴 결과에 나온
대상만 남기고 내용은 코드가 채우며, 성공한 초안 카드는 답이 빠뜨려도·답 단계가 실패해도 코드가 붙인다.
대화·메시지(그 턴의 도구 호출 포함)는 ax_assistant_* 에 저장한다(§6)."""

import json
import re
import sqlite3
from datetime import datetime

from backend import assistant_tools, calls, chat, documents, prompts
from backend.assistant_tools import assistant_tools as make_tools
from backend.harness.loop import TRUST_NOTICE, AgentSpec, Runner, external
from backend.harness.store import RunStore
from backend.harness.tools import Tool, ToolRegistry
from backend.search_cards import card
from backend.verify import allowed_numbers, numbers_ok
from shared.clinic import Clinic
from shared.llm import BudgetExceeded

AGENT, TASK, REPLY_TASK = "assistant", "assistant", "assistant_reply"
TOOL_ALIAS = "chat"  # 도구 단계 — 다음 묶음에서 로컬 모델 자리(§7.1)
REPLY_ALIAS = "chat"  # 답 단계
PROMPT_VERSION = "v1"
REPLY_VERSION = "v1"
MAX_TOOL_CALLS = (
    chat.CHAT_MAX_TOOL_CALLS
)  # 턴당 도구 호출(done_tools 포함) — ③ 값에서 시작, dev 실측으로 정함(§4.1)
MAX_USD = chat.CHAT_MAX_USD
FAILED_TEXT = "확인된 결과로 답하지 못했습니다"
WEEKDAYS = "월화수목금토일"
DONE, ASK, DRAFT = "done_tools", "ask_records", "draft_appointment"
TITLE_LEN = 40  # 대화 제목 = 첫 말 앞부분(LLM 없음, §6)
SUMMARY_LEN = 300  # 메시지에 남기는 도구 결과 요약 글자 수(§5.3 "재생성 때" 학습 재료)
CARD_TYPES = ("patient", "call", "draft", "claim", "recon_day", "source")
# 결과 안 이 키의 값이 그 종류 카드의 대상 id
ID_KEYS = {
    "chart_no": "patient",
    "call_uid": "call",
    "source_call_uid": "call",
    "claim_id": "claim",
}
CITE, NUMBER = "assistant.cite_outside", "assistant.number_unverified"
DRAFT_CLAIM = "assistant.draft_unbacked"
FAILED_STATUS = {CITE: "cite_failed", NUMBER: "number_failed", DRAFT_CLAIM: "draft_failed"}
_KEEP = r"\s*(두었|뒀|놓았|놨|드렸)"  # "잡아 두었습니다"·"넣어 드렸습니다"처럼 붙는 보조 동사
# 초안·예약을 만든(잡은·넣은·생긴) 것처럼 쓴 말 — 이번 턴에 성공한 draft_appointment가 없는데 이 말이 있으면 어긋남.
# "예약을 원했습니다"·"초안을 만들까요?"·"예약이 잡혀 있어요"·"예약을 하셨습니다"처럼 일을 끝냈다는 말이 아니면
# 걸리지 않게 끝난 동작 어미(능동·피동)만 본다
DRAFT_CLAIM_RE = re.compile(
    r"(초안|예약)\s*(을|를|도|이|가)?\s*("
    rf"(만들|잡|넣|등록해|생성해|작성해)(었|았){_KEEP}?|(만들어|잡아|넣어|등록해|생성해|작성해){_KEEP}"
    r"|생성했|작성했|등록했|했습니다|했어요|해\s*(두었|뒀|드렸)"
    r"|만들어졌|잡혔|(생성|작성|등록)?(되었|됐)(습|어))"
)
# 예약을 확정·승인(완료)한 것처럼 쓴 말 — 비서에는 승인 도구가 없어(승인은 직원이 초안 카드에서, §5.2) 초안을 만든
# 턴에도 언제나 어긋남. "승인하시면 확정됩니다"·"확정되어 있습니다"처럼 끝나지 않은 말은 걸리지 않는다
CONFIRM_CLAIM_RE = re.compile(
    rf"(확정|승인)(했|해{_KEEP})"
    r"|(예약|초안)\s*(을|를|이|가|도|은|는)?\s*(확정|승인|완료)\s*(처리)?\s*(되었|됐)"
)
EVIDENCE_SKIP = (*chat.EVIDENCE_SKIP, "question")  # 모델이 쓴 자유 글은 숫자 근거가 아니다
RETRY_REASON = {
    CITE: "cited_ids가 이번 턴 기록 검색·차트 조회 결과 밖임.",
    NUMBER: "답의 숫자가 이번 턴 도구 결과·입력에 없음.",
    DRAFT_CLAIM: "이번 턴에 만든 예약 초안이 없는데 답이 초안·예약을 만든 것처럼 썼거나, 예약을 확정·승인한"
    " 것처럼 씀. 지난 예약·이미 있는 예약은 그대로 설명해도 되지만, 이번 턴 결과에 초안 카드가 없으면 무엇도 새로"
    " 만들었다·잡았다고 쓰지 않는다. 확정·승인은 직원이 초안 카드에서 하므로 했다고 쓰지 않는다.",
}
RETRY_HINT = " 이번 턴 결과에 있는 사실·숫자만 그대로 써서 다시 답한다."
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "text": {"type": "string"},
        "cards": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": list(CARD_TYPES)},
                    "id": {"type": "string"},
                },
                "required": ["type", "id"],
                "additionalProperties": False,
            },
        },
        "cited_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["text", "cards", "cited_ids"],
    "additionalProperties": False,
}


def _now() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


def _with_event(tool: Tool, on_event) -> Tool:
    def run(ctx, args):
        on_event({"type": "step", "tool": tool.name, "args": args})
        return tool.fn(ctx, args)

    return Tool(tool.name, tool.description, tool.args, run, tool.writes, tool.ends_run)


def registry(
    clinic: Clinic, searcher, ctx_turn: dict, on_event=None
) -> tuple[ToolRegistry, tuple]:
    reg = ToolRegistry()
    tools = make_tools(clinic, searcher, ctx_turn)
    for t in tools:
        reg.register(_with_event(t, on_event) if on_event and t.name != DONE else t)
    return reg, tuple(t.name for t in tools)


def spec(names: tuple) -> AgentSpec:
    return AgentSpec(
        name=AGENT,
        alias=TOOL_ALIAS,
        task=TASK,
        prompt_version=PROMPT_VERSION,
        system=lambda v: prompts.load(TASK, v)["system"],
        tools=names,
        max_tool_calls=MAX_TOOL_CALLS,
        max_usd=MAX_USD,
        end_tools=(DONE,),  # 글로 끝나면 한 번 되돌린다
    )


def card_detail(conn: sqlite3.Connection, type_: str, id_: str, clinic: Clinic | None = None):
    """카드 내용(코드가 채움) — 대상이 없으면 None. recon_day만 clinic이 필요하다. 초안의 source_call_pool은 근거
    통화가 판정 풀이면 1 — 판정 풀 통화는 통화 화면에 없어 화면이 링크를 걸지 않는다(AI 비서 설계 §8)"""
    if type_ == "patient":
        r = conn.execute(
            "SELECT chart_no, name, birth_date FROM source.emr_patients WHERE chart_no = ?", (id_,)
        ).fetchone()
        return dict(r) if r else None
    if type_ == "call":
        rows = calls._rows(conn, "calls.call_uid = :u", {"u": id_})
        if not rows:
            return None
        c = calls._card(rows[0], {})
        return {
            k: c[k]
            for k in ("call_uid", "at", "direction", "name", "chart_no", "purpose", "summary")
        }
    if type_ == "draft":
        r = (
            id_.isdigit()
            and conn.execute(
                "SELECT w.id, w.scheduled_at, w.duration_min, w.type, w.chair_no, w.status, w.reason,"
                " w.source_call_uid, COALESCE(c.in_eval_pool, 0) AS source_call_pool,"
                " w.decided_by, w.decided_at, p.chart_no, p.name, s.name AS staff,"
                " s.role AS staff_role, r.name AS procedure FROM ax_emr_writes w"
                " JOIN source.emr_patients p ON p.id = w.patient_id"
                " JOIN source.emr_staff s ON s.id = w.staff_id"
                " LEFT JOIN source.emr_procedures r ON r.code = w.procedure_code"
                " LEFT JOIN source.ax_calls c ON c.call_uid = w.source_call_uid WHERE w.id = ?",
                (int(id_),),
            ).fetchone()
        )
        return dict(r) if r else None
    if type_ == "claim":
        r = (
            id_.isdigit()
            and conn.execute(
                "SELECT c.id AS claim_id, c.chart_no, p.name, c.service_date, r.name AS procedure_name,"
                " c.claim_amount, c.status, c.paid_amount, c.adjust_note FROM source.emr_claims c"
                " JOIN source.emr_patients p ON p.id = c.patient_id"
                " JOIN source.emr_procedures r ON r.code = c.procedure_code WHERE c.id = ?",
                (int(id_),),
            ).fetchone()
        )
        return dict(r) if r else None
    if type_ == "recon_day":
        got = assistant_tools._recon_days(clinic, conn, {"month": id_[:7]})
        return next((d for d in got.get("days", []) if d["date"] == id_), None)
    if type_ == "source":  # ③ 출처 카드와 같은 모양(api._chat_sources)
        r = conn.execute("SELECT * FROM ax_chunks WHERE id = ?", (id_,)).fetchone()
        return card(conn, dict(r), documents.replacements(conn)) if r else None
    return None


def _history(conn, conversation_id: int) -> list[dict]:
    """앞 메시지 — 역할·글·카드의 {type, id}만(앞 턴 도구 결과는 다시 넣지 않는다, §4.1)"""
    rows = conn.execute(
        "SELECT role, text, cards FROM ax_assistant_messages WHERE conversation_id = ? ORDER BY seq",
        (conversation_id,),
    ).fetchall()
    return [
        {
            "role": r["role"],
            "text": r["text"],
            "cards": [{"type": c["type"], "id": c["id"]} for c in json.loads(r["cards"] or "[]")],
        }
        for r in rows
    ]


def _today_lines(conn, clinic: Clinic) -> list[str]:
    """기준일(요일)과 휴진 요일 — 휴진은 원본 영업시간(emr_clinic.hours_json)에서 읽는다"""
    hours = json.loads(conn.execute("SELECT hours_json FROM emr_clinic").fetchone()[0])
    closed = "·".join(WEEKDAYS[d] for d in sorted(hours["closed_weekdays"]))
    today = clinic.today
    return [f"오늘: {today.isoformat()}({WEEKDAYS[today.weekday()]})", f"휴진 요일: {closed}"]


def _input(history: list[dict], today_lines: list[str], text: str) -> str:
    parts = []
    if history:
        lines = []
        for m in history:
            line = f"{'직원' if m['role'] == 'user' else 'AI 비서'}: {m['text']}"
            if m["cards"]:
                line += f"\n카드: {json.dumps(m['cards'], ensure_ascii=False)}"
            lines.append(line)
        parts.append("앞 대화:\n" + "\n".join(lines))
    return "\n\n".join([*parts, "\n".join(today_lines), f"이번 말: {text}"])


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _tool_log(steps: list[dict]) -> list[dict]:
    return [
        {
            "name": s["tool"],
            "args": s["args"],
            # 성공 = 예외·훅 막힘이 없고 결과가 {error: [...]}가 아님(초안 검사에 걸린 호출도 실패)
            "ok": not s["is_error"]
            and s["hook_status"] == "pass"
            and not (isinstance(s["result"], dict) and "error" in s["result"]),
            "result_summary": _dump(s["result"])[:SUMMARY_LEN],
        }
        for s in steps
        if s["kind"] == "tool"
    ]


def _ok_steps(steps: list[dict]) -> list[dict]:
    return [
        s
        for s in steps
        if s["kind"] == "tool"
        and not s["is_error"]
        and s["hook_status"] == "pass"
        and s["tool"] != DONE
    ]


def _walk(value, out: dict) -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            if k in ID_KEYS and isinstance(v, (str, int)):
                out[ID_KEYS[k]].add(str(v))
            _walk(v, out)
    elif isinstance(value, list):
        for v in value:
            _walk(v, out)


def _targets(ok: list[dict], records: list[dict]) -> dict[str, set]:
    """이번 턴 결과에 나온 카드 대상 id(종류별). source는 ③ 출처와 차트 조회 id — 인용 허용 범위와 같다"""
    out = {t: set() for t in CARD_TYPES}
    for s in ok:
        _walk(s["result"], out)
        if s["tool"] == DRAFT and "error" not in s["result"]:
            out["draft"].add(str(s["result"]["id"]))
            out["patient"].add(s["args"]["chart_no"])
        elif s["tool"] == "recon_days":
            out["recon_day"] |= {d["date"] for d in s["result"].get("days", [])}
        elif s["tool"] == "query_charts":
            out["source"] |= {r["id"] for r in s["result"]["results"]}
    for r in records:
        out["source"] |= {*r["cited_ids"], *r["retrieved_ids"]}
    return out


def _allowed(prompt: str, ok: list[dict], records: list[dict]) -> set[int]:
    sources = [
        prompt
    ] + [  # 실패한 호출·초안의 인자는 근거가 아니다 — 초안 시각은 성공한 결과의 scheduled_at
        _dump({k: v for k, v in s["args"].items() if k not in EVIDENCE_SKIP and v is not None})
        for s in ok
        if s["tool"] != DRAFT and not (isinstance(s["result"], dict) and "error" in s["result"])
    ]
    results = [s["result"] for s in ok] + [r["answer"] for r in records]
    return allowed_numbers(sources, results, True)


def _check(data: dict, nums: set[int], cites: set[str], drafted: bool) -> str | None:
    if not set(data["cited_ids"]) <= cites:
        return CITE
    if not numbers_ok(data["text"], nums):
        return NUMBER
    if CONFIRM_CLAIM_RE.search(data["text"]) or (
        not drafted and DRAFT_CLAIM_RE.search(data["text"])
    ):
        return DRAFT_CLAIM
    return None


def _reply_input(prompt: str, steps: list[dict], records: list[dict]) -> str:
    tools = [
        external(
            f"tool:{s['tool']}",
            _dump({"args": s["args"], "ok": not s["is_error"], "result": s["result"]}),
        )
        for s in steps
        if s["kind"] == "tool" and s["tool"] not in (DONE, ASK)
    ]
    asked = [external("ask_records", _dump(r)) for r in records]
    return "\n\n".join(
        [
            external("input", prompt),
            "이번 턴 도구 결과:\n" + ("\n".join(tools) or "없음"),
            *(["기록 검색 답:\n" + "\n".join(asked)] if asked else []),
        ]
    )


def _reply(conn, gw, store, run_id, prompt, steps, records, clinic) -> tuple[str, str, list]:
    """답 단계 → (상태, 글, 카드)"""
    ok = _ok_steps(steps)
    targets = _targets(ok, records)
    nums = _allowed(prompt, ok, records)
    system = prompts.load(REPLY_TASK, REPLY_VERSION)["system"] + TRUST_NOTICE
    messages = [{"role": "user", "content": _reply_input(prompt, steps, records)}]
    for attempt in range(2):
        try:
            r = gw.complete(
                REPLY_ALIAS,
                task=REPLY_TASK,
                system=system,
                messages=messages,
                schema=REPLY_SCHEMA,
                prompt_version=REPLY_VERSION,
                ref=("agent_run", run_id),
            )
        except BudgetExceeded:  # 도구 단계와 같게 — 하루 상한이면 고정 문구로 끝낸다
            return "budget", FAILED_TEXT, []
        if not r.ok or not r.data:
            return "failed", FAILED_TEXT, []
        data = r.data
        rule = _check(data, nums, targets["source"], bool(targets["draft"]))
        if rule is None:
            break
        with conn:
            store.add_block(rule)
        if attempt == 1:
            break
        messages = [  # 새 목록 — 앞 호출에 넘긴 목록을 바꾸지 않게
            *messages,
            {"role": "assistant", "content": r.text},
            {"role": "user", "content": RETRY_REASON[rule] + RETRY_HINT},
        ]
    cards, seen = [], set()
    for c in data[
        "cards"
    ]:  # 이번 턴 결과 밖 대상은 그 카드만 버린다 — 어긋난 답이어도 남는 카드는 검증된 대상
        key = (c["type"], c["id"])
        if key in seen or c["id"] not in targets.get(c["type"], ()):
            continue
        seen.add(key)
        if (detail := card_detail(conn, c["type"], c["id"], clinic)) is not None:
            cards.append({"type": c["type"], "id": c["id"], "data": detail})
    if rule is not None:
        return FAILED_STATUS[rule], FAILED_TEXT, cards
    return "ok", data["text"], cards


def _with_drafts(conn, cards: list, steps: list[dict]) -> list:
    """이번 턴에 성공한 초안은 답의 상태와 상관없이 카드로 붙인다(답이 이미 넣었으면 그대로) — 답이 빠뜨리거나
    답 단계가 예산·실패로 끝나도 만든 초안이 보이고 승인·취소할 수 있게(§5.2)"""
    out, have = list(cards), {(c["type"], c["id"]) for c in cards}
    for s in _ok_steps(steps):
        if s["tool"] != DRAFT or "error" in s["result"]:
            continue
        id_ = str(s["result"]["id"])
        if ("draft", id_) not in have and (detail := card_detail(conn, "draft", id_)):
            have.add(("draft", id_))
            out.append({"type": "draft", "id": id_, "data": detail})
    return out


def _save(
    conn, conversation_id, seq, role, text, cards=None, tool_calls=None, run_id=None, status="ok"
):
    conn.execute(
        "INSERT INTO ax_assistant_messages (conversation_id, seq, role, text, cards, tool_calls,"
        " run_id, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            conversation_id,
            seq,
            role,
            text,
            None if cards is None else _dump(cards),
            None if tool_calls is None else _dump(tool_calls),
            run_id,
            status,
            _now(),
        ),
    )


NO_INDEX_SEARCHER = (
    object()
)  # 검색 색인이 없을 때 searcher 자리에 넣는다 — ask_records는 "색인 없음" 결과
NO_INDEX_TEXT = "자료실 색인 없음 — 기록 검색을 쓸 수 없습니다"


def turn(
    conn: sqlite3.Connection,
    gw,
    searcher,
    clinic: Clinic,
    *,
    conversation_id: int | None,
    text: str,
    part: str,
    on_event=None,
) -> dict:
    """사용자 말 1개 → {conversation_id, seq, status, text, cards, tool_calls, run_id}(seq = 어시스턴트 메시지).
    상태: ok · number_failed · cite_failed · draft_failed · failed · budget · cost_limit. 이번 턴에 성공한 초안은
    상태와 상관없이 카드로 붙는다(_with_drafts)"""
    if conversation_id is None:
        conversation_id = conn.execute(
            "INSERT INTO ax_assistant_conversations (title, part, created_at) VALUES (?, ?, ?)",
            (text[:TITLE_LEN], part, _now()),
        ).lastrowid
    history = _history(conn, conversation_id)
    seq = len(history) + 1
    _save(conn, conversation_id, seq, "user", text)
    conn.commit()  # Runner.start가 거절되면 미커밋 쓰기가 되돌아가므로
    target = f"{conversation_id}:{seq}"
    try:
        out = _turn(conn, gw, searcher, clinic, conversation_id, seq, history, text, on_event)
    except Exception:
        # 실행이 running으로 남지 않게 닫고, 실패 메시지를 남긴 뒤 원래 예외를 다시 던진다
        conn.rollback()
        try:
            conn.execute(
                "UPDATE ax_agent_runs SET status = 'failed', reason = '예외', locked_by = NULL"
                " WHERE agent = ? AND target = ? AND status = 'running'",
                (AGENT, target),
            )
            run = conn.execute(
                "SELECT id FROM ax_agent_runs WHERE agent = ? AND target = ?", (AGENT, target)
            ).fetchone()
            run_id = run["id"] if run else None
            steps = RunStore(conn).steps(run_id) if run_id else []
            try:
                _save(
                    conn,
                    conversation_id,
                    seq + 1,
                    "assistant",
                    FAILED_TEXT,
                    _with_drafts(conn, [], steps),
                    _tool_log(steps),
                    run_id,
                    "failed",
                )
            except sqlite3.IntegrityError:
                pass  # 같은 대화에 동시에 보낸 말이 그 자리를 먼저 썼다 — 실행만 닫는다
            conn.commit()
        except Exception:  # 정리가 실패해도 열린 트랜잭션을 남기지 않고 원래 예외를 던진다
            conn.rollback()
        raise
    if on_event:
        on_event({"type": "reply", **out})
    return out


def _turn(conn, gw, searcher, clinic, conversation_id, seq, history, text, on_event) -> dict:
    prompt = _input(history, _today_lines(conn, clinic), text)
    reg, names = registry(
        clinic, searcher, {"conversation_id": conversation_id, "turn": seq}, on_event
    )
    store = RunStore(conn)
    outcome = Runner(spec(names), gw, store, reg).start(f"{conversation_id}:{seq}", prompt)
    steps = store.steps(outcome.run_id)
    if outcome.status == "failed" or outcome.reason in ("budget", "cost_limit"):
        status = "failed" if outcome.status == "failed" else outcome.reason
        reply_text, cards = FAILED_TEXT, []
    else:  # done·call_limit — 모은 결과로 답한다
        records = []
        for s in _ok_steps(steps):
            if (
                s["tool"] == ASK
            ):  # 도구 단계가 끝난 뒤 ③에 맡긴다(도구 안에서 LLM을 부르지 않는 규칙)
                if searcher is NO_INDEX_SEARCHER:
                    got = {
                        "status": "no_index",
                        "answer_text": NO_INDEX_TEXT,
                        "cited_ids": [],
                        "retrieved_ids": [],
                    }
                else:
                    got = chat.chat(conn, gw, searcher, clinic, s["result"]["queued"])
                records.append(
                    {
                        "question": s["result"]["queued"],
                        "status": got["status"],
                        "answer": got["answer_text"],
                        "cited_ids": got["cited_ids"],
                        "retrieved_ids": got["retrieved_ids"],
                    }
                )
        status, reply_text, cards = _reply(
            conn, gw, store, outcome.run_id, prompt, steps, records, clinic
        )
    cards = _with_drafts(conn, cards, steps)
    tool_calls = _tool_log(steps)
    _save(
        conn,
        conversation_id,
        seq + 1,
        "assistant",
        reply_text,
        cards,
        tool_calls,
        outcome.run_id,
        status,
    )
    conn.commit()
    return {
        "conversation_id": conversation_id,
        "seq": seq + 1,
        "status": status,
        "text": reply_text,
        "cards": cards,
        "tool_calls": tool_calls,
        "run_id": outcome.run_id,
    }


def conversations(
    conn: sqlite3.Connection, limit: int = 20, before_id: int | None = None
) -> list[dict]:
    """대화 목록(최근 순) — before_id보다 작은 id만(다음 쪽)"""
    rows = conn.execute(
        "SELECT id, title, part, created_at FROM ax_assistant_conversations"
        " WHERE deleted_at IS NULL AND (? IS NULL OR id < ?) ORDER BY id DESC LIMIT ?",
        (before_id, before_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def conversation(conn: sqlite3.Connection, conversation_id: int) -> dict | None:
    """저장된 대화 그대로(모델을 부르지 않는다, §6) — 초안 카드만 지금 상태로 다시 채운다"""
    row = conn.execute(
        "SELECT id, title, part, created_at, deleted_at IS NOT NULL AS deleted"
        " FROM ax_assistant_conversations WHERE id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return None
    messages = []
    for m in conn.execute(
        "SELECT seq, role, text, cards, tool_calls, status, run_id, created_at"
        " FROM ax_assistant_messages"
        " WHERE conversation_id = ? ORDER BY seq",
        (conversation_id,),
    ):
        cards = json.loads(m["cards"] or "[]")
        for c in cards:
            if c["type"] == "draft" and (detail := card_detail(conn, "draft", c["id"])):
                c["data"] = detail
        messages.append(
            {**dict(m), "cards": cards, "tool_calls": json.loads(m["tool_calls"] or "[]")}
        )
    return {**dict(row), "deleted": bool(row["deleted"]), "messages": messages}


def delete_conversation(conn: sqlite3.Connection, conversation_id: int, at: str) -> bool:
    """목록에서 숨김(행·메시지는 남는다 — 승인 예약의 근거). 이미 숨겼으면 그대로, 없으면 False"""
    cur = conn.execute(
        "UPDATE ax_assistant_conversations SET deleted_at = coalesce(deleted_at, ?) WHERE id = ?",
        (at, conversation_id),
    )
    return cur.rowcount > 0
