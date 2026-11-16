"""eval.py — 평가 실행. 정의는 docs/치과 AX - 평가 설계.md §7.

  uv run python -m evaluation.eval --split dev [--retrieval-only] [--prompt-version v2] [--condition reranker=off]
  uv run python -m evaluation.eval --split test --section classification|judge|crosscheck|auto|retrieval|chat|followup [--prompt-version v2] [--condition …] [--force]
  → reports/eval_<split>_<condition>_<시각>.md + 같은 이름 .json(make gate가 읽음) + reports/eval_runs.jsonl 1줄

- DB는 app.sqlite(앱 결과)에 source.sqlite(생성 데이터)·truth.sqlite(정답)를 붙여 읽는다(앱 설계 §7).
- 분류(①)는 앱이 쓴 분류·판정·투표 테이블을 읽기만 한다. RAG(③)는 앱의 search·chat을 같은 프로세스에서 호출한다(rag.RagApp,
  backend/eval_hooks.py의 make_app(app_db, source_db)) — 앱이 없으면 RAG 절은 건너뛰고, 앱 경고(rag 색인·리랭커)가 있으면 test를 거절한다. 평가 때 게이트웨이 파일 캐시·is_eval 기록은 앱 쪽 훅의 일
- test는 묶음마다 1회: 한 번에 묶음 하나(분류·버전 / 판정·판정기 버전 / 교차검증·자동 종결·분류 버전 × 판정기 버전 / 검색·조건 / 챗·조건 /
  후속 처리·후속 프롬프트 버전 × 활성 분류 버전),
  이력에 같은 묶음이 있으면 AlreadyRun — 다시는 --force.
  test의 --condition은 기본 조건을 다시 돌리지 않고 이력의 기본 조건 test 결과(문항별 통과 여부)와 비교한다
- 건별 목록(틀린 통화, 구제/손실·실패 문항)은 dev에서만
"""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import yaml

from backend import prep as backend_prep
from backend.calls import active_versions
from evaluation import (
    EvalRefused,
    autoclose,
    call_actions,
    claim_appeal,
    claim_check,
    claim_evidence,
    claim_omission,
    claim_qa,
    classification,
    expense,
    followup,
    gold_audit,
    prep,
    rag,
    recon,
    report,
)
from evaluation.metrics import fraction
from generator.paths import ROOT, output_paths
from shared.clinic import load_clinic

REPORTS = ROOT / "reports"
PROMPTS = ROOT / "prompts"
RUNS = "eval_runs.jsonl"
# 챗은 app.sqlite 사본에서 돈다 — 실행 기록·규칙 누계에 평가 표시가 없어 운영에 섞이지 않게, test 문항이 data/에 남지 않게
CHAT_COPY = ROOT / "evaluation" / "runs" / "rag.sqlite"
SECTIONS = (
    "classification",
    "judge",
    "crosscheck",
    "auto",
    "retrieval",
    "chat",
    "followup",
)  # test 묶음 — 분류·판정·교차검증·자동 종결·후속 처리는 버전, 검색·챗은 조건 단위로 1회
VERSIONED = ("classification", "judge", "crosscheck", "auto")  # --prompt-version을 받는 묶음
# 이력 키가 버전인 묶음 — 후속 처리는 지금 후속 프롬프트 버전 × 활성 분류 버전을 코드가 정한다(평가 설계 §7)
VERSION_KEYED = (*VERSIONED, "followup")


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


