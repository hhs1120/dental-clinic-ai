"""프롬프트 불러오기 (backend/prompts.py, 앱 설계 §7.2) — classify는 버전 폴더, 그 밖은 버전 파일."""

import pytest

from backend import prompts


def test_load_classify_folder(tmp_path):
    d = tmp_path / "classify" / "v9"
    d.mkdir(parents=True)
    (d / "labels.md").write_text("---\nfewshot_call_uids: [c-0001]\n---\n라벨 지시\n", "utf-8")
    (d / "summary.md").write_text("요약 지시\n", "utf-8")
    assert prompts.load("classify", "v9", root=tmp_path) == {
        "labels": "라벨 지시",
        "summary": "요약 지시",
    }


def test_load_judge_file(tmp_path):
    (tmp_path / "judge").mkdir()
    (tmp_path / "judge" / "v2.md").write_text("판정 지시", "utf-8")
    assert prompts.load("judge", "v2", root=tmp_path) == {"system": "판정 지시"}


def test_missing_version_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        prompts.load("judge", "v404", root=tmp_path)
    with pytest.raises(FileNotFoundError):
        prompts.load("classify", "v404", root=tmp_path)


def test_repo_v1_prompts_load():
    c = prompts.load("classify", "v1")
    assert "has_todo = true" in c["labels"] and "명사형" in c["summary"]
    assert "has_todo = true" in prompts.load("judge", "v1")["system"]
