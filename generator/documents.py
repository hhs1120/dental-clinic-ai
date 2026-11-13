"""9단계 RAG 문서 — ax_documents·truth_doc_meta (가상 데이터 생성 설계 §4.2 '9단계 구현 결정').
두 단계, 9b(차트 서술)와 같은 틀:
- doc_plan(코드): doc_catalog 표로 문서·정답을 쓰고, 본문에는 사실을 그대로 옮긴 초안을 넣는다(가짜 게이트웨이면 남음).
  문서 번호는 시드 셔플, 경로는 번호 — 표 순서(핵심 → 방해)나 표 이름(sim-·old-)이 source.sqlite에 드러나지 않게.
  나중에 더한 문서(APPENDED)는 셔플에서 빼고 뒤 번호를 붙인다 — 기존 번호(캐시·③ 질문 정답)를 그대로 둔다
- documents(LLM): 초안을 문서로 다시 쓴다 — 심은 사실 포함, 비슷한 주제는 짝의 사실 없음(llm_jobs 검증).
  건 id는 문서 번호(작업 파일에도 표 이름이 없게). 청구 기준은 계획 단계 본문이 최종(generator/claim_standards.py — LLM에 안 보냄)
"""

import json

from generator import llm_jobs
from generator.claim_standards import INTRO_NOTE, section_body
from generator.doc_catalog import BY_KEY, CATALOG, CLAIM_HEADINGS, Doc
from generator.llm_jobs import Task
from generator.master import CLINIC_NAME

# 본문 글자 수(공백 제외) — 생성 검증. 공지는 짧게
DOC_LENGTH_CHARS = {"notice": (150, 1500), "default": (400, 3000)}
SCHEMA = {
    "type": "object",
    "properties": {
        "body": {"type": "string", "description": "마크다운 문서 본문 (첫 줄 '# 제목')"}
    },
    "required": ["body"],
    "additionalProperties": False,
}
TASK = Task(name="document", prompt="gen_document", schema=SCHEMA, store=("body",), fake=False)
# 목록에 나중에 더한 문서 키 — 더한 순서대로 셔플된 번호 뒤에 붙인다(§4.2 '저장'). 문서를 더할 때 여기에도 뒤에 넣는다
APPENDED = ("manual-oral-care-sales", "manual-plan-followup")


def version(d: Doc) -> str:
    return "2" if d.type == "notice" and d.kind == "core" else "1"


def path(d: Doc, doc_id: int) -> str:
    return f"docs/{d.type}/{doc_id:03d}.md"


def length(d: Doc) -> list[int]:
    return list(DOC_LENGTH_CHARS.get(d.type, DOC_LENGTH_CHARS["default"]))


def _words(d: Doc) -> list[str]:
    return [w for f in d.facts for w in f.words]


def draft(d: Doc) -> str:
    """초안 — 사실 문장을 그대로. 청구 기준은 사유마다 소제목 한 절의 최종 본문(generator/claim_standards.py)"""
    if (
        d.type == "claim_standard"
    ):  # 최종 본문 — 원문 발췌를 글자 그대로 두려고 LLM에 보내지 않는다(stage_bodies)
        lines = [f"# {d.title}", "", f"{d.topic}. {INTRO_NOTE}"]
        for r in d.sections:
            lines += ["", f"## {CLAIM_HEADINGS[r]}", "", section_body(r)]
        return "\n".join(lines)
    lines = [f"# {d.title}", "", f"{d.topic}."]
    if d.effective:
        lines.append(f"시행일: {d.effective}")
    lines += [""] + [f"- {f.text}." for f in d.facts]
    return "\n".join(lines)


def payload(d: Doc) -> dict:
    """LLM 입력 — 방해 여부·종류(정답)는 넣지 않고 쓸 방향만"""
    p = {"type": d.type, "title": d.title, "topic": d.topic, "draft": draft(d)}
    if d.effective:
        p["effective_date"] = d.effective
    p["facts"] = [f.text for f in d.facts]
    if d.kind == "similar":
        p["skip_topic"] = d.skip
    old = next((o for o in CATALOG if o.kind == "old_version" and o.pair == d.key), None)
    if (
        old is not None and not d.recurring
    ):  # 최신 공지 — "기존 ○○에서"를 지어내지 않게 옛 버전 값(휴진처럼 해마다 내는 공지는 뺌)
        p["previous"] = {"effective_date": old.effective, "facts": [f.text for f in old.facts]}
    if d.kind == "external":
        p["source"] = "다른 치과·일반 자료"
    else:
        p["clinic"] = CLINIC_NAME
    return p


def checks(d: Doc) -> dict:
    forbidden = []
    if d.kind == "similar":  # 짝과 주제가 겹치는 핵심 문서(avoid)의 사실 말 금지
        forbidden = [w for k in (d.pair, *d.avoid) for w in _words(BY_KEY[k])]
    elif d.kind == "external":
        forbidden = [CLINIC_NAME]
    c = {
        "field": "body",
        "must_include": [[w] for w in _words(d)],
        "forbidden_words": forbidden + list(d.forbidden),
        "forbidden_patterns": llm_jobs.forbidden_patterns(),
        "length": length(d),
    }
    return c


def stage_plan(run, rng, fake) -> None:
    base = [d for d in CATALOG if d.key not in APPENDED]
    ids = {base[i].key: n for n, i in enumerate(rng.permutation(len(base)).tolist(), start=1)}
    ids |= {key: len(base) + n for n, key in enumerate(APPENDED, start=1)}
    old_of = {d.pair: d.key for d in CATALOG if d.kind == "old_version"}
    docs, meta = [], []
    for d in CATALOG:
        docs.append(
            (ids[d.key], d.type, d.title, version(d), d.effective, path(d, ids[d.key]), draft(d))
        )
        facts = [
            {
                "id": f.id,
                "text": f.text,
                "words": list(f.words),
                "reason": f.reason,
                "same": f.same,
            }
            for f in d.facts
        ]
        meta.append(
            (
                ids[d.key],
                d.key,
                d.kind,
                int(d.kind != "core"),
                ids.get(d.pair),
                ids.get(old_of.get(d.key)),
                json.dumps(sorted(ids[k] for k in d.avoid)),
                json.dumps(facts, ensure_ascii=False),
            )
        )
    run.conn.executemany(
        "INSERT INTO ax_documents (id, type, title, version, effective_date, path, body)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        docs,
    )
    run.conn.executemany("INSERT INTO truth.truth_doc_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?)", meta)


def stage_bodies(run, rng, fake) -> None:
    ctx = run.state["llm"]
    for doc_id, key in run.conn.execute(
        "SELECT doc_id, doc_key FROM truth.truth_doc_meta ORDER BY doc_id"
    ).fetchall():
        d = BY_KEY[key]
        if d.type == "claim_standard":  # 본문은 계획 단계 초안 그대로(draft)
            continue
        out = ctx.resolve(TASK, str(doc_id), payload(d), checks(d))
        if out is not None:
            run.conn.execute(
                "UPDATE ax_documents SET body = ? WHERE id = ?", (out["body"], doc_id)
            )