def _followup_test_ready(conn, runs_path: Path | None, acts_path: Path | None) -> str:
    """test 후속 처리 묶음을 돌릴 수 있는지 — 모든 모수 통화에 지금 버전 처리 정답 라벨과 지금 후속 버전·활성 분류 행의
    실행이 있어야 한다(빠지면 오답·실행 전으로 세어 1회 기회를 소진하므로 이력을 쓰기 전에 거절, 평가 설계 §5·§7).
    이력 키 = 후속 프롬프트 버전 × 활성 분류 버전"""
    uids = call_actions.eval_calls(conn, ("test",))["test"]
    if not uids:
        raise EvalRefused(
            "test 후속 처리 모수 통화가 없음 — 활성 분류·골드 모두 할 일인 test 판정 풀 통화"
        )
    acts = {
        r["call_uid"]: r
        for r in (call_actions.load(acts_path) if acts_path and acts_path.exists() else [])
    }
    unlabeled = sum(
        u not in acts or acts[u].get("prompt_version") != call_actions.PROMPT_VERSION for u in uids
    )
    if unlabeled:
        raise EvalRefused(
            f"test 처리 정답 라벨이 없거나 옛 버전인 통화 {unlabeled}/{len(uids)} — make action-label 먼저"
        )
    runs = followup.load(runs_path) if runs_path else []
    truth = call_actions.truth(list(acts.values()))
    not_run = followup.score(conn, runs, truth, {"test": uids})["test"]["not_run"]
    if not_run:
        raise EvalRefused(
            f"test 후속 처리 실행 전 통화 {not_run}/{len(uids)} — python -m evaluation.followup run --split test 먼저"
        )
    return f"{followup.followup_version()}×{active_versions(conn)[0]}"


