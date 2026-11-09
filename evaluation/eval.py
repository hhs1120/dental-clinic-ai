"""eval.py — 평가 실행. 정의는 docs/치과 AX - 평가 설계.md §7.

  uv run python -m evaluation.eval --split dev [--retrieval-only] [--prompt-version v2] [--condition reranker=off]
  uv run python -m evaluation.eval --split test --section classification|judge|crosscheck|auto|retrieval|chat [--prompt-version v2] [--condition …] [--force]
  → reports/eval_<split>_<condition>_<시각>.md + 같은 이름 .json(make gate가 읽음) + reports/eval_runs.jsonl 1줄

- DB는 app.sqlite(앱 결과)에 source.sqlite(생성 데이터)·truth.sqlite(정답)를 붙여 읽는다(앱 설계 §7).
- 분류(①)는 앱이 쓴 분류·판정·투표 테이블을 읽기만 한다. RAG(③)는 앱의 search·chat을 같은 프로세스에서 호출한다(rag.RagApp,
  backend/eval_hooks.py의 make_app(app_db, source_db)) — 앱이 없으면 RAG 절은 건너뛰고, 앱 경고(rag 색인·리랭커)가 있으면 test를 거절한다. 평가 때 게이트웨이 파일 캐시·is_eval 기록은 앱 쪽 훅의 일
- test는 묶음마다 1회: 한 번에 묶음 하나(분류·버전 / 판정·판정기 버전 / 교차검증·자동 종결·분류 버전 × 판정기 버전 / 검색·조건 / 챗·조건),
  이력에 같은 묶음이 있으면 AlreadyRun — 다시는 --force.
  test의 --condition은 기본 조건을 다시 돌리지 않고 이력의 기본 조건 test 결과(문항별 통과 여부)와 비교한다
- 건별 목록(틀린 통화, 구제/손실·실패 문항)은 dev에서만
- 준비표(④)·자동 처리(§5 — 앱의 조건 함수를 쓴다)는 해당 기능 착수 때
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import yaml

from backend.calls import active_versions
from evaluation import (
    EvalRefused,
    autoclose,
    call_actions,
    classification,
    followup,
    gold_audit,
    rag,
)
from evaluation.metrics import fraction
from generator.paths import ROOT, output_paths

REPORTS = ROOT / "reports"
PROMPTS = ROOT / "prompts"
RUNS = "eval_runs.jsonl"
SECTIONS = (
    "classification",
    "judge",
    "crosscheck",
    "auto",
    "retrieval",
    "chat",
)  # test 묶음 — 분류·판정·교차검증·자동 종결은 버전, 검색·챗은 조건 단위로 1회
VERSIONED = ("classification", "judge", "crosscheck", "auto")


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
    return prompt_version if section in VERSIONED else condition


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
    gold_audit_path: Path | None = None,
    followup_runs: Path | None = None,
    call_actions_path: Path | None = None,
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
                "test는 --section 하나(classification·judge·crosscheck·auto·retrieval·chat)만 돌린다 (평가 설계 §7)"
            )
        if section in VERSIONED and not prompt_version:
            raise EvalRefused(f"test {section}는 --prompt-version 하나")
        mismatch = (
            "--retrieval-only"
            if retrieval_only and section != "retrieval"
            else "--condition"
            if condition and section in VERSIONED
            else "--prompt-version"
            if prompt_version and section not in VERSIONED
            else None
        )
        if mismatch:
            raise EvalRefused(
                f"test {section} 묶음에는 {mismatch}를 쓰지 않는다 — 리포트·이력에 맞지 않는 값이 남음"
            )
        table, what = (
            ("ax_model_judgments", "판정기")
            if section == "judge"
            else ("ax_call_classifications", "분류")
        )
        if (
            section in VERSIONED
            and not conn.execute(
                f"SELECT 1 FROM {table} WHERE prompt_version = ? LIMIT 1", (prompt_version,)
            ).fetchone()
        ):
            raise EvalRefused(f"{what} 버전 {prompt_version}의 기록이 없음")
        if section == "crosscheck":
            jv = classification.judge_version(conn)
            if not jv:
                raise EvalRefused(
                    "교차검증할 판정기 버전을 못 정함 — 판정기 버전이 여럿이면 ax_settings.prompt_versions의 judge"
                )
            x = classification.crosscheck(conn, split, prompt_version, jv)
            if "skipped" in x or not x["n"]:
                raise EvalRefused(
                    f"교차검증 {prompt_version}×{jv}: {x.get('skipped') or '판정기와 겹치는 통화가 없음'}"
                )
            pv = f"{prompt_version}×{jv}"
        if section == "auto":
            jv = classification.judge_version(conn)
            if not jv:
                raise EvalRefused("자동 종결을 잴 판정기 버전을 못 정함")
            pv = f"{prompt_version}×{jv}"
        if section not in VERSIONED and app is None:
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
    has_chat = app is None or getattr(app, "supports_chat", True)
    if section == "chat" and not has_chat:
        raise EvalRefused("챗이 없는 앱 — ③ 챗 착수 뒤에 돌린다")
    started = datetime.now()
    do_cls = dev or section == "classification"
    parts = (
        (("retrieval",) if retrieval_only else rag.PARTS)
        if dev
        else (section,)
        if section not in VERSIONED
        else ()
    )
    parts = tuple(p for p in parts if p != "chat" or has_chat)
    result = {
        "split": split,
        "section": section or "all",
        "condition": cond,
        "prompt_version": pv,
        "retrieval_only": retrieval_only,
        "classification": None,
        "judge": None,
        "crosscheck": None,
        "rag": None,
        "gold_audit": None,
        "autoclose": None,
        "followup": None,
    }
    gold_drop = None
    if (dev or section == "judge") and gold_audit_path and gold_audit_path.exists():
        jv = prompt_version if section == "judge" else classification.judge_version(conn)
        if not jv:
            result["gold_audit"] = {"error": "판정기 버전을 못 정함"}
        else:  # 골드 점검(평가 설계 §2.3) — 오류 추정 제외는 dev만, test는 원래 골드 수치뿐
            cur, stale = gold_audit.current(gold_audit.load(gold_audit_path), conn, jv)
            result["gold_audit"] = {
                "judge_version": jv,
                "summary": gold_audit.summarize(cur),
                "stale": stale,
                "missing": len(gold_audit.targets(conn, jv)) - len(cur),
            }
            gold_drop = gold_audit.errors(cur) if dev and cur else None
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
        fewshot = {v: fewshot_uids(prompts_dir / "classify" / v / "labels.md") for v in versions}
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
            conn, split, versions, fewshot, dump=dev, gold_drop=gold_drop
        )
    if dev or section == "judge":
        result["judge"] = classification.evaluate_judge(
            conn, split, [prompt_version] if section == "judge" else None, gold_drop
        )
    if dev or section == "crosscheck":
        jv = classification.judge_version(conn)
        a_versions = (
            [prompt_version]
            if section == "crosscheck"
            else list(result["classification"]["versions"])
        )
        result["crosscheck"] = (
            [
                classification.crosscheck(conn, split, a, jv, fewshot.get(a) if dev else None)
                for a in a_versions
            ]
            if jv
            else []
        )
    if dev or section == "auto":  # 자동 종결 오류율(평가 설계 §5) — dev는 활성 분류 × 활성 판정기
        jv = classification.judge_version(conn)
        a = prompt_version if section == "auto" else active_versions(conn)[0]  # 앱과 같은 기본값
        if jv and a:
            result["autoclose"] = autoclose.evaluate(conn, split, a, jv)
    if dev and call_actions_path and call_actions_path.exists():
        # 후속 처리안(평가 설계 §5) — 정답은 LLM 라벨 + 사람 표본, 실행은 make followup-eval이 쌓은 줄. test 정답 라벨은 아직 없다
        acts = call_actions.load(call_actions_path)
        runs = followup.load(followup_runs) if followup_runs else []
        truth, split_calls = call_actions.truth(acts), call_actions.eval_calls(conn)
        versions = sorted({r["prompt_version"] for r in runs if r.get("prompt_version")})
        result["followup"] = {
            "prompt_version": followup.followup_version(),
            "score": followup.score(conn, runs, truth, split_calls),
            "by_version": {  # 버전 비교 — 같은 통화·같은 정답으로
                v: followup.score(conn, runs, truth, split_calls, v) for v in versions
            },
            "labels": len(acts),
            # 다시 라벨하다 실패해 옛 프롬프트 버전으로 남은 줄 — 0이 아니면 make action-label을 다시
            "stale_labels": sum(
                r.get("prompt_version") != call_actions.PROMPT_VERSION for r in acts
            ),
            "agreement": call_actions.agreement(acts),
        }
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
        r["warnings"] = getattr(app, "warnings", list)()
        r["chat_skipped"] = not has_chat and not retrieval_only
        if not dev and r["warnings"]:  # 이력에 쓰기 전에 — test 1회 기회를 소진하지 않게
            raise EvalRefused(f"앱 경고가 있어 test를 인정하지 않는다: {'; '.join(r['warnings'])}")
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
        out += _render_classification(
            r["classification"], (r.get("gold_audit") or {}).get("judge_version")
        )
    audit_jv = (r.get("gold_audit") or {}).get("judge_version")
    if r["judge"] is not None:
        out += _render_judge(r["judge"], audit_jv)
        out += _render_gold_audit(r["gold_audit"])
    if r["crosscheck"] is not None:
        out += _render_crosscheck(r["crosscheck"])
    if r.get("autoclose") is not None:
        out += _render_autoclose(r["autoclose"])
    if r.get("followup") is not None:
        out += _render_followup(r["followup"])
    if r["rag"] is not None or app_missing:
        out += _render_rag(r["rag"])
    return "\n".join(out) + "\n"


def _render_classification(c: dict, audit_jv: str | None = None) -> list[str]:
    out = ["## ① 분류", ""]
    if not c["versions"]:
        out += ["분류 기록 없음", ""]
    for v, x in c["versions"].items():
        g = x["model_vs_gold"]
        out += [
            f"### {v} ({', '.join(x['model_actual'])})",
            "",
            f"목적 Strict — 모델 vs 골드 {_kn(g['purpose'])} · 가중 {_pct(g['purpose']['weighted'])} · 단순 평균 {_pct(g['purpose']['macro'])}",
            *_audited_line(x.get("model_vs_gold_audited"), audit_jv),
            "카테고리별 n(모델 vs 골드) — "
            + " · ".join(f"{c} {_kn(d)}" for c, d in g["purpose"]["by_category"].items()),
            "",
            "| 항목 | 모델 vs 골드 |",
            "|---|---|",
        ]
        out += [f"| {i} | {_kn(g['items'][i])} |" for i in classification.ITEMS]
        e = x["excluded"]
        out += [
            "",
            f"제외 — 폴백 {e['fallback']} · 실패 {e['failed']} · few-shot {e['fewshot']}",
            "",
        ]
    for cmp in c["compare"]:
        out += [
            f"### 버전 비교 {cmp['a']} → {cmp['b']} — 모델 vs 골드 (교집합 {cmp['n']}건)",
            "",
            "| 항목 | 전 | 후 | p |",
            "|---|---|---|---|",
        ]
        out += [
            f"| {i} | {fraction(d['a'], cmp['n'])} | {fraction(d['b'], cmp['n'])} | {d['p']:.3f} |"
            for i, d in cmp["items"].items()
        ]
        out.append("")
    for name, title in (("llm", "LLM 쌍 비교"), ("human", "사람 투표")):
        for b in c["blind"][name]:
            a, z = b["sides"]
            total = a["wins"] + z["wins"] + b["tie"] + b["both_wrong"]
            who = f"{title} {b['judge']}" if "judge" in b else title
            out += [
                f"### 블라인드({who}) {a['version']}({a['model']}) vs {z['version']}({z['model']}) — {total}표",
                "",
                f"승 {a['version']} {a['wins']} · {z['version']} {z['wins']} · 비슷 {b['tie']} · 둘 다 틀림 {b['both_wrong']}"
                f" / 위치 왼쪽 {b['left']} · 오른쪽 {b['right']}",
                "",
            ]
    for ag in c["blind"]["agreement"]:
        out += [
            f"블라인드 LLM 쌍 비교({ag['judge']}) vs 사람 일치 {fraction(ag['k'], ag['n'])}",
            "",
        ]
    if c.get("dump"):
        out += [
            "### 틀린 항목 (dev 판정 풀 — 골드 기준)",
            "",
            "| 통화 | 버전 | 항목 | 모델 | 골드 | 전사 |",
            "|---|---|---|---|---|---|",
        ]
        out += [
            f"| {d['call_uid']} | {d['version']} | {d['item']} | {_cell(d['model'])} | {_cell(d['gold'])} |"
            f" {_cell((d['transcript'] or '')[:60])}… |"
            for d in c["dump"]
        ]
        out.append("")
    return out


def _render_judge(j: dict, audit_jv: str | None = None) -> list[str]:
    out = ["## ② 판정기 vs 골드", ""]
    if not j:
        out += ["판정기 결과 없음", ""]
    for v, x in j.items():
        g = x["judge_vs_gold"]
        out += [
            f"### {v} ({', '.join(x['model_actual'])}) — 목적 {_kn(g['purpose'])} · 가중 {_pct(g['purpose']['weighted'])}",
            *_audited_line(x.get("judge_vs_gold_audited"), audit_jv),
            "",
            " · ".join(f"{i} {_kn(g['items'][i])}" for i in classification.ITEMS),
            "",
        ]
    return out


def _audited_line(g: dict | None, audit_jv: str | None) -> list[str]:
    """골드 오류 추정을 뺀 수치(dev만) — 원래 골드가 주 수치(평가 설계 §2.3). 어느 판정기 버전의 점검인지 함께"""
    if g is None:
        return []
    return [
        f"골드 오류 추정 제외(중재 기준·참고, 점검 판정기 {audit_jv}) — 목적 {_kn(g['purpose'])} · 가중 {_pct(g['purpose']['weighted'])}"
    ]


def _render_gold_audit(a: dict | None) -> list[str]:
    out = ["## 골드 점검", ""]
    if a is None:
        return out + ["골드 점검 전", ""]
    if "error" in a:
        return out + [f"골드 점검 — {a['error']}", ""]
    s = a["summary"]
    out += [
        f"판정기 {a['judge_version']} · 대상 {s['calls']}통화"
        + (f" · 낡은 기록(녹취록·값이 바뀜) {a['stale']}" if a["stale"] else "")
        + (f" · 중재 안 된 대상 {a['missing']}" if a["missing"] else ""),
        "",
        "| 항목 | 중재: 골드 쪽 | 판정기 쪽 | 둘 다 아님 | 중재 vs 사람 |",
        "|---|---|---|---|---|",
    ]
    for i in classification.ITEMS:
        if i not in s["arbiter"]:
            continue
        c, ag = s["arbiter"][i], s["agreement"].get(i)
        out.append(
            f"| {i} | {c['gold']} | {c['judge']} | {c['neither']} | {_kn(ag) if ag else '—'} |"
        )
    t, h = s["agreement_total"], s["human_errors"]
    names = {"gold": "골드 쪽", "judge": "판정기 쪽", "neither": "둘 다 아님"}
    out += [
        "",
        "중재 선택별 사람 일치 — "
        + (
            " · ".join(
                f"{names[k]} {_kn(s['by_arbiter'][k])}" for k in names if k in s["by_arbiter"]
            )
            or "—"
        ),
        f"골드 오류 추정(중재) — 판정기 쪽 {sum(c['judge'] for c in s['arbiter'].values())}"
        f" · 둘 다 아님 {sum(c['neither'] for c in s['arbiter'].values())}",
        f"중재 vs 사람 전체 {_kn(t) if t['n'] else '—'} · 사람 기준 오류 추정(표본, 참고) 판정기 쪽 {h['judge']}"
        f" · 둘 다 아님 {h['neither']} · 사람 미판정 {s['human_pending']}통화",
        "",
    ]
    return out


def _render_autoclose(a: dict) -> list[str]:
    e, ci = a["errors"], a["errors"]["ci"]
    names = {"has_todo": "할 일", "emergency": "응급", "complaint": "불만", "urgent": "급함"}
    return [
        f"## ① 자동 종결 — {a['pair']}",
        "",
        f"대상 {a['n']}통화 · 조건을 넘은 통화 {a['closed']} ({_pct(a['rate'])})",
        f"오류(골드가 할 일·응급·불만·급함) {_kn(e)}"
        + (f" · 95% 구간 {_pct(ci[0])}~{_pct(ci[1])}" if ci else ""),
        "항목별 — " + " · ".join(f"{names[i]} {k}" for i, k in a["by_item"].items()),
        "",
    ]


def _render_followup(f: dict) -> list[str]:
    ag = f["agreement"]
    out = [
        "## ① 후속 처리",
        "",
        f"처리 정답 {f['labels']}통화(Opus 라벨 + 사람 표본) · 라벨 vs 사람 {_kn(ag) if ag['n'] else '—'}"
        + (
            f" · 옛 버전 라벨 {f['stale_labels']}통화(make action-label 다시)"
            if f.get("stale_labels")
            else ""
        ),
        "",
        f"후속 에이전트 {f['prompt_version']}",
        "",
        "| 구간 | 통화 | 에이전트 | 규칙 기준선 | 실패(이유) | 실행 전 | 비용 | 분류기 놓침 | 분류기만 할 일 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for sp, x in f["score"].items():
        why = ", ".join(f"{k} {n}" for k, n in x["failed_by_reason"].items())
        out.append(
            f"| {sp} | {x['n']} | {_kn(x['agent'])} | {_kn(x['baseline'])} | {x['failed']}"
            + (f"({why})" if why else "")
            + f" | {x['not_run']} | ${x['cost_usd']} | {x['missed_by_classifier']}"
            f" | {x['extra_by_classifier']} |"
        )
    out.append("")
    if len(f.get("by_version", {})) > 1:  # 버전 비교 — 같은 통화·같은 정답
        out.append(
            "버전 비교 — "
            + " / ".join(
                f"{v}: " + ", ".join(f"{sp} {_kn(x['agent'])}" for sp, x in sc.items())
                for v, sc in f["by_version"].items()
            )
        )
        out.append("")
    for sp, x in f["score"].items():
        rows = [
            f"{a} 에이전트 {_kn(x['agent']['by_action'][a])} · 기준선 {_kn(x['baseline']['by_action'][a])}"
            for a in x["agent"]["by_action"]
        ]
        if rows:
            out += [f"{sp} 종류별 — " + " / ".join(rows), ""]
    return out


def _render_crosscheck(xs: list[dict]) -> list[str]:
    out = ["## ① 교차검증 (모델 A × 판정기)", ""]
    if not xs:
        out += ["판정기 결과 없음 또는 판정기 버전을 못 정함", ""]
    for x in xs:
        if "skipped" in x:
            out += [f"### {x['a']} × {x['b']} — 내지 않음({x['skipped']})", ""]
            continue

        def rate(d):
            return f"{_kn(d)} · 가중 {_pct(d['weighted'])}"

        lift = "—" if x["lift"] is None else f"{x['lift']:.2f}배"
        out += [
            f"### {x['a']} × {x['b']} — {x['n']}통화",
            "",
            f"엇갈림 {rate(x['disagree'])} (핵심 {_kn(x['core'])}) · 일치 오류 {rate(x['agree_error'])}"
            f" · 오류 포착 {rate(x['catch'])} · 향상 배수 {lift}",
            "",
        ]
    return out


def _render_rag(r: dict | None) -> list[str]:
    out = ["## ③ RAG", ""]
    if r is None:
        return out + ["앱 없음 — 건너뜀(앱 착수 후 backend가 rag.RagApp의 search·chat을 구현)", ""]
    for w in r.get("warnings", []):
        out += [f"**경고**: {w}", ""]
    if r.get("chat_skipped"):
        out += ["챗 건너뜀 — 앱에 챗이 아직 없다", ""]
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
    parser.add_argument(
        "--prompt-version",
        help="분류 버전 하나만(판정 묶음은 판정기 버전). 없으면 분류가 있는 버전 전부",
    )
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
            gold_audit_path=gold_audit.AUDIT_PATH,
            followup_runs=followup.OUT,
            call_actions_path=call_actions.ACTIONS_PATH,
        )
    except AlreadyRun as e:
        print(e)
        return 2
    except (EvalRefused, ValueError) as e:  # 앱이 조건·색인을 거절 — 이력에 남지 않는다
        print(e)
        return 1
    print(f"리포트: {out['report_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
