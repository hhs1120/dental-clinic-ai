"""eval.py 실행 (평가 설계 §7) — 리포트 md·json, 실행 이력, test 묶음별 1회 가드, 건별 목록은 dev만, 앱 연결."""

import json
import sys

import pytest

from evaluation import EvalRefused
from evaluation import eval as ev
from tests.evaluation.test_rag import SEARCH, FakeApp, make_rag_db


@pytest.fixture
def conn(tmp_path):
    return make_rag_db(tmp_path)


@pytest.fixture
def reports(tmp_path):
    return tmp_path / "reports"


def _run(conn, reports, **kw):
    return ev.run(conn, reports_dir=reports, app=kw.pop("app", FakeApp()), **kw)


def _json(out):
    return json.loads(out["report_path"].with_suffix(".json").read_text(encoding="utf-8"))


def _history(reports):
    return [
        json.loads(line)
        for line in (reports / "eval_runs.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def test_dev_run_writes_reports_and_history(conn, reports):
    out = _run(conn, reports, split="dev")
    md = out["report_path"]
    assert (
        md.exists() and md.with_suffix(".json").exists() and md.name.startswith("eval_dev_base_")
    )
    data = _json(out)
    assert (
        data["split"] == "dev"
        and data["rag"]["retrieval"]["hit5"]["n"] == 3
        and data["rag"]["chat"]["tool"]["n"] == 1
    )
    assert data["classification"]["dump"]  # dev는 건별 덤프
    assert "c-0002" in md.read_text(encoding="utf-8")
    (line,) = _history(reports)
    assert line | {"started_at": None} == {
        "split": "dev",
        "section": "all",
        "condition": "base",
        "prompt_version": "all",
        "started_at": None,
        "report_path": md.name,
    }


def test_test_refusals_leave_no_trace(conn, reports):
    """test를 돌리지 못할 이유가 있으면 리포트·이력을 쓰기 전에 멈춘다 — 앱 없음·오타가 1회 기회를 소진하지 않게"""
    for kw, msg in (
        ({}, "section"),
        ({"section": "classification"}, "prompt-version"),
        ({"section": "classification", "prompt_version": "v9"}, "v9"),
        ({"section": "retrieval", "app": None}, "앱"),
        ({"section": "chat", "app": None}, "앱"),
    ):
        with pytest.raises(EvalRefused, match=msg):
            _run(conn, reports, split="test", **kw)
    for kw, msg in (
        ({"section": "chat", "retrieval_only": True}, "retrieval-only"),
        ({"section": "classification", "prompt_version": "v1", "condition": "x=1"}, "condition"),
        ({"section": "retrieval", "prompt_version": "v1"}, "prompt-version"),
    ):
        with pytest.raises(EvalRefused, match=msg):  # 묶음과 안 맞는 옵션
            _run(conn, reports, split="test", **kw)
    assert not (reports / "eval_runs.jsonl").exists()
    with pytest.raises(EvalRefused, match="base"):
        _run(conn, reports, split="dev", condition="base")
    with pytest.raises(EvalRefused, match="section"):
        _run(conn, reports, split="dev", section="chat")  # dev는 묶음 없이 전부


def test_test_sections_have_no_item_lists(conn, reports):
    cls = _json(_run(conn, reports, split="test", section="classification", prompt_version="v1"))
    assert (
        cls["rag"] is None
        and "dump" not in cls["classification"]
        and list(cls["classification"]["versions"]) == ["v1"]
    )
    ret = _json(_run(conn, reports, split="test", section="retrieval"))
    assert (
        ret["classification"] is None
        and ret["rag"]["chat"] is None
        and "questions" not in ret["rag"]
    )
    assert all(
        set(q) <= {"id", "type", "hit", "top1", "fresh", "rr", "chat_ok"}
        for q in ret["rag"]["paired"]
    )
    chat = _json(_run(conn, reports, split="test", section="chat"))
    assert (
        chat["rag"]["retrieval"] is None and chat["rag"]["chat"]["refusal"]["n"] == 0
    )  # test 문항엔 거절형 없음


def test_test_runs_once_per_section(conn, reports):
    """막는 단위 = 묶음(분류·버전 / 검색·조건 / 챗·조건) — 검색 test를 돌려도 챗 test는 막히지 않는다"""
    _run(conn, reports, split="test", section="retrieval")
    with pytest.raises(ev.AlreadyRun):
        _run(conn, reports, split="test", section="retrieval")
    with pytest.raises(ev.AlreadyRun):
        _run(conn, reports, split="test", retrieval_only=True)  # --retrieval-only = 검색 묶음
    _run(conn, reports, split="test", section="chat")
    _run(conn, reports, split="test", section="retrieval", condition="reranker=off")
    _run(conn, reports, split="test", section="classification", prompt_version="v1")
    _run(conn, reports, split="test", section="classification", prompt_version="v2")
    with pytest.raises(ev.AlreadyRun):
        _run(conn, reports, split="test", section="classification", prompt_version="v1")
    _run(conn, reports, split="test", section="retrieval", force=True)
    assert len(_history(reports)) == 6


def test_dev_runs_repeat_freely(conn, reports):
    a, b = _run(conn, reports, split="dev"), _run(conn, reports, split="dev")
    assert a["report_path"] != b["report_path"]  # 같은 초에 돌려도 파일이 겹치지 않음


def test_dev_condition_runs_base_too(conn, reports):
    app = FakeApp()
    data = _json(
        _run(conn, reports, split="dev", retrieval_only=True, condition="reranker=off", app=app)
    )
    assert {c for kind, _, c in app.calls} == {None, "reranker=off"}
    cmp = data["rag"]["compare"]
    assert cmp["metrics"]["hit5"]["n"] == 3 and "mrr" in cmp["metrics"] and "rescued" in cmp


def test_test_condition_compares_with_stored_base(conn, reports):
    """test 조건 비교는 기본 조건을 다시 돌리지 않고 이력의 기본 조건 test 결과와 — 구제/손실 목록은 내지 않는다"""
    _run(conn, reports, split="test", section="retrieval")
    app = FakeApp()
    data = _json(
        _run(conn, reports, split="test", section="retrieval", condition="reranker=off", app=app)
    )
    assert {c for kind, _, c in app.calls} == {"reranker=off"}
    assert data["rag"]["compare"]["metrics"]["hit5"] == {"base": 1, "cond": 1, "n": 1, "p": 1.0}
    assert "rescued" not in data["rag"]["compare"]


def test_test_condition_without_base(conn, reports):
    data = _json(_run(conn, reports, split="test", section="retrieval", condition="reranker=off"))
    assert data["rag"]["compare"] is None


def test_test_condition_uses_latest_usable_base(conn, reports):
    """기본 조건 test가 여러 번이면(--force) 마지막 것, 쓸 수 없는 json(rag·paired 없음)은 건너뛴다"""
    first = _run(conn, reports, split="test", section="retrieval")
    worse = dict(SEARCH, m2={"manual_results": [], "case_results": []})
    _run(conn, reports, split="test", section="retrieval", force=True, app=FakeApp(search=worse))
    data = _json(_run(conn, reports, split="test", section="retrieval", condition="reranker=off"))
    assert data["rag"]["compare"]["metrics"]["hit5"]["base"] == 0  # 마지막 기본 결과(놓친 것)
    broken = reports / "eval_runs.jsonl"
    last = json.loads(broken.read_text(encoding="utf-8").splitlines()[1])
    for n, broken_text in enumerate(
        ('{"rag": null}', '{"rag": {"pai')
    ):  # 비었거나 중간에 잘린 json
        (reports / last["report_path"]).with_suffix(".json").write_text(
            broken_text, encoding="utf-8"
        )
        data = _json(_run(conn, reports, split="test", section="retrieval", condition=f"x={n}"))
        assert (
            data["rag"]["compare"]["metrics"]["hit5"]["base"] == 1
        )  # 깨진 마지막 대신 첫 기본 결과
    assert first["report_path"].exists()


def test_no_app_skips_rag(conn, reports):
    out = _run(conn, reports, split="dev", app=None)
    assert (
        "앱 없음" in out["report_path"].read_text(encoding="utf-8") and _json(out)["rag"] is None
    )


def test_prompt_version_filter_and_fewshot(conn, reports, tmp_path):
    prompts = tmp_path / "prompts"
    (prompts / "classify").mkdir(parents=True)
    (prompts / "classify" / "v1.md").write_text(
        "---\nfewshot_call_uids: [c-0001]\n---\n분류하라", encoding="utf-8"
    )
    data = _json(_run(conn, reports, split="dev", prompt_version="v1", prompts_dir=prompts))
    assert list(data["classification"]["versions"]) == ["v1"]
    assert data["classification"]["versions"]["v1"]["excluded"]["fewshot"] == 1


def test_fewshot_from_test_split_refused(conn, reports, tmp_path):
    """few-shot 예시에 test 통화(c-0003)를 쓰면 평가를 거부한다 — test 점수가 샌다"""
    prompts = tmp_path / "prompts"
    (prompts / "classify").mkdir(parents=True)
    (prompts / "classify" / "v1.md").write_text(
        "---\nfewshot_call_uids: [c-0003]\n---\n분류하라", encoding="utf-8"
    )
    with pytest.raises(EvalRefused, match="test 통화"):
        _run(conn, reports, split="dev", prompt_version="v1", prompts_dir=prompts)


@pytest.mark.parametrize(
    "text, uids",
    [
        ("---\nfewshot_call_uids: [c-0001, c-0042]\n---\n본문", {"c-0001", "c-0042"}),
        ("---\r\nfewshot_call_uids:\r\n  - c-0001\r\n---\r\n본문", {"c-0001"}),  # CRLF·블록 목록
        ("---\nfewshot_call_uids: [c-0001]\n---", {"c-0001"}),  # 머리말 뒤 본문 없음
        ("---\nnote: x\n---\n본문", set()),
        ("본문만", set()),
    ],
)
def test_fewshot_header_parse(tmp_path, text, uids):
    p = tmp_path / "v.md"
    p.write_text(text, encoding="utf-8")
    assert ev.fewshot_uids(p) == uids


def test_fewshot_header_malformed_raises(tmp_path):
    """형식이 틀린 머리말을 빈 집합으로 넘기면 예시 통화가 dev 집계에 섞인다"""
    p = tmp_path / "v.md"
    for text in (
        "---\nfewshot_call_uids: [c-0001]\n본문",
        "---\nfewshot_call_uids: c-0001\n---\n",
    ):
        p.write_text(text, encoding="utf-8")
        with pytest.raises(EvalRefused):
            ev.fewshot_uids(p)
    assert ev.fewshot_uids(tmp_path / "none.md") == set()


def test_markdown_cells_escaped(conn, reports):
    """질문에 '|'가 있어도 실패 문항 표가 깨지지 않게 (m1을 검색에서 놓치게 해 표에 올린다)"""
    conn.execute("UPDATE truth.truth_rag_qa SET question = '질문 m1 | 몇 번?' WHERE id = 'm1'")
    app = FakeApp(
        search=dict(SEARCH, **{"m1 | 몇 번?": {"manual_results": [], "case_results": []}})
    )
    out = _run(conn, reports, split="dev", app=app)
    assert "질문 m1 \\| 몇 번?" in out["report_path"].read_text(encoding="utf-8")


# ── main·앱 연결 ────────────────────────────────────────────


def _db_files(conn, tmp_path):
    conn.commit()
    return [
        "--db",
        str(tmp_path / "app.sqlite"),
        "--source",
        str(tmp_path / "source.sqlite"),
        "--truth",
        str(tmp_path / "truth.sqlite"),
    ]


def test_main_exit_codes(conn, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ev, "load_app", lambda db, source: FakeApp())
    args = _db_files(conn, tmp_path) + ["--reports-dir", str(tmp_path / "out")]
    assert ev.main(["--split", "dev"] + args) == 0
    assert ev.main(["--split", "test", "--section", "retrieval"] + args) == 0
    assert ev.main(["--split", "test", "--section", "retrieval"] + args) == 2  # 이미 돌림
    assert ev.main(["--split", "test"] + args) == 1  # 묶음 없음 — 거부는 한 줄 + exit 1
    assert ev.main(["--split", "dev", "--db", str(tmp_path / "nope.sqlite")]) == 1
    assert (tmp_path / "out" / "eval_runs.jsonl").exists()


def test_main_passes_db_to_app(conn, tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(ev, "load_app", lambda db, source: seen.append((db, source)))
    ev.main(["--split", "dev", "--reports-dir", str(tmp_path / "out")] + _db_files(conn, tmp_path))
    assert seen == [(tmp_path / "app.sqlite", tmp_path / "source.sqlite")]


@pytest.fixture
def fake_backend(tmp_path, monkeypatch):
    """임시 backend 패키지 — 진짜 backend를 잠시 내리고 테스트 뒤 돌려놓는다"""
    pkg = tmp_path / "pkgs" / "backend"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path / "pkgs"))

    def loaded():
        return [m for m in sys.modules if m == "backend" or m.startswith("backend.")]

    real = {name: sys.modules.pop(name) for name in loaded()}
    yield pkg
    for name in loaded():
        del sys.modules[name]
    sys.modules.update(real)


def test_load_app_none_without_hooks(fake_backend):
    assert ev.load_app("x.sqlite", "s.sqlite") is None  # backend는 있지만 eval_hooks가 없음


def test_load_app_passes_db(fake_backend):
    (fake_backend / "eval_hooks.py").write_text(
        "def make_app(db, source):\n    return ('app', db, source)\n", encoding="utf-8"
    )
    assert ev.load_app("x.sqlite", "s.sqlite") == ("app", "x.sqlite", "s.sqlite")


def test_load_app_dependency_error_propagates(fake_backend):
    """eval_hooks 안의 의존성이 없으면 '앱 없음'으로 삼키지 않는다 — RAG가 조용히 빠진 리포트를 막음"""
    (fake_backend / "eval_hooks.py").write_text(
        "import not_installed_dependency_xyz\n", encoding="utf-8"
    )
    with pytest.raises(ModuleNotFoundError):
        ev.load_app("x.sqlite", "s.sqlite")


class NoChatApp(FakeApp):
    supports_chat = False
    notes = []

    def warnings(self):
        return self.notes


def test_dev_skips_chat_when_app_has_no_chat(conn, reports):
    app = NoChatApp()
    out = _run(conn, reports, split="dev", app=app)
    assert out["report_path"].exists() and not [c for c in app.calls if c[0] == "chat"]


def test_test_chat_section_is_refused_without_chat(conn, reports):
    with pytest.raises(EvalRefused, match="챗"):
        _run(conn, reports, split="test", section="chat", app=NoChatApp())


def test_test_with_app_warning_is_refused_and_keeps_chance(conn, reports):
    app = NoChatApp()
    app.notes = ["리랭커 실패 1회"]
    with pytest.raises(EvalRefused, match="리랭커"):
        _run(conn, reports, split="test", section="retrieval", app=app)
    assert not (reports / "eval_runs.jsonl").exists()


def test_dev_report_shows_app_warning(conn, reports):
    app = NoChatApp()
    app.notes = ["리랭커 실패 1회"]
    out = _run(conn, reports, split="dev", retrieval_only=True, app=app)
    assert "리랭커 실패 1회" in out["report_path"].read_text(encoding="utf-8")