def _key(section: str, condition: str, prompt_version: str) -> str:
    return prompt_version if section in VERSION_KEYED else condition


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
                "test는 --section 하나(classification·judge·crosscheck·auto·retrieval·chat·followup)만 돌린다 (평가 설계 §7)"
            )
        if section in VERSIONED and not prompt_version:
            raise EvalRefused(f"test {section}는 --prompt-version 하나")
        mismatch = (
            "--retrieval-only"
            if retrieval_only and section != "retrieval"
            else "--condition"
            if condition and section in VERSION_KEYED
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
        if section == "followup":
            pv = _followup_test_ready(conn, followup_runs, call_actions_path)
        if section in rag.PARTS and app is None:
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
        if section in rag.PARTS
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
        "prep": None,
        "report": None,
        "claim_check": None,
        "claim_omission": None,
        "claim_basis": None,
        "claim_appeal": None,
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
    judges = classification.judge_versions(conn)
    # 판정기 학습 통화 — 판정기끼리 같은 모수가 되게 모든 쌍에서 뺀다(dev만, Qwen 판정기 설계 '비교 규칙')
    trained = classification.judge_skip(conn, judges, prompts_dir) if dev else set()
    result["judge_trained_skip"] = len(trained)
    if dev or section == "judge":
        result["judge"] = classification.evaluate_judge(
            conn, split, [prompt_version] if section == "judge" else None, gold_drop, trained
        )
    if dev or section == "crosscheck":
        jv = classification.judge_version(conn)
        a_versions = (
            [prompt_version]
            if section == "crosscheck"
            else list(result["classification"]["versions"])
        )
        b_versions = judges if dev else [jv]  # dev는 판정기 버전 전부 나란히, test는 묶음 하나
        result["crosscheck"] = (
            [
                classification.crosscheck(
                    conn, split, a, b, fewshot.get(a) if dev else None, trained
                )
                for a in a_versions
                for b in b_versions
            ]
            if jv
            else []
        )
    if dev or section == "auto":  # 자동 종결 오류율(평가 설계 §5) — dev는 활성 분류 × 활성 판정기
        jv = classification.judge_version(conn)
        a = prompt_version if section == "auto" else active_versions(conn)[0]  # 앱과 같은 기본값
        if jv and a:
            result["autoclose"] = autoclose.evaluate(conn, split, a, jv)
            if dev:  # 판정기 비교 — 같은 분류 버전으로(검증 구간이라 학습 통화와 겹치지 않는다)
                result["autoclose_by_judge"] = [
                    autoclose.evaluate(conn, split, a, b) for b in judges
                ]
    if (dev or section == "followup") and call_actions_path and call_actions_path.exists():
        # 후속 처리안(평가 설계 §5) — 정답은 LLM 라벨 + 사람 표본, 실행은 make followup-eval(test는
        # python -m evaluation.followup run --split test)이 쌓은 줄. dev는 dev·검증 구간, test는 test 판정 풀만
        spans = call_actions.DEV_PARTS if dev else ("test",)
        pop = {r["call_uid"] for k in spans for r in call_actions.split_population(conn)[k]}
        acts = [r for r in call_actions.load(call_actions_path) if r["call_uid"] in pop]
        runs = followup.load(followup_runs) if followup_runs else []
        truth, split_calls = call_actions.truth(acts), call_actions.eval_calls(conn, spans)
        versions = sorted({r["prompt_version"] for r in runs if r.get("prompt_version")})
        result["followup"] = {
            "prompt_version": followup.followup_version(),
            "score": followup.score(conn, runs, truth, split_calls),
            "by_version": {  # 버전 비교 — 같은 통화·같은 정답으로(test는 지금 버전 하나)
                v: followup.score(conn, runs, truth, split_calls, v) for v in versions if dev
            },
            "labels": len(acts),
            # 다시 라벨하다 실패해 옛 프롬프트 버전으로 남은 줄 — 0이 아니면 make action-label을 다시
            "stale_labels": sum(
                r.get("prompt_version") != call_actions.PROMPT_VERSION for r in acts
            ),
            "agreement": call_actions.agreement(acts),
        }
    has_prep = conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE name = 'ax_prep_sheets'"
    ).fetchone()
    if dev and has_prep:
        # ④ 준비표(평가 설계 §4) — 정답 없는 코드 채점, 범위는 날짜 탭(기준일 포함 최근 8일)
        days = backend_prep.tab_days(load_clinic().today)
        result["prep"] = {"days": [days[0].isoformat(), days[-1].isoformat()]} | prep.score(
            conn, days
        )
    has_report = conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE name = 'ax_reports'"
    ).fetchone()
    if dev and has_report:
        # ⑥ 경영 리포트 해석(평가 설계 §5) — 정답 없는 코드 채점, 범위는 화면 기간 전부
        result["report"] = report.score(conn, load_clinic().today)
    has_checks = conn.execute(
        "SELECT 1 FROM main.sqlite_master WHERE name = 'ax_claim_checks'"
    ).fetchone()
    if dev and has_checks and conn.execute("SELECT 1 FROM ax_claim_checks LIMIT 1").fetchone():
        # ⑤-2 AI 점검(평가 설계 §5 ⑤) — dev만, test는 python -m evaluation.claim_check run --split test(사람)
        result["claim_check"] = claim_check.score(conn, load_clinic().today, "dev")
    if dev:
        # ⑤-2b 추가 처치 누락(설계 §2b) — 과금 없음(합성 dev 기록·저장된 2b 결과로). 실데이터 누락 줄은 dev에서 빼고
        # 정상 줄의 잘못 잡은 수만, test는 python -m evaluation.claim_omission synth·real --split test(사람)
        part = _claim_omission(conn)
        if part:
            result["claim_omission"] = part
    if dev:
        # ⑤-3 근거 찾기(설계 §3) — 청구 기준 질문 Hit@5·MRR(질문 임베딩·리랭커 호출)과 서류 사유 차트 문장 실재율(과금 없음).
        # test는 python -m evaluation.claim_qa score --split test(사람)
        part = _claim_basis(conn, app, condition)
        if part:
            result["claim_basis"] = part
    if dev and claim_appeal.RUNS.exists() and _has_table_rows(conn, "truth_claim_errors", "truth"):
        # ⑤-4 재심 사유서(설계 §4) — 과금 없음(기록은 make claim-appeal-dev). test는 python -m evaluation.claim_appeal run --split test(사람)
        result["claim_appeal"] = claim_appeal.score(
            claim_appeal.targets(conn, "dev"), claim_appeal.read_runs(claim_appeal.RUNS)
        )
    if dev and _has_recon(conn):
        # ⑧-1 규칙 기준선 vs 조사 에이전트(평가 설계 §5) — 과금 없음(실행은 make recon). test는 python -m evaluation.recon score --split test(사람)
        result["recon"] = recon.score(conn, split, load_clinic().today)
    if dev and _has_expense(conn):
        # ⑧-2 규칙 기준선 vs 증빙 탐색 에이전트(평가 설계 §5) — 과금 없음(실행은 make expense-ai). test는 python -m evaluation.expense score --split test(사람)
        result["expense"] = expense.score_agent(conn, split)
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
    if r.get("judge_trained_skip"):  # 판정기 채점·교차검증 둘 다에 걸린다
        out += [
            f"판정기 학습 통화 제외 {r['judge_trained_skip']}통화 — 판정기 채점·교차검증에서 모든 판정기 쪽으로",
            "",
        ]
    if r["judge"] is not None:
        out += _render_judge(r["judge"], audit_jv)
        out += _render_gold_audit(r["gold_audit"])
    if r["crosscheck"] is not None:
        out += _render_crosscheck(r["crosscheck"])
    if r.get("autoclose") is not None:
        out += _render_autoclose(r["autoclose"])
    for a in r.get("autoclose_by_judge") or []:
        if a["pair"] != r["autoclose"]["pair"]:
            out += _render_autoclose(a)
    if r.get("followup") is not None:
        out += _render_followup(r["followup"])
    if r.get("prep") is not None:
        out += _render_prep(r["prep"])
    if r.get("report") is not None:
        out += _render_report(r["report"])
    if r.get("claim_check") is not None:
        out += _render_claim_check(r["claim_check"])
    if r.get("claim_omission") is not None:
        out += _render_claim_omission(r["claim_omission"])
    if r.get("claim_basis") is not None:
        out += _render_claim_basis(r["claim_basis"])
    if r.get("claim_appeal") is not None:
        out += _render_claim_appeal(r["claim_appeal"])
    if r.get("recon") is not None:
        out += _render_recon(r["recon"])
    if r.get("expense") is not None:
        out += _render_expense(r["expense"])
    if r["rag"] is not None or app_missing:
        out += _render_rag(r["rag"])
    return "\n".join(out) + "\n"


