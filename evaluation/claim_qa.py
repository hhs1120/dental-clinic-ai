"""⑤-3 청구 기준 질문 평가셋 (설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §3, 평가 설계 §5 ⑤-3·§3.1).
상세 사유마다 코드를 말하지 않는 직원 말투 질문을 AI(별칭 gen)가 초안으로 쓰고, 베끼기 경고(정답 절 본문과 연속 4어절·
절 제목·사유 코드·사유 문구)가 없는 것만 사람 검토에 올린다. 검토는 evaluation/ 안의 로컬 페이지(review) — 승인 JSON은
ground_truth/claim_qa.json, make data가 truth_claim_qa에 적재한다. dev/test는 사유 단위(사유 키 정렬 순서로 번갈아).
정답이 붙은 입력이라 초안·호출 기록은 evaluation/ 파일에만 쓴다(app.sqlite에 쓰지 않는다).
uv run --env-file .env python -m evaluation.claim_qa draft   (과금, 이어 돎 — make claim-qa-draft)"""

import argparse
import json
import re
from pathlib import Path

from generator.doc_catalog import CLAIM_HEADINGS
from generator.master import ADJUST_REASONS
from shared.llm import Gateway, JsonlLog, load_config

ROOT = Path(__file__).resolve().parents[1]
DRAFTS = ROOT / "evaluation" / "claim_qa_drafts.jsonl"
LLM_LOG = ROOT / "evaluation" / "claim_qa_llm.jsonl"  # ax_llm_calls에 쓰지 않는다
DRAFT_ALIAS, DRAFT_TASK = "gen", "claim-qa-draft"
DRAFT_PROMPT = ROOT / "prompts" / "gen_claim_qa.md"
PER_REASON, DRAFT_ATTEMPTS = 2, 3
COPY_WORDS = 4  # 베끼기 — 정답 절 본문과 연속 어절 수(평가 설계 §3.1)
DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"questions": {"type": "array", "items": {"type": "string"}}},
    "required": ["questions"],
    "additionalProperties": False,
}
_WORD = re.compile(r"[0-9A-Za-z가-힣/·]+")


def reasons(conn) -> list[dict]:
    """상세 사유 11개 — {reason, heading, note, body}. body는 source의 청구 기준 문서(ax_documents claim_standard)
    에서 그 절 본문(LLM이 쓴 문서라 코드에 없다)"""
    from backend.rag_index import _sections

    bodies = {}
    for title, body in conn.execute(
        "SELECT title, body FROM ax_documents WHERE type = 'claim_standard'"
    ):
        bodies.update({h: b.strip() for h, b in _sections(title, body)})
    return [
        {
            "reason": k,
            "heading": h,
            "note": ADJUST_REASONS[k]["note"],
            "body": bodies.get(h, ""),
        }
        for k, h in sorted(CLAIM_HEADINGS.items())
    ]


def split_of(reason: str) -> str:
    """사유 키 정렬 순서로 dev·test 번갈아(짝수 번째 dev) — 같은 문서의 다른 절이 양쪽에 걸칠 수 있다"""
    return "dev" if sorted(CLAIM_HEADINGS).index(reason) % 2 == 0 else "test"


def _grams(text: str, n: int) -> set[tuple]:
    words = _WORD.findall(text)
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def copy_warnings(question: str, reason: dict) -> list[str]:
    """베끼기 경고 — 정답 절 본문과 연속 COPY_WORDS어절이 겹치거나, 절 제목·사유 코드·상세 사유 문구를 그대로 쓴 질문"""
    out = []
    if _grams(question, COPY_WORDS) & _grams(reason["body"], COPY_WORDS):
        out.append(f"기준 본문과 {COPY_WORDS}어절 이상 겹침")
    if reason["heading"] in question:
        out.append(f"절 제목 '{reason['heading']}'")
    if re.search(
        rf"(?<![A-Za-z0-9]){reason['reason']}(?![0-9])", question
    ):  # 'R02로'처럼 조사가 붙어도
        out.append(f"사유 코드 '{reason['reason']}'")
    if reason["note"] in question:
        out.append("상세 사유 문구 그대로")
    return out


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def _ask(reason: dict, n: int, problems: list[str]) -> list[dict]:
    body = {
        "사유": reason["note"],
        "기준 절": {"제목": reason["heading"], "본문": reason["body"]},
        "몇 개": n,
    }
    if problems:
        body["지난 답의 문제"] = problems
    return [{"role": "user", "content": json.dumps(body, ensure_ascii=False)}]


def draft(gw, path: Path = DRAFTS, *, reasons: list[dict], per_reason: int = PER_REASON) -> dict:
    """사유마다 경고 없는 질문이 per_reason개 될 때까지 최대 DRAFT_ATTEMPTS번 쓴다(다시 쓸 때 걸린 이유를 알려 준다).
    이미 per_reason개 있는 사유는 건너뛴다(이어 돎). 모자란 사유는 short로 돌려준다"""
    have: dict[str, list[str]] = {}
    for r in _read(path):
        have.setdefault(r["reason"], []).append(r["question"])
    system = DRAFT_PROMPT.read_text(encoding="utf-8")
    out = {"written": 0, "skipped": 0, "short": []}
    for reason in reasons:
        got = have.get(reason["reason"], [])
        if len(got) >= per_reason:
            out["skipped"] += 1
            continue
        problems: list[str] = []
        for _ in range(DRAFT_ATTEMPTS):
            r = gw.complete(
                DRAFT_ALIAS,
                task=DRAFT_TASK,
                system=system,
                messages=_ask(reason, per_reason - len(got), problems),
                schema=DRAFT_SCHEMA,
            )
            asked = r.data.get("questions", []) if r.ok and isinstance(r.data, dict) else []
            problems = []
            for q in (q.strip() for q in asked):
                warn = copy_warnings(q, reason)
                if warn:
                    problems.append(f"'{q}' — {', '.join(warn)}")
                elif q and q not in got and len(got) < per_reason:
                    got.append(q)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with path.open("a", encoding="utf-8") as f:
                        row = {
                            "id": f"cq-{reason['reason']}-{len(got)}",
                            "reason": reason["reason"],
                            "question": q,
                        }
                        f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    out["written"] += 1
            if len(got) >= per_reason:
                break
        else:
            out["short"].append(reason["reason"])
    return out


def _source():
    import sqlite3

    from generator.paths import output_paths

    conn = sqlite3.connect(f"{output_paths()['source'].resolve().as_uri()}?mode=ro", uri=True)
    return conn


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m evaluation.claim_qa")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("draft", help="질문 초안(과금, 이어 돎)")
    args = p.parse_args(argv)
    if args.command == "draft":
        gw = Gateway(load_config(), JsonlLog(LLM_LOG), scope="generator")
        print(draft(gw, reasons=reasons(_source())))


if __name__ == "__main__":
    main()
