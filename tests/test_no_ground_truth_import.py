"""정답 격리: 앱 코드는 ground_truth·generator를 참조하지 않는다 (평가 설계 §1).
.py만이 아니라 설정·SQL·노트북 등 모든 텍스트 파일을 본다 — 경로·테이블 이름으로 정답에 닿는 것도 막는다."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# backend: 정답 DB·테이블·JSON, 생성기, 정답을 읽는 평가 코드(import하면 정답이 옮겨 온다)
BACKEND_FORBIDDEN = ("ground_truth", "generator", "evaluation", "truth.sqlite", "truth_")
# frontend: 정답 파일·테이블 (generator는 JS 라이브러리 이름에도 흔해 뺀다)
FRONTEND_FORBIDDEN = ("ground_truth", "truth.sqlite", "truth_", "call_labels", "doc_meta", "rag_qa")
SKIP_DIRS = {"node_modules", ".next", "dist", "build", "__pycache__", ".venv"}


def offenders(root: Path, words: tuple) -> list[str]:
    out = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]  # node_modules 등은 들어가지 않는다
        for name in files:
            p = Path(folder) / name
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:  # 이미지 등 바이너리
                continue
            out += [f"{p.relative_to(root)}: {w}" for w in words if w in text]
    return out


def test_backend_does_not_reference_ground_truth():
    assert not offenders(ROOT / "backend", BACKEND_FORBIDDEN)


def test_frontend_does_not_reference_ground_truth():
    assert not offenders(ROOT / "frontend", FRONTEND_FORBIDDEN)


def test_offenders_catch_non_python_files(tmp_path):
    (tmp_path / "settings.yaml").write_text("db: ../ground_truth/truth.sqlite\n", encoding="utf-8")
    (tmp_path / "query.sql").write_text("SELECT * FROM truth_call_labels\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "lib.js").write_text("ground_truth\n", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\xff\xfe")
    got = offenders(tmp_path, BACKEND_FORBIDDEN)
    assert sorted(got) == ["query.sql: truth_", "settings.yaml: ground_truth", "settings.yaml: truth.sqlite"]
