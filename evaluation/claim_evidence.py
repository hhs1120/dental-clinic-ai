"""⑤-3 서류 사유 차트 문장 채점 (설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §3, 평가 설계 §5 ⑤-3).
서류 사유 조정·불능 전부(backend/claim_evidence.py targets "all") 중 지금 프롬프트 버전으로 점검한 줄의 인용 실재 후검증
통과 수와 답 분포(인용 있음 / 없음). 정답이 없어 정확도는 재지 않는다. 과금 없음(실행은 make claim-evidence SCOPE=all)."""

from datetime import date

from backend import claim_evidence as ce


def score(conn, today: date) -> dict:
    rows = ce.targets(conn, today, scope="all")
    got = ce.results(conn, [r["claim_id"] for r in rows])
    now = [g for g in got.values() if g["prompt_version"] == ce.PROMPT_VERSION]
    return {
        "n": len(rows),
        "checked": len(now),
        "valid": sum(g["valid"] for g in now),
        "answered": sum(bool(g["quotes"]) for g in now),
        "empty": sum(g["quotes"] == [] for g in now),
    }
