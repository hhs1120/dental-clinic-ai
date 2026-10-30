"""RAG 질문 검사 (truth_rag_qa — 평가 설계 §3.1, 질문을 쓰기 전엔 빈 테이블이라 검사 없음)"""

import json
import re
import sqlite3
from collections import Counter, defaultdict

from generator import llm_jobs
from generator.verify.common import _rows
from generator.verify.documents import _docs

# 유형별 문항 수(평가 설계 §3.1 표 — tests/generator/test_doc_sync.py가 비교). dev·test 반씩
QA_TYPE_COUNTS = {"manual": 18, "notice": 10, "case": 12, "data": 12, "chart": 10, "refusal": 8}


# 도구 → expected_args에 쓸 수 있는 인자 (앱 설계 §3 시그니처. search_docs의 자유 텍스트 query는 채점하지 않아 뺀다)
QA_TOOL_ARGS = {
    "search_docs": {"source_types", "date_from", "date_to", "patient_chart_no", "top_k"},
    "query_patient": {"chart_no", "name"},
    "query_schedule": {
        "date_from",
        "date_to",
        "status",
        "type",
        "has_unpaid_balance",
        "is_new_patient",
    },
    "query_claims": {"date_from", "date_to", "chart_no", "procedure_code", "status"},
    "refuse": {"reason"},
}


QA_REFUSE_REASONS = {"not_found", "restricted"}


# 정답 출처 id (ground_truth/schema.sql truth_rag_qa) — 번호는 앞자리 0 없이 (doc:07과 doc:7이 다른 문자열로 격리 검사를 빠져나가지 않게),
# 통화는 call_uid (앱 설계 §1)
QA_SOURCE_ID = re.compile(r"(doc|chart):([1-9]\d*)|call:(c-\d{4,})|thread:(C-\d{5}):(\d{4}-\d{2})")


QA_SOURCES = {
    "manual": ("doc",),
    "notice": ("doc",),
    "case": ("call", "thread"),
    "chart": ("chart",),
}  # 유형 → 출처


QA_CHART_ANSWERS = (1, 5)  # 차트형 정답 집합 크기 (정답이 많으면 Hit@5가 거저 통과)


QA_COPY_RUN = 4  # 질문과 정답 문단이 이만큼 연속한 어절(조사 뗀)이 같으면 베낀 것 (경고)


# 어절 끝에서 떼는 조사·어미 (긴 것부터 한 번만). 연속 구절 비교에서 '부도가'·'부도는'을 같게 보려는 거친 근사
_QA_SUFFIXES = sorted(
    [
        "이면",
        "으면",
        "면",
        "이가",
        "가",
        "이",
        "은",
        "는",
        "을",
        "를",
        "에서",
        "에게",
        "께",
        "에",
        "의",
        "도",
        "으로",
        "로",
        "와",
        "과",
        "이랑",
        "랑",
        "하고",
        "까지",
        "부터",
        "만",
        "요",
        "야",
        "지",
        "나",
        "이야",
        "이지",
        "였지",
        "했지",
        "했어",
        "어",
        "아",
    ],
    key=len,
    reverse=True,
)


def qa_stems(text: str) -> list[str]:
    """어절 → 조사·어미를 한 번 뗀 말 (연속 구절 비교용)"""
    out = []
    for w in re.findall(r"[0-9A-Za-z가-힣,]+", text):
        w = w.strip(",")
        cut = next((x for x in _QA_SUFFIXES if w.endswith(x) and len(w) > len(x)), "")
        if w:
            out.append(w[: len(w) - len(cut)] if cut else w)
    return out


_QA_JSON_COLS = ("answer_doc_ids", "expected_tools", "expected_args")


def _qa_table(conn) -> list[dict]:
    """truth_rag_qa 행 그대로(JSON 칸만 풂)"""
    rows = _rows(conn, "SELECT * FROM truth.truth_rag_qa ORDER BY id")
    for r in rows:
        for c in _QA_JSON_COLS:
            r[c] = json.loads(r[c])
    return rows


def _qa(conn) -> list[dict]:
    """질문 + sources(answer_doc_ids 순서대로 _qa_source 결과). 문서는 한 번만 읽는다"""
    rows = _qa_table(conn)
    docs = {d["id"]: d for d in _docs(conn)} if rows else {}
    for r in rows:
        r["sources"] = [_qa_source(conn, i, docs) for i in r["answer_doc_ids"]]
    return rows