def _render_classification(c: dict, audit_jv: str | None = None) -> list[str]:
    out = [
        "## ① 분류",
        "",
        "급함은 urgent 여부만 센다(골드 normal/low가 녹취와 무관한 추첨 — 평가 설계 §2.2). 이 정의 전 리포트와 급함 숫자를 직접 비교하지 않는다",
        "",
    ]
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
    b = c.get("baselines")
    if b and b["n"]:
        vs = list(b["versions"])
        out += [
            f"### 강한 기준선과 나란히 — silver 재료 밖 골드 {b['n']}통화(n-gram은 재료 {b['trained_on']}통화로 학습)",
            "",
            "모든 열이 같은 통화(모든 버전이 쓸 수 있는 통화)를 센다. 급함은 urgent 여부만(평가 설계 §2.2)",
            "",
            "| 항목 | 규칙(학습 없음) | 글자 n-gram | " + " | ".join(vs) + " |",
            "|---|---|---|" + "---|" * len(vs),
        ]
        out += [
            f"| {i} | {_kn(b['rule'][i]) if i in b['rule'] else '—'} | {_kn(b['ngram'][i])} | "
            + " | ".join(_kn(b["versions"][v][i]) for v in vs)
            + " |"
            for i in classification.ITEMS
        ]
        out.append("")
    if c.get("hard") and c["hard"]["n"]:
        out += _render_hard(c["hard"])
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


def _render_hard(h: dict) -> list[str]:
    """어려운 통화(hc-) 표 — 칸마다 'n-gram → 버전' 맞힌 수, 줄은 전체·종류별·급함 뒤집기 절반별(어려운 통화 설계 §6)"""
    vs = list(h["versions"])
    out = [
        f"### 어려운 통화 hard {h['n']}통화 — 규칙·글자 n-gram(재료 {h['trained_on']}통화)·분류기",
        "",
        "종류마다 같은 건수로 고르게 나눈 셋(현실 비율 아님). 칸 = n-gram → "
        + " / ".join(vs)
        + ". 급함은 urgent 여부만",
        "",
        "| 묶음 | n | 할 일 규칙 | " + " | ".join(classification.ITEMS) + " |",
        "|---|---|---|" + "---|" * len(classification.ITEMS),
    ]
    rows = [("전체", h)] + list(h.get("by_type", {}).items()) + list(h.get("by_half", {}).items())
    for name, g in rows:
        cells = [
            " → ".join([_kn(g["ngram"][i])] + [_kn(g["versions"][v][i]) for v in vs])
            for i in classification.ITEMS
        ]
        out.append(
            f"| {name} | {g['n']} | {_kn(g['rule']['has_todo'])} | " + " | ".join(cells) + " |"
        )
    return out + [""]


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


def _render_report(p: dict) -> list[str]:
    return [
        "## ⑥ 경영 리포트 해석",
        "",
        f"화면 기간 {p['periods']} · 저장 {p['saved']} · 후검증 통과 {p['valid']}"
        f" (빈 해석 {p['empty']}, 숫자가 바뀐 해석 {p['stale']}) · 점 {p['points']}개",
        "",
        "| 지표 | 값 |",
        "|---|---|",
        f"| 본문 숫자 metrics 존재(점 단위) | {_kn(p['numbers_ok'])} |",
        "",
    ]


