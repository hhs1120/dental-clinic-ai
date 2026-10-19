"""정답 격리: 앱 코드는 ground_truth·generator를 참조하지 않는다 (평가 설계 §1)."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("ground_truth", "generator")


def test_backend_does_not_reference_ground_truth():
    offenders = [
        f"{p.relative_to(ROOT)}: {word}"
        for p in (ROOT / "backend").rglob("*.py")
        for word in FORBIDDEN
        if word in p.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders
