"""eval.py — 평가 실행. 정의는 docs/치과 AX - 평가 설계.md §7.

  uv run python -m evaluation.eval --split dev [--retrieval-only] [--prompt-version v2] [--condition reranker=off]
  uv run python -m evaluation.eval --split test --section classification|retrieval|chat [--prompt-version v2] [--condition …] [--force]
  → reports/eval_<split>_<condition>_<시각>.md + 같은 이름 .json(make gate가 읽음) + reports/eval_runs.jsonl 1줄

- DB는 app.sqlite(앱 결과)에 source.sqlite(생성 데이터)·truth.sqlite(정답)를 붙여 읽는다(앱 설계 §7).
- 분류(①)는 앱이 쓴 분류·판정·투표 테이블을 읽기만 한다. RAG(③)는 앱의 search·chat을 같은 프로세스에서 호출한다(rag.RagApp,
  backend/eval_hooks.py의 make_app(app_db, source_db)) — 앱이 없으면 RAG 절은 건너뛴다. 평가 때 게이트웨이 파일 캐시·is_eval 기록은 앱 쪽 훅의 일
- test는 묶음마다 1회: 한 번에 묶음 하나(분류·버전 / 검색·조건 / 챗·조건), 이력에 같은 묶음이 있으면 AlreadyRun — 다시는 --force.
  test의 --condition은 기본 조건을 다시 돌리지 않고 이력의 기본 조건 test 결과(문항별 통과 여부)와 비교한다
- 건별 목록(틀린 통화, 구제/손실·실패 문항)은 dev에서만
- 준비표(④)·모델 2개 교차검증(§2.5)은 해당 기능 착수 때
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import yaml

from evaluation import EvalRefused, classification, rag
from evaluation.metrics import fraction
from generator.paths import ROOT, output_paths

REPORTS = ROOT / "reports"
PROMPTS = ROOT / "prompts"
RUNS = "eval_runs.jsonl"
SECTIONS = (
    "classification",
    "retrieval",
    "chat",
)  # test 묶음 — 분류는 버전, 검색·챗은 조건 단위로 1회


class AlreadyRun(Exception):
    pass


def fewshot_uids(path: Path) -> set[str]:
    """프롬프트 파일 머리말(--- 사이 YAML)의 fewshot_call_uids — 파일·머리말·키가 없으면 빈 집합, 형식이 틀리면 ValueError
    (조용히 빈 집합이면 예시 통화가 dev 집계에 섞인다)"""
    if not path.exists():
        return set()
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return set()
    m = re.match(r"---\n(.*?)\n---(?:\n|$)", text, re.S)
    if not m:
        raise EvalRefused(f"{path}: 머리말이 '---'로 닫히지 않음")
    uids = (yaml.safe_load(m[1]) or {}).get("fewshot_call_uids", [])
    if not isinstance(uids, list) or not all(isinstance(u, str) for u in uids):
        raise EvalRefused(f"{path}: fewshot_call_uids는 call_uid 목록이어야 함")
    return set(uids)


def _history(reports_dir: Path) -> list[dict]:
    p = reports_dir / RUNS
    return (
        [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line]
        if p.exists()
        else []
    )


def _stored_base(reports_dir: Path, history: list[dict]) -> list[dict] | None:
    """이력에서 가장 최근의 쓸 수 있는 기본 조건 test 결과(문항별 통과 여부) — 없거나 깨진 json은 건너뛴다"""
    for h in reversed(history):
        path = (reports_dir / h["report_path"]).with_suffix(".json")
        if h["condition"] != "base" or not path.exists():
            continue
        try:
            r = json.loads(path.read_text(encoding="utf-8")).get("rag") or {}
        except json.JSONDecodeError:
            continue
        if r.get("paired"):
            return r["paired"]
    return None


def _key(section: str, condition: str, prompt_version: str) -> str:
    return prompt_version if section == "classification" else condition


def run(
    conn,
    reports_dir: Path | None = None,
    app: rag.RagApp | None = None,
    split: str = "dev",
    section: str | None = None,
    retrieval_only: bool = False,
    prompt_version: str | None = None,
    condition: str | None = None,
    force: bool = False,
    prompts_dir: Path | None = None,
) -> dict:
    """section = test 묶음(SECTIONS 하나). dev는 없으면 전부(--retrieval-only면 챗 빼고)"""
    reports_dir, prompts_dir = reports_dir or REPORTS, prompts_dir or PROMPTS
    if condition == "base":
        raise EvalRefused("base는 기본 조건 이름 — --condition 없이 돌린다")
    cond, pv = condition or "base", prompt_version or "all"
    dev = split == "dev"
    if dev and section:
        raise EvalRefused(
            "--section은 test 묶음용 — dev는 묶음 없이 전부 돌린다(검색만은 --retrieval-only)"
        )
    if not dev:
        # 돌리지 못할 이유는 리포트·이력을 쓰기 전에 — 앱 없음·오타가 test 1회 기회를 소진하지 않게
        section = section or ("retrieval" if retrieval_only else None)
        if section not in SECTIONS:
            raise EvalRefused(
                "test는 --section 하나(classification·retrieval·chat)만 돌린다 (평가 설계 §7)"
            )
        if section == "classification" and not prompt_version:
            raise EvalRefused("test 분류는 --prompt-version 하나")
        mismatch = (
            "--retrieval-only"
            if retrieval_only and section != "retrieval"
            else "--condition"
            if condition and section == "classification"
            else "--prompt-version"
            if prompt_version and section != "classification"
            else None
        )
        if mismatch:
            raise EvalRefused(
                f"test {section} 묶음에는 {mismatch}를 쓰지 않는다 — 리포트·이력에 맞지 않는 값이 남음"
            )
        if (
            section == "classification"
            and not conn.execute(
                "SELECT 1 FROM ax_call_classifications WHERE prompt_version = ? LIMIT 1",
                (prompt_version,),
            ).fetchone()
        ):
            raise EvalRefused(f"분류 버전 {prompt_version}의 분류 기록이 없음")
        if section != "classification" and app is None:
            raise EvalRefused("앱 없음 — test RAG는 앱(backend/eval_hooks.py)이 생긴 뒤에")
        history = [
            h
            for h in _history(reports_dir)
            if h["split"] == "test" and h.get("section") == section
        ]
        if not force and any(
            _key(section, h["condition"], h["prompt_version"]) == _key(section, cond, pv)
            for h in history
        ):
            raise AlreadyRun(
                f"test {section}({_key(section, cond, pv)})은 이미 돌렸다 — 다시 돌리려면 --force (평가 설계 §1 원칙 2)"
            )
    started = datetime.now()
    do_cls = dev or section == "classification"
    parts = (
        (("retrieval",) if retrieval_only else rag.PARTS)
        if dev
        else (section,)
        if section != "classification"
        else ()
    )
    result = {
        "split": split,
        "section": section or "all",
        "condition": cond,
        "prompt_version": pv,
        "retrieval_only": retrieval_only,
        "classification": None,
        "rag": None,
    }
    if do_cls:
        versions = (
            [prompt_version]
            if prompt_version
            else sorted(
                {
                    r[0]
                    for r in conn.execute(
                        "SELECT DISTINCT prompt_version FROM ax_call_classifications"
                    )
                },
                key=classification.version_key,
            )
        )
        fewshot = {v: fewshot_uids(prompts_dir / "classify" / f"{v}.md") for v in versions}
        test_uids = {
            r[0]
            for r in conn.execute(
                "SELECT call_uid FROM truth.truth_call_labels WHERE split = 'test'"
            )
        }
        leaked = sorted(u for uids in fewshot.values() for u in uids if u in test_uids)
        if leaked:  # test 통화를 프롬프트 예시로 쓰면 test 점수가 새는 셈 — 평가 설계 §2.1
            raise EvalRefused(f"few-shot에 test 통화가 있음: {', '.join(leaked)}")
        result["classification"] = classification.evaluate(
            conn, split, versions, fewshot, dump=dev
        )
    if parts and app is not None:
        r = rag.evaluate(conn, app, split, condition, parts)
        if condition and dev:
            r["compare"] = rag.compare(
                rag.evaluate(conn, app, split, None, parts)["questions"], r["questions"]
            )
        elif condition:
            base = _stored_base(reports_dir, history)
            r["compare"] = (
                {"metrics": rag.compare(base, rag.paired(r))["metrics"]} if base else None
            )
        if not dev:
            r["paired"] = rag.paired(r)
            r.pop("questions")
        result["rag"] = r
    reports_dir.mkdir(parents=True, exist_ok=True)
    stem = f"eval_{split}_{re.sub(r'[^0-9A-Za-z._-]+', '-', cond)}_{started:%Y%m%d-%H%M%S}"
    md = reports_dir / f"{stem}.md"
    n = 1
    while md.exists():
        n += 1
        md = reports_dir / f"{stem}-{n}.md"
    md.write_text(
        render(result, started, app_missing=bool(parts) and app is None), encoding="utf-8"
    )
    md.with_suffix(".json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    with (reports_dir / RUNS).open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "split": split,
                    "section": result["section"],
                    "condition": cond,
                    "prompt_version": pv,
                    "started_at": started.isoformat(timespec="seconds"),
                    "report_path": md.name,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return {"report_path": md, "result": result}


# ── 리포트 (md) ─────────────────────────────────────────────


def _pct(x) -> str:
    return "—" if x is None else f"{int(100 * x + 0.5)}%"


def _kn(d) -> str:
    return fraction(d["k"], d["n"])


def _cell(x) -> str:
    """md 표 칸 — '|'·줄바꿈이 표를 깨지 않게"""
    return str(x if x is not None else "").replace("|", "\\|").replace("\n", " ")


def render(r: dict, started: datetime, app_missing: bool = False) -> str:
    out = [
        f"# 평가 리포트 — {r['split']} · {r['section']} · {r['condition']} · {started:%Y-%m-%d %H:%M}",
        "",
        f"prompt_version: {r['prompt_version']}"
        + (" · 검색만(--retrieval-only)" if r["retrieval_only"] else ""),
        "",
    ]
    if r["classification"] is not None:
        out += _render_classification(r["classification"])
    if r["rag"] is not None or app_missing:
        out += _render_rag(r["rag"])
    return "\n".join(out) + "\n"


def _render_classification(c: dict) -> list[str]:
    out = ["## ① 분류", ""]
    if not c["versions"]:
        out += ["분류 기록 없음", ""]
    for v, x in c["versions"].items():
        g, h = x["model_vs_gold"], x["model_vs_human"]
        out += [
            f"### {v} ({', '.join(x['model_actual'])})",
            "",
            f"목적 Strict — 모델 vs 골드 {_kn(g['purpose'])} · 가중 {_pct(g['purpose']['weighted'])} · 단순 평균 {_pct(g['purpose']['macro'])}"
            f" / 모델 vs 사람 {_kn(h['purpose'])} · 가중 {_pct(h['purpose']['weighted'])}",
            "카테고리별 n(모델 vs 골드) — "
            + " · ".join(f"{c} {_kn(d)}" for c, d in g["purpose"]["by_category"].items()),
            "",
            "| 항목 | 모델 vs 골드 | 모델 vs 사람 |",
            "|---|---|---|",
        ]
        out += [
            f"| {i} | {_kn(g['items'][i])} | {_kn(h['items'][i])} |" for i in classification.ITEMS
        ]
        e = x["excluded"]
        out += [
            "",
            f"제외 — 폴백 {e['fallback']} · 실패 {e['failed']} · few-shot {e['fewshot']}",
            "",
        ]
    hg = c["human_vs_gold"]
    out += [
        f"### 사람 vs 골드 (라벨 타당성) — 목적 {_kn(hg['purpose'])} · 가중 {_pct(hg['purpose']['weighted'])}",
        "",
    ]
    for cmp in c["compare"]:
        ref = "골드" if cmp["ref"] == "gold" else "사람"
        out += [
            f"### 버전 비교 {cmp['a']} → {cmp['b']} — 모델 vs {ref} (교집합 {cmp['n']}건)",
            "",
            "| 항목 | 전 | 후 | p |",
            "|---|---|---|---|",
        ]
        out += [
            f"| {i} | {fraction(d['a'], cmp['n'])} | {fraction(d['b'], cmp['n'])} | {d['p']:.3f} |"
            for i, d in cmp["items"].items()
        ]
        out.append("")
    for b in c["blind"]:
        a, z = b["sides"]
        total = a["wins"] + z["wins"] + b["tie"] + b["both_wrong"]
        out += [
            f"### 블라인드 {a['version']}({a['model']}) vs {z['version']}({z['model']}) — {total}표",
            "",
            f"승 {a['version']} {a['wins']} · {z['version']} {z['wins']} · 비슷 {b['tie']} · 둘 다 틀림 {b['both_wrong']}"
            f" / 위치 왼쪽 {b['left']} · 오른쪽 {b['right']}",
            "",
        ]
    if c.get("dump"):
        out += [
            "### 틀린 항목 (dev 판정 풀 — 사람 판정, 없으면 골드 기준)",
            "",
            "| 통화 | 버전 | 항목 | 모델 | 사람 | 골드 | 전사 |",
            "|---|---|---|---|---|---|---|",
        ]
        out += [
            f"| {d['call_uid']} | {d['version']} | {d['item']} | {_cell(d['model'])} | {_cell(d['human'])} | {_cell(d['gold'])} |"
            f" {_cell((d['transcript'] or '')[:60])}… |"
            for d in c["dump"]
        ]
        out.append("")
    return out


def _render_rag(r: dict | None) -> list[str]:
    out = ["## ③ RAG", ""]
    if r is None:
        return out + ["앱 없음 — 건너뜀(앱 착수 후 backend가 rag.RagApp의 search·chat을 구현)", ""]
    s, ch = r["retrieval"], r["chart"]
    if s:
        out += [
            f"검색(문서 검색형) — Hit@5 {_kn(s['hit5'])} · MRR@5 {_num(s['mrr'])} · Top-1 {_kn(s['top1'])} · 최신성 {_kn(s['fresh'])}",
            f"차트형 — Hit@5 {_kn(ch['hit5'])} · MRR@5 {_num(ch['mrr'])}",
            "",
        ]
    if r["chat"]:
        c = r["chat"]
        out += [
            f"챗 — 인용 정확도 {_kn(c['citation'])} · 도구 선택 {_kn(c['tool'])} · 환각 억제 {_kn(c['refusal'])}",
            "",
        ]
    if "compare" in r and r["compare"] is None:
        out += [
            f"### 조건 비교 기본 → {r['condition']}",
            "",
            "기본 조건 test 결과가 이력에 없음 — 기본 조건 test를 먼저",
            "",
        ]
    elif "compare" in r:
        out += [
            f"### 조건 비교 기본 → {r['condition']}",
            "",
            "| 지표 | 기본 | 조건 | p |",
            "|---|---|---|---|",
        ]
        out += [
            f"| {k} | {_num(m['base'])} | {_num(m['cond'])} | — |"
            if "p" not in m
            else f"| {k} | {fraction(m['base'], m['n'])} | {fraction(m['cond'], m['n'])} | {m['p']:.3f} |"
            for k, m in r["compare"]["metrics"].items()
        ]
        for k in r["compare"].get("rescued", {}):
            if r["compare"]["rescued"][k] or r["compare"]["lost"][k]:
                out.append(
                    f"- {k}: 구제 {', '.join(r['compare']['rescued'][k]) or '—'} / 손실 {', '.join(r['compare']['lost'][k]) or '—'}"
                )
        out.append("")
    fails = [q for q in r.get("questions", []) if q.get("hit") is False or q.get("chat_fail")]
    if fails:
        out += [
            "### 실패 문항 (dev)",
            "",
            "| 문항 | 유형 | 순위 | 챗 | 질문 |",
            "|---|---|---|---|---|",
        ]
        out += [
            f"| {q['id']} | {q['type']} | {q.get('rank') or '—'} | {_cell(q.get('chat_fail'))} | {_cell(q['question'])} |"
            for q in fails
        ]
        out.append("")
    return out


def _num(x) -> str:
    return "—" if x is None else f"{x:.3f}"


def load_app(db: Path, source: Path) -> rag.RagApp | None:
    """앱의 평가 훅 backend/eval_hooks.py의 make_app(app_db, source_db) — eval.py가 채점하는 DB를 앱도 읽게. 훅 모듈이 없으면(앱 착수 전)
    None, 훅 안의 의존성이 없으면 그대로 오류(RAG가 조용히 빠진 리포트를 막음)"""
    try:
        from backend.eval_hooks import make_app  # noqa: PLC0415 — 앱이 생긴 뒤에만 있는 모듈
    except ModuleNotFoundError as e:
        if e.name in ("backend", "backend.eval_hooks"):
            return None
        raise
    return make_app(db, source)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--split", choices=("dev", "test"), required=True)
    parser.add_argument("--section", choices=SECTIONS, help="test 묶음 하나(test는 필수)")
    parser.add_argument(
        "--retrieval-only",
        action="store_true",
        help="LLM 없이 검색 지표만($0). test에선 --section retrieval",
    )
    parser.add_argument("--prompt-version", help="분류 버전 하나만. 없으면 분류가 있는 버전 전부")
    parser.add_argument("--condition", help="검색·챗 조건(예: reranker=off) — 기본 조건과 짝 비교")
    parser.add_argument("--force", action="store_true", help="같은 조건의 test를 다시 돌림")
    parser.add_argument("--db", type=Path, help="app.sqlite")
    parser.add_argument("--source", type=Path, help="source.sqlite(생성기 출력)")
    parser.add_argument("--truth", type=Path)
    parser.add_argument("--reports-dir", type=Path, default=REPORTS)
    args = parser.parse_args(argv)
    paths = output_paths()
    db, source = args.db or paths["app"], args.source or paths["source"]
    truth = args.truth or paths["truth"]
    for p in (db, source, truth):
        if not p.exists():
            print(f"DB 없음: {p} — make data 먼저")
            return 1
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS source", (f"file:{source}?mode=ro",))
    conn.execute("ATTACH DATABASE ? AS truth", (f"file:{truth}?mode=ro",))
    try:
        out = run(
            conn,
            reports_dir=args.reports_dir,
            app=load_app(db, source),
            split=args.split,
            section=args.section,
            retrieval_only=args.retrieval_only,
            prompt_version=args.prompt_version,
            condition=args.condition,
            force=args.force,
        )
    except AlreadyRun as e:
        print(e)
        return 2
    except EvalRefused as e:
        print(e)
        return 1
    print(f"리포트: {out['report_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
