"""코어 합격 기준 `make gate`의 실데이터 확인 — ① 판정 풀이 활성 분류 버전으로 전부 `ok`, dev 평가를 새로 돌려
③ 도구 선택·Hit@5 하한과 ① 후속 처리안이 규칙 기준선 이상인지 본다. pytest·`make e2e`·`next build`는 Makefile이 돈다.
원본: 기능 정의 §4(①·②·③ 완료 기준)·§6, 값은 evaluation/metrics.py `CORE_*`."""

import argparse
import json
import sqlite3
from pathlib import Path

from backend.calls import active_versions
from evaluation import eval as eval_
from evaluation.metrics import CORE_HIT5_MIN, CORE_TOOL_MIN, fraction


def pool_not_ok(app: Path, source: Path) -> int:
    """판정 풀 통화 중 활성 분류 버전의 `ok` 행이 없는 수(기능 정의 §4 ①)"""
    conn = sqlite3.connect(f"file:{app}?mode=ro", uri=True)
    try:
        conn.execute("ATTACH DATABASE ? AS source", (f"file:{source}?mode=ro",))
        active = active_versions(conn)[0]
        return conn.execute(
            "SELECT count(*) FROM source.ax_calls c WHERE c.in_eval_pool = 1 AND NOT EXISTS ("
            " SELECT 1 FROM ax_call_classifications k WHERE k.call_uid = c.call_uid"
            " AND k.model_alias = 'classify' AND k.prompt_version = ? AND k.status = 'ok')",
            (active,),
        ).fetchone()[0]
    finally:
        conn.close()


def _at_least(s: dict, share: float) -> bool:
    return s["n"] > 0 and s["k"] >= share * s["n"]


def check_report(result: dict) -> dict[str, tuple[bool, str]]:
    """조건 이름 → (통과, 설명) — dev 리포트(eval.py)에서"""
    rag, fu = result["rag"], result["followup"]["score"]["dev"]
    tool = {"k": 0, "n": 0} if rag["chat_skipped"] else rag["chat"]["tool"]
    hit5 = rag["retrieval"]["hit5"]
    return {
        "warnings": (not rag["warnings"], ", ".join(rag["warnings"]) or "앱 경고 없음"),
        "tool": (_at_least(tool, CORE_TOOL_MIN), f"③ 도구 선택 {fraction(tool['k'], tool['n'])}"),
        "hit5": (_at_least(hit5, CORE_HIT5_MIN), f"③ Hit@5 {fraction(hit5['k'], hit5['n'])}"),
        "followup": (
            fu["agent"]["k"] >= fu["baseline"]["k"],
            f"① 후속 처리안 {fu['agent']['k']}/{fu['n']} · 기준선 {fu['baseline']['k']}/{fu['n']}",
        ),
    }


def main(argv=None) -> int:
    paths = eval_.output_paths()
    p = argparse.ArgumentParser(prog="python -m evaluation.gate")
    p.add_argument("--app", type=Path, default=paths["app"])
    p.add_argument("--source", type=Path, default=paths["source"])
    p.add_argument("--reports-dir", type=Path, default=eval_.REPORTS)
    args = p.parse_args(argv)
    missing = pool_not_ok(args.app, args.source)
    if missing:  # 과금하는 dev 평가 전에 멈춘다
        print(f"불합격 — 판정 풀 {missing}건이 활성 분류 버전으로 ok가 아님(make classify)")
        return 1
    print("판정 풀 전부 ok")
    runs = args.reports_dir / eval_.RUNS
    before = len(runs.read_text().splitlines()) if runs.exists() else 0
    argv_eval = ["--split", "dev", "--db", str(args.app), "--source", str(args.source),
                 "--reports-dir", str(args.reports_dir)]  # fmt: skip
    if eval_.main(argv_eval) != 0:
        print("불합격 — dev 평가 실패")
        return 1
    lines = runs.read_text().splitlines()
    if len(lines) <= before:
        print("불합격 — dev 평가가 리포트를 남기지 않음")
        return 1
    path = (args.reports_dir / json.loads(lines[-1])["report_path"]).with_suffix(".json")
    checks = check_report(json.loads(path.read_text()))
    for name, (ok, text) in checks.items():
        print(f"{name:10} {'통과' if ok else '불통과'}  {text}")
    passed = all(ok for ok, _ in checks.values())
    print("합격" if passed else "불합격")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