def _render_prep(p: dict) -> list[str]:
    versions = ", ".join(f"{v} {n}행" for v, n in p["by_prompt_version"].items()) or "없음"
    return [
        "## ④ 준비표",
        "",
        f"{p['days'][0]} ~ {p['days'][1]} · 저장 행 {p['valid']['n']}({versions})"
        f" · 만들지 않은 예약 {p['not_generated']}",
        "",
        "| 지표 | 제안(LLM) | 템플릿 기준선 |",
        "|---|---|---|",
        *(
            f"| {name} | {_kn(p[m])} | {_kn(p['template'][m])} |"
            for m, name in (
                ("valid", "후검증 통과"),
                ("reflect", "주의사항 반영률(관련 주의사항이 있는 행)"),
                ("over_mention", "과잉 언급률(무관 주의사항을 근거로 씀)"),
                ("over_suggest", "과잉 제안률(칩이 다 꺼진 행의 suggest)"),
            )
        ),
        "",
        "템플릿 기준선 = 같은 행 사실만으로 켜진 칩 키를 모두 근거로 제안(LLM 없음) — 네 지표는 빠뜨림·지어냄만 재므로 둘이 같으면 제안의 쓸모는 이 지표로 가려지지 않는다",
        "",
    ]


def _render_claim_check(c: dict) -> list[str]:
    def pct(v):
        return "—" if v is None else f"{v:.1%}"

    q, a = c["requirement"], c["adjust"]
    ans = q["answers"]
    out = [
        "## ⑤-2 청구 AI 점검",
        "",
        f"요건 대상 {q['n']}줄 · 점검({c['prompt_version']}) {q['checked']} · 후검증 통과 {q['valid']}"
        f" · 요건 오류 {q['errors']}",
        f"답: 근거 있음 {ans['met']} · 근거 없음 {ans['missing']} · 판단 불가 {ans['na']}"
        f" · 후검증 실패 {ans['failed']}(판단 불가·실패는 음성)",
        "",
        "| 요건 오류 | AI 정밀도 | AI 재현율 | 서술 있는 줄 전부 정밀도 | 재현율 | 키워드 정밀도 | 재현율 |",
        "|---|---|---|---|---|---|---|",
    ]
    for label, part in (
        ("전체", q),
        ("확정 월", q["by_period"]["confirmed"]),
        ("제출·대기", q["by_period"]["filed"]),
    ):
        ai, base, kw = part["ai"], part["narrative_all"], part["keyword"]
        out.append(
            f"| {label} | {pct(ai['precision'])} | {pct(ai['recall'])}"
            f" | {pct(base['precision'])} | {pct(base['recall'])}"
            f" | {pct(kw['precision'])} | {pct(kw['recall'])} |"
        )
    out += [
        "",
        f"조정 건 판단 {a['n']}건 — AI {pct(a['ai']['accuracy'])}(답 {a['ai']['answered']})"
        f" · 모두 정당 {pct(a['all_justified'])} · 모두 다툴 만함 {pct(a['all_contestable'])}",
        "",
    ]
    return out


def _claim_omission(conn) -> dict | None:
    """합성 dev 기록이 있으면 합성 채점, 2b 결과가 저장돼 있으면 실데이터 dev — 둘 다 없으면 None"""
    synth = None
    if claim_omission.RUNS.exists():
        runs = claim_omission._read(claim_omission.RUNS)
        synth = claim_omission.score_synth(claim_omission.load_cases(), runs, "dev")
    has = (
        conn.execute(
            "SELECT 1 FROM main.sqlite_master WHERE name = 'ax_claim_omissions'"
        ).fetchone()
        and conn.execute("SELECT 1 FROM ax_claim_omissions LIMIT 1").fetchone()
    )
    real = claim_omission.score_real(conn, "dev") if has else None
    if synth is None and real is None:
        return None
    return {"prompt_version": claim_omission.co.PROMPT_VERSION, "synth": synth, "real": real}


