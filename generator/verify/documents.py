"""RAG 문서 검사 (9단계, 가상 데이터 생성 설계 §4.2·§5.3)"""

import json
import re
from collections import Counter

from generator import llm_jobs
from generator.master import (
    ADJUST_REASONS,
    CLINIC_NAME,
)
from generator.verify.common import _rows


def _docs(conn) -> list[dict]:
    rows = _rows(
        conn,
        "SELECT d.*, m.doc_key, m.kind, m.is_distractor, m.pair_doc_id, m.supersedes_doc_id,"
        " m.avoid_doc_ids, m.planted_facts FROM ax_documents d JOIN truth.truth_doc_meta m ON m.doc_id = d.id"
        " ORDER BY d.id",
    )
    for r in rows:
        r["facts"], r["avoid"] = json.loads(r["planted_facts"]), json.loads(r["avoid_doc_ids"])
    return rows


def check_document_counts(conn, ctx):
    """문서 수 = 핵심 documents_core + 방해 documents_distractor (truth_gen_meta)"""
    n = dict(
        conn.execute(
            "SELECT m.is_distractor, count(*) FROM ax_documents d JOIN truth.truth_doc_meta m"
            " ON m.doc_id = d.id GROUP BY m.is_distractor"
        ).fetchall()
    )
    got, want = (n.get(0, 0), n.get(1, 0)), (ctx.documents_core, ctx.documents_distractor)
    return [] if got == want else [{"core·distractor": got, "expected": want}]


def check_doc_meta_match(conn, ctx):
    """ax_documents ↔ truth_doc_meta 1:1"""
    return _rows(
        conn,
        "SELECT d.id, NULL AS meta FROM ax_documents d LEFT JOIN truth.truth_doc_meta m ON m.doc_id = d.id"
        " WHERE m.doc_id IS NULL UNION ALL SELECT NULL, m.doc_id FROM truth.truth_doc_meta m"
        " LEFT JOIN ax_documents d ON d.id = m.doc_id WHERE d.id IS NULL",
    )


def check_doc_planted_facts(conn, ctx):
    """심은 사실의 말(청구 기준 소제목 포함)이 본문에 있음 — 앞에 숫자가 붙지 않게(llm_jobs.contains)"""
    bad = []
    for r in _docs(conn):
        missing = [
            w for f in r["facts"] for w in f["words"] if not llm_jobs.contains(r["body"], w)
        ]
        if missing:
            bad.append({"id": r["id"], "key": r["doc_key"], "missing": missing})
    return bad


def check_doc_similar_clean(conn, ctx):
    """비슷한 주제 문서에 짝 문서·avoid 문서의 심은 사실이 없음 (방해 문서에 정답 텍스트 없음)"""
    docs = {r["id"]: r for r in _docs(conn)}
    bad = []
    for r in docs.values():
        if r["kind"] != "similar":
            continue
        pair = docs.get(r["pair_doc_id"])
        hit = [
            w
            for d in ([pair] if pair else []) + [docs[i] for i in r["avoid"] if i in docs]
            for f in d["facts"]
            for w in f["words"]
            if llm_jobs.contains(r["body"], w)
        ]
        if pair is None or hit:
            bad.append({"id": r["id"], "key": r["doc_key"], "pair_words": hit or "짝 없음"})
    return bad


def check_doc_external_clean(conn, ctx):
    """타 병원·일반 문서에 우리 병원 이름(master.CLINIC_NAME) 없음"""
    return [
        {"id": r["id"], "key": r["doc_key"]}
        for r in _docs(conn)
        if r["kind"] == "external" and CLINIC_NAME in r["body"]
    ]


DOC_PATH = re.compile(r"docs/[a-z_]+/\d{3}\.md")


def check_doc_key_leak(conn, ctx):
    """문서 경로는 docs/<type>/<번호>.md, 경로·제목에 목록 이름(doc_key — sim-·old- 같은 종류 표시) 없음"""
    return [
        {"id": r["id"], "path": r["path"]}
        for r in _docs(conn)
        if not DOC_PATH.fullmatch(r["path"])
        or r["path"] != f"docs/{r['type']}/{r['id']:03d}.md"
        or r["doc_key"] in r["path"]
        or r["doc_key"] in r["title"]
    ]