def _qa_source(conn, sid: str, docs: dict) -> dict | None:
    """출처 id → {kind, text, patient_id, doc} (없거나 형식이 틀리면 None). docs = {id: _docs 행}"""
    m = QA_SOURCE_ID.fullmatch(sid) if isinstance(sid, str) else None
    if not m:
        return None
    kind = m.group(1) or ("call" if m.group(3) else "thread")
    num = int(m.group(2) or 0)
    if kind == "doc":
        d = docs.get(num)
        return d and {"kind": kind, "text": d["body"], "patient_id": None, "doc": d}
    if kind == "call":
        r = conn.execute(
            "SELECT transcript, patient_id FROM ax_calls WHERE call_uid = ? AND transcript IS NOT NULL",
            (m.group(3),),
        ).fetchone()
        return r and {"kind": kind, "text": r[0], "patient_id": r[1], "doc": None}
    if kind == "thread":
        rows = conn.execute(
            "SELECT m.body, p.id FROM emr_messages m JOIN emr_patients p ON p.id = m.patient_id"
            " WHERE p.chart_no = ? AND substr(m.sent_at, 1, 7) = ? ORDER BY m.sent_at",
            (m.group(4), m.group(5)),
        ).fetchall()
        return (
            {
                "kind": kind,
                "text": "\n".join(r[0] for r in rows),
                "patient_id": rows[0][1],
                "doc": None,
            }
            if rows
            else None
        )
    r = conn.execute(
        "SELECT c.note, t.patient_id FROM emr_chart_notes c JOIN emr_treatments t ON t.id = c.treatment_id"
        " WHERE c.treatment_id = ?",
        (num,),
    ).fetchone()
    return r and {"kind": kind, "text": r[0], "patient_id": r[1], "doc": None}


def has_qa(conn, ctx) -> bool:
    """RAG 질문 검사를 돌릴지 — 적재된 질문이 있거나, 짝 rag_qa.json이 있음(JSON만 쓰고 make data를 안 돌린 경우도 잡게)"""
    return bool(conn.execute("SELECT 1 FROM truth.truth_rag_qa LIMIT 1").fetchone()) or bool(
        ctx.rag_qa and ctx.rag_qa.exists()
    )


def check_qa_matches_json(conn, ctx):
    """적재된 truth_rag_qa = ground_truth/rag_qa.json(원본) — JSON만 고치고 make data를 안 돌리면 예전 질문을 검사·평가하게 됨.
    짝 JSON이 없는 DB(verify 테스트 DB)는 보지 않는다"""
    if ctx.rag_qa is None:
        return []
    table = {r["id"]: r for r in _qa_table(conn)}
    if not ctx.rag_qa.exists():
        return [{"problem": "rag_qa.json 없음", "loaded": len(table)}] if table else []
    cols = [r[1] for r in conn.execute("PRAGMA truth.table_info(truth_rag_qa)")]
    src = {
        q["id"]: {c: int(q[c]) if c == "expect_refusal" else q.get(c) for c in cols}
        for q in json.loads(ctx.rag_qa.read_text(encoding="utf-8"))
    }
    diff = sorted(i for i in table.keys() | src.keys() if table.get(i) != src.get(i))
    return [{"problem": "make data로 다시 적재", "ids": diff[:10], "n": len(diff)}] if diff else []


def _hint_fact(q, sources) -> dict | None:
    """answer_chunk_hint가 정답 문서의 심은 사실 id면 그 사실"""
    return next(
        (
            f
            for s in sources
            if s and s["doc"]
            for f in s["doc"]["facts"]
            if f["id"] == q["answer_chunk_hint"]
        ),
        None,
    )