def _render_claim_omission(c: dict) -> list[str]:
    def pct(v):
        return "—" if v is None else f"{v:.1%}"

    out = [f"## ⑤-2b 추가 처치 누락 ({c['prompt_version']})", ""]
    s = c["synth"]
    if s:
        out += [
            f"합성 사례 dev {s['n']}건 · 후검증 실패 {s['failed']} · 점검 없음 {s['missing']}(항목 단위)",
            "",
            "| | 정밀도 | 재현율 | 맞음 | 잘못 잡음 | 놓침 |",
            "|---|---|---|---|---|---|",
        ]
        for label, part in (("AI", s["ai"]), ("키워드", s["keyword"])):
            out.append(
                f"| {label} | {pct(part['precision'])} | {pct(part['recall'])}"
                f" | {part['tp']} | {part['fp']} | {part['fn']} |"
            )
        out += ["", "| 모양 | 건 | AI 맞음 | 키워드 맞음 |", "|---|---|---|---|"]
        for shape, b in s["by_shape"].items():
            out.append(f"| {shape} | {b['n']} | {b['ai_ok']} | {b['keyword_ok']} |")
        out.append("")
    r = c["real"]
    if r:
        out += [
            f"실데이터 dev 정상 줄 {r['lines']}줄(누락 정답 줄 제외, 점검 없음 {r['missing']}"
            f" · 후검증 실패 {r['failed']}) — 잘못 잡은 줄 AI {r['ai_fp_lines']}"
            f" · 키워드 {r['keyword_fp_lines']}",
            "",
        ]
    return out


def _has_table_rows(conn, table: str, schema: str = "main") -> bool:
    return bool(
        conn.execute(f"SELECT 1 FROM {schema}.sqlite_master WHERE name = ?", (table,)).fetchone()
        and conn.execute(f"SELECT 1 FROM {schema}.{table} LIMIT 1").fetchone()
    )


def _claim_basis(conn, app, condition) -> dict | None:
    """질문 정답(truth_claim_qa)과 검색 앱·색인이 있으면 Hit@5·MRR, 차트 문장이 저장돼 있으면 실재율 — 둘 다 없으면 None"""
    qa = None
    if (
        app is not None
        and callable(getattr(app, "claim_search", None))
        and _has_table_rows(conn, "truth_claim_qa", "truth")
        and _has_table_rows(conn, "ax_chunks")
    ):
        qa = claim_qa.score(conn, app, "dev", condition)
    evidence = (
        claim_evidence.score(conn, load_clinic().today)
        if _has_table_rows(conn, "ax_claim_evidence")
        else None
    )
    return None if qa is None and evidence is None else {"qa": qa, "evidence": evidence}


def _render_claim_basis(c: dict) -> list[str]:
    def pct(v):
        return "—" if v is None else f"{v:.1%}"

    out = ["## ⑤-3 근거 찾기", ""]
    if q := c["qa"]:
        out += [
            f"청구 기준 질문 dev {q['n']}문항 — Hit@5 {pct(q['hit5'])} · MRR "
            + ("—" if q["mrr"] is None else f"{q['mrr']:.2f}")
            + f"(절 단위, 후보 절 {q['candidates']}개뿐이라 쉬운 편)",
            "",
        ]
    if e := c["evidence"]:
        out += [
            f"서류 사유 차트 문장 — 대상 {e['n']}건 · 점검 {e['checked']} · 인용 실재 통과 {e['valid']}"
            f" · 인용 있음 {e['answered']} · 옮겨 적을 문장 없음 {e['empty']}",
            "",
        ]
    return out


def _render_claim_appeal(c: dict) -> list[str]:
    rate = "—" if c["unbacked_rate"] is None else f"{c['unbacked_rate']:.1%}"
    return [
        f"## ⑤-4 재심 사유서 초안 ({c['prompt_version']})",
        "",
        f"정답 다툴 만함 dev {c['n']}건 · 초안 {c['run']} — 후검증 통과 첫 답 {c['first_valid']}/{c['run']}"
        f" · 다시 부른 뒤 {c['final_valid']}/{c['run']}",
        f"첫 답 근거 없는 문장 {c['unbacked']}/{c['sentences']}({rate})",
        "",
    ]