def check_notice_versions(conn, ctx):
    """최신 공지마다 옛 공지 정확히 1개. 옛 공지: 공지끼리, 최신이 대체(supersedes), 옛 시행일 < 최신 시행일 ≤ TODAY,
    옛 시행일 < 생성 기간 시작"""
    docs = {r["id"]: r for r in _docs(conn)}
    olds = Counter(r["pair_doc_id"] for r in docs.values() if r["kind"] == "old_version")
    bad = [
        {"id": r["id"], "key": r["doc_key"], "reason": f"옛 공지 {olds[r['id']]}개"}
        for r in docs.values()
        if r["kind"] == "core" and r["type"] == "notice" and olds[r["id"]] != 1
    ]
    for r in docs.values():
        if r["kind"] != "old_version":
            continue
        new = docs.get(r["pair_doc_id"])
        why = (
            "최신 없음"
            if new is None
            else "공지 아님"
            if (r["type"], new["type"]) != ("notice", "notice")
            else "대체 표시 없음"
            if new["supersedes_doc_id"] != r["id"]
            else "시행일 없음"
            if not (r["effective_date"] and new["effective_date"])
            else "옛 ≥ 최신"
            if r["effective_date"] >= new["effective_date"]
            else "최신 > TODAY"
            if new["effective_date"] > ctx.today.isoformat()
            else "옛 ≥ 생성 기간 시작"
            if r["effective_date"] >= ctx.range_start.isoformat()
            else None
        )
        if why:
            bad.append({"id": r["id"], "key": r["doc_key"], "reason": why})
    return bad


def check_claim_sections(conn, ctx):
    """청구 기준 문서에 master.ADJUST_REASONS 상세 사유마다 절(본문의 '## 소제목')이 정확히 1개, 그 사유의 기준 말이 그 절 안에"""
    found, bad = Counter(), []
    for r in _docs(conn):
        if r["type"] != "claim_standard":
            continue
        sections = llm_jobs.md_sections(r["body"])
        for f in r["facts"]:
            heading = next((w for w in f["words"] if w.startswith("## ")), None)
            if f.get("reason") and heading:
                hits = [text for head, text in sections if head == heading]
                found[f["reason"]] += len(hits)
                rules = [
                    w
                    for g in r["facts"]
                    if g.get("reason") == f["reason"] and g is not f
                    for w in g["words"]
                ]
                if len(hits) == 1 and not all(llm_jobs.contains(hits[0], w) for w in rules):
                    bad.append({"reason": f["reason"], "id": r["id"], "problem": "기준이 절 밖"})
    bad += [
        {"reason": k, "sections": found.get(k, 0)}
        for k in sorted(ADJUST_REASONS)
        if found.get(k, 0) != 1
    ]
    return bad + [
        {"reason": k, "sections": v} for k, v in sorted(found.items()) if k not in ADJUST_REASONS
    ]


def check_doc_patient_names(conn, ctx):
    """문서 본문에 환자 이름 없음"""
    names = sorted({r[0] for r in conn.execute("SELECT name FROM emr_patients")})
    return [
        {"id": r["id"], "name": n}
        for r in _rows(conn, "SELECT id, body FROM ax_documents ORDER BY id")
        for n in names
        if n in r["body"]
    ]


def check_forbidden_topics(conn, ctx):
    """금지 토픽(config forbidden_topics) 코퍼스 전체 미포함 — 문서·전사·차트·문자"""
    corpus = [("document", r[0], r[1]) for r in conn.execute("SELECT id, body FROM ax_documents")]
    corpus += [
        ("call", r[0], r[1])
        for r in conn.execute("SELECT id, transcript FROM ax_calls WHERE transcript IS NOT NULL")
    ]
    corpus += [
        ("chart", r[0], r[1])
        for r in conn.execute("SELECT treatment_id, note FROM emr_chart_notes")
    ]
    corpus += [("message", r[0], r[1]) for r in conn.execute("SELECT id, body FROM emr_messages")]
    patterns = llm_jobs.forbidden_patterns()
    return [
        {"source": src, "id": i, "pattern": p}
        for src, i, text in corpus
        for p in patterns
        if re.search(p, text)
    ]