def check_qa_fields(conn, ctx):
    """질문 칸: 도구는 앱 설계 §3의 5개 — 문서·사례·차트형은 search_docs만, 데이터형은 query_*만, 거절형은 마지막만 refuse(앞은
    search_docs). expected_args = {도구: {인자}}, 도구는 expected_tools 안, 인자는 QA_TOOL_ARGS 안(refuse 이유는
    QA_REFUSE_REASONS). answer_sql은 데이터·차트형에만"""
    bad = []
    for q in _qa(conn):
        tools, t, args = q["expected_tools"], q["type"], q["expected_args"]
        ok_args = isinstance(args, dict) and all(
            tool in tools
            and isinstance(a, dict)
            and set(a) <= QA_TOOL_ARGS.get(tool, set())
            and (tool != "refuse" or a.get("reason") in QA_REFUSE_REASONS)
            for tool, a in args.items()
        )
        why = (
            "도구 없음"
            if not tools
            else "모르는 도구"
            if set(tools) - set(QA_TOOL_ARGS)
            else "search_docs만"
            if t in QA_SOURCES and tools != ["search_docs"]
            else "query_*만"
            if t == "data" and not all(x.startswith("query_") for x in tools)
            else "마지막만 refuse"
            if t == "refusal" and (tools[-1] != "refuse" or set(tools[:-1]) - {"search_docs"})
            else "expected_args"
            if not ok_args
            else "answer_sql"
            if (q["answer_sql"] is not None) != (t in ("data", "chart"))
            else None
        )
        if why:
            bad.append({"id": q["id"], "problem": why})
    return bad


def check_qa_counts(conn, ctx):
    """유형별 문항 수 = QA_TYPE_COUNTS, dev·test 반씩 (유형 비율 동일)"""
    qa = _qa(conn)
    if not qa:
        return []
    n = Counter((q["type"], q["split"]) for q in qa)
    return [
        {"type": t, "dev": n[t, "dev"], "test": n[t, "test"], "expected": total}
        for t, total in sorted(QA_TYPE_COUNTS.items())
        if not n[t, "dev"] == n[t, "test"] == total / 2
    ]


def check_qa_answer_ids(conn, ctx):
    """정답 출처가 있고 유형에 맞음(문서형 doc, 사례형 call·thread, 차트형 chart, 데이터·거절형 없음), 방해 문서는 정답 아님,
    공지형은 최신 공지(옛 공지를 대체한 것)를 포함, answer_chunk_hint는 정답 문서의 심은 사실 id이거나 정답 본문의 구절"""
    bad = []
    for q in _qa(conn):
        ids, kinds, sources = q["answer_doc_ids"], QA_SOURCES.get(q["type"], ()), q["sources"]
        hint = q["answer_chunk_hint"]
        why = (
            "정답 없음"
            if kinds and not ids
            else "정답 있으면 안 됨"
            if not kinds and ids
            else "출처 없음"
            if None in sources
            else "유형과 다른 출처"
            if any(s["kind"] not in kinds for s in sources)
            else "방해 문서"
            if any(s["doc"] and s["doc"]["is_distractor"] for s in sources)
            else "최신 공지 없음"
            if q["type"] == "notice"
            and not any(
                s["doc"]["type"] == "notice" and s["doc"]["supersedes_doc_id"] for s in sources
            )
            else "힌트가 정답에 없음"
            if kinds
            and hint
            and not _hint_fact(q, sources)
            and not any(hint in s["text"] for s in sources)
            else None
        )
        if why:
            bad.append({"id": q["id"], "problem": why})
    return bad


def check_qa_split_isolation(conn, ctx):
    """정답 출처(문서·통화·스레드·차트)는 한 split에만 — dev/test가 같은 출처를 공유하면 누출"""
    seen = defaultdict(set)
    for q in _qa(conn):
        for i in q["answer_doc_ids"]:
            seen[i].add(q["split"])
    return [{"source": i, "splits": sorted(s)} for i, s in sorted(seen.items()) if len(s) > 1]


def check_qa_same_fact_docs(conn, ctx):
    """힌트 사실의 같은 사실 묶음(same)에 든 핵심 문서는 모두 정답에 (평가 설계 §3.1)"""
    groups = defaultdict(set)
    for d in _docs(conn):
        for f in d["facts"]:
            if f.get("same") and not d["is_distractor"]:
                groups[f["same"]].add(f"doc:{d['id']}")
    bad = []
    for q in _qa(conn):
        fact = _hint_fact(q, q["sources"])
        missing = (
            sorted(groups.get(fact.get("same"), set()) - set(q["answer_doc_ids"])) if fact else []
        )
        if missing:
            bad.append({"id": q["id"], "same": fact["same"], "missing": missing})
    return bad


