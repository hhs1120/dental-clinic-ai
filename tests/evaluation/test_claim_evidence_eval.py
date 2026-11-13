"""⑤-3 서류 사유 차트 문장 채점(evaluation/claim_evidence.py) — 인용 실재 후검증 통과율과 답 분포(정답이 없어 정확도는 재지
않는다, 설계 docs/superpowers/specs/2026-11-10-claim-review-design.md §3)."""

import json

from backend import claim_evidence as ce
from evaluation import claim_evidence as ev
from tests.backend.test_claim_evidence import DAY, claim, src  # noqa: F401 — 픽스처


def save(conn, cid, quotes, version=ce.PROMPT_VERSION):
    result = None if quotes is None else json.dumps({"quotes": quotes})
    with conn:
        conn.execute(
            "INSERT INTO ax_claim_evidence VALUES (?, '{}', 'h', ?, ?, ?, 'm', 'now')",
            (cid, result, int(result is not None), version),
        )


def test_evidence_score(src):  # noqa: F811
    a, b, c, d = (claim(src, i) for i in range(1, 5))
    claim(src, 5)  # 점검 안 함
    conn = src.app()
    save(conn, a, ["치주낭 5mm, 출혈 있음"])
    save(conn, b, [])
    save(conn, c, None)
    save(conn, d, ["옛 버전"], version="v0")
    assert ev.score(conn, DAY) == {"n": 5, "checked": 3, "valid": 2, "answered": 1, "empty": 1}
