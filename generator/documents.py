"""9단계 RAG 문서 — ax_documents·truth_doc_meta (가상 데이터 생성 설계 §4.2 '9단계 구현 결정').
두 단계, 9b(차트 서술)와 같은 틀:
- doc_plan(코드): doc_catalog 표로 문서·정답을 쓰고, 본문에는 사실을 그대로 옮긴 초안을 넣는다(가짜 게이트웨이면 남음).
  문서 번호는 시드 셔플, 경로는 번호 — 표 순서(핵심 → 방해)나 표 이름(sim-·old-)이 app.sqlite에 드러나지 않게
- documents(LLM): 초안을 문서로 다시 쓴다 — 심은 사실·청구 기준 소제목 포함, 비슷한 주제는 짝의 사실 없음(llm_jobs 검증).
  건 id는 문서 번호(작업 파일에도 표 이름이 없게)
"""
import json

from generator import llm_jobs
from generator.doc_catalog import BY_KEY, CATALOG, CLAIM_HEADINGS, Doc
from generator.llm_jobs import Task
from generator.master import CLINIC_NAME

DOC_LENGTH_CHARS = {"notice": (150, 1500), "default": (400, 3000)}  # 본문 글자 수(공백 제외) — 생성 검증. 공지는 짧다
SCHEMA = {
    "type": "object",
    "properties": {"body": {"type": "string", "description": "마크다운 문서 본문 (첫 줄 '# 제목')"}},
    "required": ["body"],
    "additionalProperties": False,
}
TASK = Task(name="document", prompt="gen_document/v1", schema=SCHEMA, store=("body",), fake=False)


def version(d: Doc) -> str:
    return "2" if d.type == "notice" and d.kind == "core" else "1"


def path(d: Doc, doc_id: int) -> str:
    return f"docs/{d.type}/{doc_id:03d}.md"


def length(d: Doc) -> list[int]:
    return list(DOC_LENGTH_CHARS.get(d.type, DOC_LENGTH_CHARS["default"]))


def _rules(d: Doc, reason: str) -> list[str]:
    return [f.text for f in d.facts if f.reason == reason and not f.id.endswith(".heading")]


def draft(d: Doc) -> str:
    """초안 — 사실 문장을 그대로. 청구 기준은 사유마다 소제목 한 절"""
    lines = [f"# {d.title}", "", f"{d.topic}."]
    if d.effective:
        lines.append(f"시행일: {d.effective}")
    if d.type == "claim_standard":
        for r in d.sections:
            lines += ["", f"## {CLAIM_HEADINGS[r]}", "", *(f"- {t}." for t in _rules(d, r) or ["기록 기준을 지킨다"])]
    else:
        lines += [""] + [f"- {f.text}." for f in d.facts]
    return "\n".join(lines)


def payload(d: Doc) -> dict:
    """LLM 입력 — 방해 여부·종류(정답)는 넣지 않고 쓸 방향만"""
    p = {"type": d.type, "title": d.title, "topic": d.topic, "draft": draft(d)}
    if d.effective:
        p["effective_date"] = d.effective
    if d.type == "claim_standard":
        p["sections"] = [{"heading": CLAIM_HEADINGS[r], "rules": _rules(d, r)} for r in d.sections]
    else:
        p["facts"] = [f.text for f in d.facts]
    if d.kind == "similar":
        p["skip_topic"] = d.skip
    old = next((o for o in CATALOG if o.kind == "old_version" and o.pair == d.key), None)
    if old is not None:  # 최신 공지 — "기존 ○○에서"를 지어내지 않게 옛 버전 값을 준다
        p["previous"] = {"effective_date": old.effective, "facts": [f.text for f in old.facts]}
    if d.kind == "external":
        p["source"] = "다른 치과·일반 자료"
    else:
        p["clinic"] = CLINIC_NAME
    return p


def checks(d: Doc) -> dict:
    forbidden = []
    if d.kind == "similar":
        forbidden = [w for f in BY_KEY[d.pair].facts for w in f.words]
    elif d.kind == "external":
        forbidden = [CLINIC_NAME]
    return {"field": "body", "must_include": [[w] for f in d.facts for w in f.words], "forbidden_words": forbidden,
            "forbidden_patterns": llm_jobs.forbidden_patterns(), "length": length(d)}


def stage_plan(run, rng, fake) -> None:
    ids = {CATALOG[i].key: n for n, i in enumerate(rng.permutation(len(CATALOG)).tolist(), start=1)}
    old_of = {d.pair: d.key for d in CATALOG if d.kind == "old_version"}
    docs, meta = [], []
    for d in CATALOG:
        docs.append((ids[d.key], d.type, d.title, version(d), d.effective, path(d, ids[d.key]), draft(d)))
        facts = [{"id": f.id, "text": f.text, "words": list(f.words), "reason": f.reason} for f in d.facts]
        meta.append((ids[d.key], d.key, d.kind, int(d.kind != "core"), ids.get(d.pair),
                     ids.get(old_of.get(d.key)), json.dumps(facts, ensure_ascii=False)))
    run.conn.executemany("INSERT INTO ax_documents (id, type, title, version, effective_date, path, body)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?)", docs)
    run.conn.executemany("INSERT INTO truth.truth_doc_meta VALUES (?, ?, ?, ?, ?, ?, ?)", meta)


def stage_bodies(run, rng, fake) -> None:
    ctx = run.state["llm"]
    for doc_id, key in run.conn.execute("SELECT doc_id, doc_key FROM truth.truth_doc_meta ORDER BY doc_id").fetchall():
        d = BY_KEY[key]
        out = ctx.resolve(TASK, str(doc_id), payload(d), checks(d))
        if out is not None:
            run.conn.execute("UPDATE ax_documents SET body = ? WHERE id = ?", (out["body"], doc_id))