def check_qa_answer_sql(conn, ctx):
    """answer_sql로 정답 재계산 — 데이터형은 숫자 한 칸이 answer_text의 유일한 숫자이고 ≥ 2, 차트형은 treatment_id 집합 = 정답이고
    크기가 QA_CHART_ANSWERS 안"""
    bad = []
    for q in _qa(conn):
        if q["type"] not in ("data", "chart"):
            continue
        try:
            rows = conn.execute(q["answer_sql"] or "").fetchall()
        except sqlite3.Error as e:
            bad.append({"id": q["id"], "problem": f"쿼리 오류 {e}"})
            continue
        if q["type"] == "data":
            value = rows[0][0] if len(rows) == 1 and len(rows[0]) == 1 else None
            nums = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]*", q["answer_text"])]
            why = (
                "숫자 한 칸 아님"
                if not isinstance(value, int)
                else "답 < 2"
                if value < 2
                else "answer_text 숫자가 답 하나가 아님"
                if nums != [value]
                else None
            )
        else:
            got = {f"chart:{r[0]}" for r in rows}
            lo, hi = QA_CHART_ANSWERS
            why = (
                "정답과 다름"
                if got != set(q["answer_doc_ids"])
                else f"정답 {len(got)}건"
                if not lo <= len(got) <= hi
                else None
            )
        if why:
            bad.append({"id": q["id"], "problem": why})
    return bad


def check_qa_distractor_clean(conn, ctx):
    """정답 문서의 방해 문서(짝이 정답 문서인 옛 공지·비슷한 주제·타 병원)에 힌트 사실의 말이 모두 들어 있지 않음 — 방해 문서가
    정답이 되면 안 됨. 짝이 아닌 문서는 보지 않는다('30일' 같은 짧은 말이 다른 공지 날짜에 맞음)"""
    distractors = [d for d in _docs(conn) if d["is_distractor"]]
    bad = []
    for q in _qa(conn):
        fact = _hint_fact(q, q["sources"])
        hits = [
            d["id"]
            for d in distractors
            if fact
            and fact["words"]
            and f"doc:{d['pair_doc_id']}" in q["answer_doc_ids"]
            and all(llm_jobs.contains(d["body"], w) for w in fact["words"])
        ]
        if hits:
            bad.append({"id": q["id"], "fact": fact["id"], "distractors": hits})
    return bad


def check_qa_expected_args(conn, ctx):
    """데이터형 정답 인자 값이 질문·정답 쿼리와 맞음 — query_patient 이름은 환자 목록에 있고 질문에 든 이름, 환자(이름·차트
    번호)는 정답 쿼리의 차트 번호, query_claims 차트 번호도 같은 환자, 날짜 인자는 정답 쿼리의 날짜(또는 그 달 LIKE)"""
    chart_of = dict(conn.execute("SELECT name, chart_no FROM emr_patients"))
    bad = []
    for q in _qa(conn):
        if q["type"] != "data" or not isinstance(q["expected_args"], dict):
            continue
        args, sql = q["expected_args"], q["answer_sql"] or ""
        charts = set(re.findall(r"C-\d{5}", sql))
        days = set(re.findall(r"\d{4}-\d{2}-\d{2}", sql))
        months = set(re.findall(r"LIKE '(\d{4}-\d{2})%'", sql))
        p, c = args.get("query_patient", {}), args.get("query_claims", {})
        who = chart_of.get(p["name"]) if "name" in p else p.get("chart_no")
        why = (
            "query_patient 이름이 환자 목록·질문에 없음"
            if "name" in p and (p["name"] not in chart_of or p["name"] not in q["question"])
            else "query_patient 환자가 정답 쿼리와 다름"
            if who and charts and who not in charts
            else "query_claims 차트 번호가 정답 쿼리·환자와 다름"
            if "chart_no" in c
            and ((charts and c["chart_no"] not in charts) or (who and c["chart_no"] != who))
            else next(
                (
                    f"{tool}.{k}가 정답 쿼리 날짜와 다름"
                    for tool, a in args.items()
                    for k in ("date_from", "date_to")
                    if k in a and a[k] not in days and a[k][:7] not in months
                ),
                None,
            )
        )
        if why:
            bad.append({"id": q["id"], "problem": why})
    return bad