def _has_recon(conn) -> bool:
    """⑧-1 채점을 넣을지 — 정답 DB에 불일치 정답이 있을 때(테스트용 작은 DB는 건너뜀)"""
    try:
        return bool(conn.execute("SELECT 1 FROM truth.truth_recon_mismatches LIMIT 1").fetchone())
    except sqlite3.OperationalError:
        return False


def _has_expense(conn) -> bool:
    """⑧-2 채점을 넣을지 — 정답 DB에 지출 정답이 있고 앱 DB에 실행 기록 표가 있을 때(테스트용 작은 DB는 건너뜀)"""
    try:
        return bool(
            conn.execute("SELECT 1 FROM truth.truth_expense_matches LIMIT 1").fetchone()
            and conn.execute(
                "SELECT 1 FROM main.sqlite_master WHERE name = 'ax_agent_runs'"
            ).fetchone()
        )
    except sqlite3.OperationalError:
        return False


_EXPENSE_GROUP = {"passed": "규칙 통과", "none": "증빙 못 찾음", "check": "과목 확인 필요"}
_CONFIDENCE = {"high": "높음", "mid": "중간", "low": "낮음"}


def _render_expense(r: dict) -> list[str]:
    pm, pa = r["pair_match"], r["pair_account"]
    out = [
        "## ⑧-2 매칭·과목 정확도 — 규칙 기준선 vs 증빙 탐색 에이전트",
        "",
        f"지출 정답 {r['n']}건 · 에이전트 프롬프트 {r['prompt_version']}",
        "",
        "| 묶음 | 규칙 매칭 | AI 매칭 | 규칙 과목 | AI 과목 |",
        "|---|---|---|---|---|",
        f"| 전체 | {_kn(r['rule']['match'])} | {_kn(r['agent']['match'])}"
        f" | {_kn(r['rule']['account'])} | {_kn(r['agent']['account'])} |",
    ]
    out += [
        f"| {_EXPENSE_GROUP[g]} | {_kn(x['rule_match'])} | {_kn(x['agent_match'])}"
        f" | {_kn(x['rule_account'])} | {_kn(x['agent_account'])} |"
        for g, x in r["by_group"].items()
    ]
    out += [
        "",
        f"짝 비교(매칭): 구제 {pm['rescued']} · 손실 {pm['lost']} · 둘 다 맞음 {pm['both']} · 둘 다 틀림 {pm['neither']}",
        f"짝 비교(과목): 구제 {pa['rescued']} · 손실 {pa['lost']} · 둘 다 맞음 {pa['both']} · 둘 다 틀림 {pa['neither']}",
        "확신도별 AI 매칭: "
        + " · ".join(f"{_CONFIDENCE[c]} {_kn(x['match'])}" for c, x in r["by_confidence"].items()),
        f"자동 확정 오류(조건을 넘은 줄 중 틀림): {_kn(r['auto'])}",
        "",
    ]
    if r["missing"]:
        out += [
            f"주의: 지금 버전 실행이 없는 정답 {r['missing']}건(오답으로 셈) — make expense-ai",
            "",
        ]
    if r["no_result"]:
        out += [f"결과 없이 끝난 정답 {r['no_result']}건(조사 상한 등, 오답으로 셈)", ""]
    if r["rows"]:
        out += ["규칙이나 AI가 틀린 줄(dev):", ""]
        out += [
            f"- {x['at']} {x['label']} {x['amount']:,}원 · {_EXPENSE_GROUP[x['group']]} · 매칭 {x['match']}"
            f" · 과목 {x['account']}(정답 {x['truth_account']}, AI {x['agent_account']})"
            for x in r["rows"]
        ]
        out.append("")
    return out


_RECON_GROUP = {"rule": "규칙으로 되는 원인", "ambiguous": "애매한 원인"}