def check_qa_patient_names(conn, ctx):
    """질문에 든 환자 이름은 동명이인 없음(데이터형 query_patient(name)도 동명이인이면 답할 수 없음)·질문 하나에만, 사례형 질문은
    정답 환자 이름을 담음, 차트형 질문엔 환자 이름 없음"""
    names = Counter(r[0] for r in conn.execute("SELECT name FROM emr_patients"))
    qa, bad = _qa(conn), []
    used = Counter(n for q in qa for n in names if n in q["question"])
    for q in qa:
        in_q = [n for n in names if n in q["question"]]
        why = (
            "이름이 다른 질문에도"
            if any(used[n] > 1 for n in in_q)
            else "동명이인"
            if any(names[n] > 1 for n in in_q)
            else "차트형에 환자 이름"
            if q["type"] == "chart" and in_q
            else None
        )
        if q["type"] == "case" and not why:
            pids = {s["patient_id"] for s in q["sources"] if s}
            pn = [
                conn.execute("SELECT name FROM emr_patients WHERE id = ?", (p,)).fetchone()[0]
                for p in pids
                if p
            ]
            why = (
                "정답 환자 없음"
                if not pn
                else "질문에 환자 이름 없음"
                if not all(n in q["question"] for n in pn)
                else "동명이인"
                if any(names[n] > 1 for n in pn)
                else None
            )
        if why:
            bad.append({"id": q["id"], "problem": why})
    return bad


def check_qa_refusal(conn, ctx):
    """expect_refusal ⇔ 거절형. 금지 토픽(config forbidden_topics)마다 그 주제를 묻는 거절형 질문이 정확히 1개, 다른 질문은 금지 토픽 아님"""
    qa = _qa(conn)
    if not qa:
        return []
    bad = [
        {"id": q["id"], "problem": "expect_refusal"}
        for q in qa
        if bool(q["expect_refusal"]) != (q["type"] == "refusal")
    ]
    for t in llm_jobs.forbidden_topics():
        hits = [q for q in qa if any(re.search(p, q["question"]) for p in t["patterns"])]
        if len(hits) != 1 or hits[0]["type"] != "refusal":
            bad.append({"topic": t["topic"], "questions": [q["id"] for q in hits]})
    return bad


def check_qa_refusal_absent(conn, ctx):
    """거절형 답이 어느 문서에도 없음 — answer_chunk_hint(답이 있다면 나올 말)가 문서 본문에 없음"""
    bodies = [(r[0], r[1]) for r in conn.execute("SELECT id, body FROM ax_documents")]
    return [
        {"id": q["id"], "hint": q["answer_chunk_hint"], "docs": hits}
        for q in _qa(conn)
        if q["type"] == "refusal"
        and q["answer_chunk_hint"]
        and (hits := [i for i, b in bodies if q["answer_chunk_hint"] in b])
    ]


def check_qa_copied_wording(conn, ctx):
    """문서·사례형 질문이 정답 문장을 베끼지 않음 — 정답 문단과 연속 QA_COPY_RUN어절(조사 뗀)이 같거나, 정답 값(힌트 사실의 말,
    사례형은 힌트 구절)이 질문에 있으면 경고. 정답 문단 = 정답 출처 본문(문서는 빈 줄로 나눈 문단, 사례형은 전사·스레드 전체).
    주제어(시술명·보험·예약 등) 한두 개가 겹치는 것은 보지 않는다"""
    bad = []
    for q in _qa(conn):
        if q["type"] not in ("manual", "notice", "case"):
            continue
        sources = [s for s in q["sources"] if s]
        fact = _hint_fact(q, sources)
        values = (
            fact["words"] if fact else [q["answer_chunk_hint"]] if q["answer_chunk_hint"] else []
        )
        leaked = [v for v in values if llm_jobs.contains(q["question"], v)]
        stems = qa_stems(q["question"])
        runs = {tuple(stems[i : i + QA_COPY_RUN]) for i in range(len(stems) - QA_COPY_RUN + 1)}
        paras = [
            p
            for s in sources
            for p in (s["text"].split("\n\n") if s["kind"] == "doc" else [s["text"]])
        ]
        para_runs = set()
        for p in paras:
            ps = qa_stems(p)
            para_runs |= {tuple(ps[i : i + QA_COPY_RUN]) for i in range(len(ps) - QA_COPY_RUN + 1)}
        copied = sorted(" ".join(r) for r in runs & para_runs)
        if leaked or copied:
            bad.append({"id": q["id"], "values": leaked, "copied": copied})
    return bad