def _render_recon(r: dict) -> list[str]:
    out = [
        "## ⑧-1 원인 정확도 — 규칙 기준선 vs 조사 에이전트",
        "",
        f"불일치 정답 {r['n']}건 · 탐지 {_kn(r['detected'])} · 에이전트 프롬프트 {r['prompt_version']}",
        "",
        "| 묶음 | 규칙(top-1) | 에이전트(top-1) |",
        "|---|---|---|",
        f"| 전체 | {_kn(r['rule'])} | {_kn(r['agent'])} |",
    ]
    out += [
        f"| {_RECON_GROUP.get(g, g)} | {_kn(kn)} | {_kn(r['agent_by_group'][g])} |"
        for g, kn in r["by_group"].items()
    ]
    p = r["pair"]
    out += [
        "",
        f"짝 비교: 구제(규칙 틀림 → 에이전트 맞음) {p['rescued']} · 손실 {p['lost']}"
        f" · 둘 다 맞음 {p['both']} · 둘 다 틀림 {p['neither']}",
        f"기권(unknown) {_kn(r['abstain'])} · 조사 단계 수 평균 {r['steps_mean']}",
        "자동 마감 오류(조건을 넘은 건 중 틀림): "
        + " · ".join(f"{_RECON_GROUP.get(g, g)} {_kn(kn)}" for g, kn in r["auto"].items()),
        "",
    ]
    if r["missing"]:
        out += [
            f"주의: 지금 버전 조사 실행이 없는 정답 {r['missing']}건(오답으로 셈) — make recon",
            "",
        ]
    if r["steps_hist"]:
        out += [
            "단계 수 분포: " + " · ".join(f"{k}단계 {v}건" for k, v in r["steps_hist"].items()),
            "",
        ]
    if r["by_cause"] is not None:
        out += ["| 원인 | 규칙 맞음 |", "|---|---|"]
        out += [f"| {c} | {_kn(kn)} |" for c, kn in r["by_cause"].items()]
        out.append("")
    if r["wrong"]:
        out += ["규칙이 틀린 건(dev):", ""]
        out += [f"- {w['date']} 정답 {w['cause']} · 규칙 {w['rule_cause']}" for w in r["wrong"]]
        out.append("")
    if r["pair_rows"]:
        out += ["에이전트 짝 비교 건(dev, 둘 다 맞음 제외):", ""]
        out += [
            f"- {x['date']} 정답 {x['cause']} · 규칙 {x['rule_cause']} · 에이전트 {x['agent_cause']}"
            f" ({x['pair']})"
            for x in r["pair_rows"]
        ]
        out.append("")
    return out


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
        if "chart_citation" in c:  # 보고 항목(관문 조건 아님) — 설계 2026-11-09 챗 "새 지표 셋"
            out += [
                f"보고 — 차트형 인용 {_kn(c['chart_citation'])} · 공지형 현재 값 {_kn(c['notice_value'])}"
                f" · 답 내용(사례·매뉴얼) {_kn(c['answer_content'])}"
                f" · 도구 호출 {c['tool_calls']}회 · ${c['cost_usd']:.4f}"
                + (f" · 폴백 제외 {c['excluded_fallback']}문항" if c["excluded_fallback"] else ""),
                "",
            ]
        if c.get("baseline"):
            b = c["baseline"]
            out += [
                f"단순 RAG 기준선 — 인용 정확도 {_kn(b['citation'])} · 환각 억제 {_kn(b['refusal'])}"
                f" · 호출 {b['calls']}회 · ${b['cost_usd']:.4f}",
            ]
            out += [
                f"- {k}: 챗이 구제 {', '.join(b['compare']['rescued'][k]) or '—'}"
                f" / 챗이 놓침 {', '.join(b['compare']['lost'][k]) or '—'}"
                for k in b["compare"]["rescued"]
                if b["compare"]["rescued"][k] or b["compare"]["lost"][k]
            ]
            out.append("")
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


def copy_app_db(db: Path, copy: Path) -> Path:
    """app.sqlite → 사본(SQLite 백업 — WAL 내용까지). 매번 새로 떠 지난 평가의 기록이 쌓이지 않게"""
    copy.parent.mkdir(parents=True, exist_ok=True)
    copy.unlink(missing_ok=True)
    src, dst = sqlite3.connect(db), sqlite3.connect(copy)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()
    return copy


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
    # 챗이 도는 실행만 사본을 뜬다(dev 기본, test --section chat) — 370MB를 매번 뜨지 않게
    chat_runs = not args.retrieval_only and (args.split == "dev" or args.section == "chat")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS source", (f"file:{source}?mode=ro",))
    conn.execute("ATTACH DATABASE ? AS truth", (f"file:{truth}?mode=ro",))
    try:
        out = run(
            conn,
            reports_dir=args.reports_dir,
            app=load_app(copy_app_db(db, CHAT_COPY) if chat_runs else db, source),
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
