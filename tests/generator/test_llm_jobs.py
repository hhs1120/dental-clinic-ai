"""LLM 작업 내보내기·들여오기 (가상 데이터 생성 설계 §5.1 'LLM 캐시'·'LLM 단계 채우기') — 장난감 작업으로 틀만 본다.
통화 전사·차트 서술 작업은 tests/generator/test_transcripts.py·test_chart.py."""

import json
from dataclasses import replace

import pytest

from generator import llm_jobs
from generator.llm_jobs import LLMContext, Task

SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string"}},
    "required": ["text"],
    "additionalProperties": False,
}
CHECKS = {"field": "text", "must_include": [["사과", "능금"]], "forbidden_words": ["배"]}


@pytest.fixture
def paths(tmp_path, monkeypatch):
    prompts = tmp_path / "prompts"
    (prompts / "toy").mkdir(parents=True)
    (prompts / "toy" / "v1.md").write_text("과일 이야기를 써라", encoding="utf-8")
    monkeypatch.setattr(llm_jobs, "PROMPTS", prompts)
    return {
        "llm_cache": tmp_path / "cache",
        "llm_jobs": tmp_path / "jobs",
        "llm_results": tmp_path / "results",
    }


TOY = Task(name="toy", prompt="toy/v1", schema=SCHEMA, store=("text",))
RULES = {
    "must_mention": [["사과", "능금"]],
    "avoid_words": ["배"],
    "avoid_patterns": [],
}  # CHECKS에서 뽑아 입력에 넣는 지시


def _export(paths, items=("a", "b"), simplify=None, payload=lambda i: {"item": i}):
    ctx = LLMContext(paths, fake=False)
    outs = {i: ctx.resolve(TOY, i, payload(i), CHECKS, simplify) for i in items}
    return outs, ctx.finish()


def _jobs(paths):
    return [
        json.loads(line)
        for line in (paths["llm_jobs"] / "toy.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def _result(paths, name, rows):
    paths["llm_results"].mkdir(parents=True, exist_ok=True)
    (paths["llm_results"] / name).write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8"
    )


def test_miss_exports_self_contained_jobs(paths):
    outs, missing = _export(paths)
    assert outs == {"a": None, "b": None} and missing == {"toy": 2}
    job = _jobs(paths)[0]
    assert (
        job["item"] == "a"
        and job["attempt"] == 1
        and job["prompt_version"] == f"toy/v1@{job['prompt_hash'][:8]}"
    )
    assert (
        job["system"] == "과일 이야기를 써라" and json.loads(job["user"]) == {"item": "a"} | RULES
    )
    assert job["schema"] == SCHEMA and job["checks"] == CHECKS and len(job["key"]) == 64


def test_import_validates_and_cache_hits_next_run(paths):
    _export(paths)
    a, b = _jobs(paths)
    _result(
        paths,
        "toy-000.claude-code.jsonl",
        [
            {"key": a["key"], "output": {"text": "능금 한 알"}, "source": "claude-code"},
            {
                "key": b["key"],
                "output": {"text": "배 한 알"},
                "source": "claude-code",
            },  # 꼭 넣을 것 없음·금지 어휘
            {"key": "0" * 64, "output": {"text": "사과"}, "source": "claude-code"},
        ],
    )
    report = llm_jobs.import_results(paths)
    assert report == {"toy": {"ok": 1, "failed": 1, "unknown": 1, "duplicate": 0, "bad": 0}}
    assert not list(paths["llm_results"].glob("*.jsonl"))  # 들여온 결과 파일은 지운다
    cache = [
        json.loads(line)
        for line in (paths["llm_cache"] / "toy.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [(e["item"], e["status"], e["source"]) for e in cache] == [
        ("a", "ok", "claude-code"),
        ("b", "failed", "claude-code"),
    ]
    assert any("사과" in p for p in cache[1]["errors"]) and any(
        "배" in p for p in cache[1]["errors"]
    )
    # 다음 실행: a는 캐시에서, b는 시도 번호를 올려 다시 나온다
    outs, missing = _export(paths)
    assert outs["a"] == {"text": "능금 한 알"} and outs["b"] is None and missing == {"toy": 1}
    assert [(j["item"], j["attempt"]) for j in _jobs(paths)] == [("b", 2)]


def test_error_result_counts_as_failed_attempt(paths):
    """채우는 쪽이 거절·잘림을 보고하면(error) 실패 시도로 남는다"""
    _export(paths, items=("a",))
    key = _jobs(paths)[0]["key"]
    _result(paths, "toy.api.jsonl", [{"key": key, "error": "refusal", "source": "model-a"}])
    assert llm_jobs.import_results(paths)["toy"]["failed"] == 1
    _export(paths, items=("a",))
    assert _jobs(paths)[0]["attempt"] == 2


def test_duplicate_result_is_ignored(paths):
    _export(paths, items=("a",))
    row = {"key": _jobs(paths)[0]["key"], "output": {"text": "사과"}, "source": "claude-code"}
    _result(paths, "one.jsonl", [row])
    _result(paths, "two.jsonl", [row])
    assert llm_jobs.import_results(paths)["toy"] == {
        "ok": 1,
        "failed": 0,
        "unknown": 0,
        "duplicate": 1,
        "bad": 0,
    }


def test_input_change_misses(paths):
    """입력(라벨·사실)이 바뀌면 같은 건이라도 미스 — 옛 출력을 쓰지 않는다"""
    _export(paths, items=("a",))
    _result(
        paths,
        "r.jsonl",
        [{"key": _jobs(paths)[0]["key"], "output": {"text": "사과"}, "source": "x"}],
    )
    llm_jobs.import_results(paths)
    outs, _ = _export(paths, items=("a",), payload=lambda i: {"item": i, "label": "바뀜"})
    assert outs["a"] is None and _jobs(paths)[0]["attempt"] == 1


def test_prompt_change_misses(paths):
    _export(paths, items=("a",))
    _result(
        paths,
        "r.jsonl",
        [{"key": _jobs(paths)[0]["key"], "output": {"text": "사과"}, "source": "x"}],
    )
    llm_jobs.import_results(paths)
    (llm_jobs.PROMPTS / "toy" / "v1.md").write_text("다른 지시", encoding="utf-8")
    outs, _ = _export(paths, items=("a",))
    assert outs["a"] is None


def test_simplify_after_failures(paths):
    """SIMPLIFY_AFTER번 실패한 뒤로는 단순화한 입력으로 내보낸다 (§4.1 절차 6)"""

    def simplify(p):
        return {"item": p["item"], "simple": True}

    for attempt in range(1, llm_jobs.MAX_ATTEMPTS + 1):
        _export(paths, items=("a",), simplify=simplify)
        job = _jobs(paths)[0]
        assert job["attempt"] == attempt
        assert ("simple" in json.loads(job["user"])) == (attempt > llm_jobs.SIMPLIFY_AFTER)
        _result(paths, "r.jsonl", [{"key": job["key"], "output": {"text": "배"}, "source": "x"}])
        llm_jobs.import_results(paths)


def test_no_miss_removes_stale_jobs_file(paths):
    _export(paths, items=("a",))
    _result(
        paths,
        "r.jsonl",
        [{"key": _jobs(paths)[0]["key"], "output": {"text": "사과"}, "source": "x"}],
    )
    llm_jobs.import_results(paths)
    _, missing = _export(paths, items=("a",))
    assert missing == {} and not (paths["llm_jobs"] / "toy.jsonl").exists()


def test_fake_fills_without_cache_or_validation(paths):
    ctx = LLMContext(paths, fake=True)
    out = ctx.resolve(TOY, "a", {"item": "a"}, CHECKS)
    assert set(out) == {"text"} and out == LLMContext(paths, fake=True).resolve(
        TOY, "a", {"item": "a"}, CHECKS
    )
    assert (
        ctx.finish() == {} and not paths["llm_cache"].exists() and not paths["llm_jobs"].exists()
    )
    assert (
        LLMContext(paths, fake=True).resolve(
            Task("toy", "toy/v1", SCHEMA, ("text",), fake=False), "a", {}, CHECKS
        )
        is None
    )


def test_check_and_split(paths, capsys):
    _export(paths, items=[f"i{n}" for n in range(5)])
    chunks = llm_jobs.split_jobs(paths, size=2)
    assert [c.name for c in chunks] == ["toy-000.jsonl", "toy-001.jsonl", "toy-002.jsonl"]
    assert sum(len(c.read_text(encoding="utf-8").splitlines()) for c in chunks) == 5
    keys = [j["key"] for j in _jobs(paths)]
    _result(
        paths,
        "toy-000.claude-code.jsonl",
        [
            {"key": keys[0], "output": {"text": "사과"}, "source": "claude-code"},
            {"key": keys[1], "output": {"text": "귤"}, "source": "claude-code"},
        ],
    )
    bad = llm_jobs.check_results(paths, paths["llm_results"] / "toy-000.claude-code.jsonl")
    assert list(bad) == [keys[1]]
    assert list(paths["llm_results"].glob("*.jsonl"))  # 검사는 지우지 않는다


@pytest.mark.parametrize(
    "output, problem",
    [
        ({"text": "환자: 사과요\n직원: 네"}, None),
        ({"text": "환자: 사과요\n손님: 네"}, "화자"),
        ({"text": "환자: 사과요"}, "화자"),  # 두 화자 모두 나와야 함
        ({"text": "환자: 사과 사과 사과 사과요\n직원: 네"}, "길이"),
        ({"text": "환자: 사과 턱관절\n직원: 네"}, "금지 토픽"),
        ({"text": "환자: 사과Pro요\n직원: 네"}, "다른 글자"),  # 대사 속 영문 찌꺼기
        ({"text": "환자: 사과요\n直원: 네"}, "다른 글자"),  # 한자로 깨진 화자 머리
        ({}, "필드"),
    ],
)
def test_problems(output, problem):
    checks = {
        "field": "text",
        "must_include": [["사과"]],
        "speakers": ["환자:", "직원:"],
        "length": [3, 8],
        "forbidden_patterns": ["턱관절"],
    }
    got = llm_jobs.problems(SCHEMA, checks, output)
    assert (got == []) if problem is None else any(problem in p for p in got)


class StubBatchGateway:
    """Gateway의 Batch 메서드만 흉내 — 결과는 custom_id(작업 키) → LLMResult 또는 None(처리 안 됨)"""

    def __init__(self, results):
        self.results, self.submitted, self.polls = results, None, 0

    def submit_batch(self, alias, requests, *, task, prompt_version=None):
        self.submitted = (alias, task, prompt_version, requests)
        return "batch_1"

    def batch_status(self, batch_id, alias=None):
        self.polls, self.status_alias = self.polls + 1, alias
        return "in_progress" if self.polls == 1 else "ended"

    def batch_results(self, batch_id, alias, *, task, prompt_version=None, schema=None):
        return self.results(self.submitted[3])


def _llm_result(ok, data=None, error=None):
    from shared.llm import LLMResult

    return LLMResult(ok, json.dumps(data or {}), data, "model-a", False, 10, 5, 0.001, 0, error)


def test_fill_api_writes_results_for_import(paths):
    _export(paths, items=("a", "b", "c"))
    gateway = StubBatchGateway(
        lambda reqs: {
            reqs[0]["custom_id"]: _llm_result(True, {"text": "사과"}),
            reqs[1]["custom_id"]: _llm_result(False, error="model-a: refusal"),
            reqs[2]["custom_id"]: None,  # 만료 — 다음에 같은 작업으로 다시
        }
    )
    sleeps = []
    llm_jobs.fill_api(paths, "toy", gateway, sleep=sleeps.append)
    alias, task, version, reqs = gateway.submitted
    assert (alias, task, version) == (
        "gen",
        "toy",
        f"toy/v1@{_jobs(paths)[0]['prompt_hash'][:8]}",
    ) and len(reqs) == 3
    assert reqs[0]["system"] == "과일 이야기를 써라" and reqs[0]["schema"] == SCHEMA
    assert reqs[0]["messages"] == [
        {"role": "user", "content": llm_jobs.user_message({"item": "a"} | RULES)}
    ]
    assert len(sleeps) == 1 and not (paths["llm_jobs"] / "toy.batch.json").exists()
    assert llm_jobs.import_results(paths)["toy"] == {
        "ok": 1,
        "failed": 1,
        "unknown": 0,
        "duplicate": 0,
        "bad": 0,
    }
    _, missing = _export(paths, items=("a", "b", "c"))
    assert [(j["item"], j["attempt"]) for j in _jobs(paths)] == [("b", 2), ("c", 1)]


def test_fill_api_resumes_saved_batch(paths):
    """제출 뒤 끊겨도 배치 번호가 남아 있으면 다시 제출하지 않고 이어서 기다린다"""
    _export(paths, items=("a",))
    key = _jobs(paths)[0]["key"]
    (paths["llm_jobs"] / "toy.batch.json").write_text(
        json.dumps({"batch_id": "batch_1", "keys": [key]}), encoding="utf-8"
    )
    gateway = StubBatchGateway(lambda reqs: {key: _llm_result(True, {"text": "사과"})})
    gateway.submitted = ("gen", "toy", "toy/v1", [])
    gateway.submit_batch = None  # 부르면 실패
    llm_jobs.fill_api(paths, "toy", gateway, sleep=lambda s: None)
    assert llm_jobs.import_results(paths)["toy"]["ok"] == 1


def test_cache_keeps_only_store_fields(paths):
    """출력은 Task.store 필드만 캐시에 — 전사의 scenario처럼 라벨을 풀어 쓴 필드는 버린다"""
    _export(paths, items=("a",))
    _result(
        paths,
        "toy.jsonl",
        [
            {
                "key": _jobs(paths)[0]["key"],
                "output": {"text": "사과", "scenario": "라벨"},
                "source": "x",
            }
        ],
    )
    llm_jobs.import_results(paths)
    assert "라벨" not in (paths["llm_cache"] / "toy.jsonl").read_text(encoding="utf-8")
    assert _export(paths, items=("a",))[0]["a"] == {"text": "사과"}


def test_exhausted_after_max_attempts(paths):
    """MAX_ATTEMPTS번 실패하면 더 내보내지 않고 시도 소진으로 남긴다"""
    for _ in range(llm_jobs.MAX_ATTEMPTS):
        _export(paths, items=("a",))
        _result(
            paths,
            "toy.jsonl",
            [{"key": _jobs(paths)[0]["key"], "error": "refusal", "source": "x"}],
        )
        llm_jobs.import_results(paths)
    ctx = LLMContext(paths, fake=False)
    assert ctx.resolve(TOY, "a", {"item": "a"}, CHECKS) is None
    assert ctx.finish() == {} and ctx.exhausted == {"toy": ["a"]}


def test_split_and_fill_skip_already_imported(paths):
    """들여온 뒤 make data 전에 다시 채워도 통과한 건은 다시 내지 않는다"""
    _export(paths, items=("a", "b"))
    a = _jobs(paths)[0]["key"]
    _result(paths, "toy.jsonl", [{"key": a, "output": {"text": "사과"}, "source": "x"}])
    llm_jobs.import_results(paths)
    assert [j["item"] for j in llm_jobs.pending(paths, "toy")] == ["b"]
    assert (
        sum(len(c.read_text(encoding="utf-8").splitlines()) for c in llm_jobs.split_jobs(paths))
        == 1
    )
    gateway = StubBatchGateway(lambda reqs: {})
    llm_jobs.fill_api(paths, "toy", gateway, sleep=lambda s: None)
    assert [r["custom_id"] for r in gateway.submitted[3]] == [_jobs(paths)[1]["key"]]


def test_fill_api_limit_spreads_sample(paths):
    """시험 채우기 — 남은 작업에서 limit건을 같은 간격으로 뽑는다(앞쪽에 몰리지 않게). 나머지는 다음에 그대로"""
    _export(paths, items=tuple("abcdef"))
    gateway = StubBatchGateway(lambda reqs: {})
    llm_jobs.fill_api(paths, "toy", gateway, sleep=lambda s: None, limit=3)
    keys = [j["key"] for j in _jobs(paths)]
    assert [r["custom_id"] for r in gateway.submitted[3]] == [keys[0], keys[2], keys[4]]


def test_fill_api_limit_must_be_positive(paths):
    with pytest.raises(SystemExit):
        llm_jobs.main(["fill-api", "--task", "toy", "--limit", "0"])


def test_prompt_version_tells_revisions_apart(paths):
    """생성기 프롬프트는 파일 하나를 고쳐 쓴다 — 호출 기록·캐시에서 수정 전후를 가르도록 버전에 내용 해시"""
    _export(paths, items=("a",))
    before = _jobs(paths)[0]["prompt_version"]
    (paths["llm_jobs"] / "toy.jsonl").unlink()
    (llm_jobs.PROMPTS / "toy" / "v1.md").write_text("과일 이야기를 길게 써라", encoding="utf-8")
    _export(paths, items=("a",))
    after = _jobs(paths)[0]["prompt_version"]
    assert before.startswith("toy/v1@") and after.startswith("toy/v1@") and before != after


def test_cache_misses_when_rendered_instructions_change(paths, monkeypatch):
    """입력 해시는 모델이 받는 사용자 메시지 전부 — instructions()가 바뀌면 같은 입력·검증 기준이어도 다시 낸다"""
    _export(paths, items=("a",))
    _result(
        paths,
        "toy.jsonl",
        [{"key": _jobs(paths)[0]["key"], "output": {"text": "사과"}, "source": "x"}],
    )
    llm_jobs.import_results(paths)
    assert _export(paths, items=("a",))[0]["a"] == {"text": "사과"}
    original = llm_jobs.instructions
    monkeypatch.setattr(
        llm_jobs, "instructions", lambda checks: original(checks) | {"hint": "새 지시"}
    )
    assert _export(paths, items=("a",))[0]["a"] is None


def test_fill_api_nothing_to_do(paths, capsys):
    assert llm_jobs.fill_api(paths, "toy", StubBatchGateway(lambda reqs: {})) is None
    assert "채울 작업 없음" in capsys.readouterr().out


def test_fill_api_stale_batch_stops(paths):
    """저장된 배치의 키가 지금 작업 파일에 없으면(작업을 다시 냄) 기다리지 않고 멈춘다"""
    _export(paths, items=("a",))
    (paths["llm_jobs"] / "toy.batch.json").write_text(
        json.dumps({"batch_id": "old", "keys": ["0" * 64]}), encoding="utf-8"
    )
    with pytest.raises(llm_jobs.StaleBatch):
        llm_jobs.fill_api(paths, "toy", StubBatchGateway(lambda reqs: {}))


def test_bad_lines_are_counted_not_fatal(paths):
    _export(paths, items=("a",))
    key = _jobs(paths)[0]["key"]
    paths["llm_results"].mkdir(parents=True, exist_ok=True)
    f = paths["llm_results"] / "toy-000.claude-code.jsonl"
    f.write_text(
        "{깨진 줄\n"
        + json.dumps({"key": key, "output": {"text": "사과"}})
        + "\n"
        + json.dumps({"key": key, "output": {"text": "사과"}, "source": "x"})
        + "\n",
        encoding="utf-8",
    )
    assert list(llm_jobs.check_results(paths, f)) == ["줄 1", "줄 2"]
    assert llm_jobs.import_results(paths)["toy"] == {
        "ok": 1,
        "failed": 0,
        "unknown": 0,
        "duplicate": 0,
        "bad": 2,
    }


@pytest.mark.parametrize(
    "text, word, hit",
    [
        ("12월 5일에 오세요", "2월 5일", False),
        ("2월 5일에 오세요", "2월 5일", True),
        ("11회 더", "1회", False),
        ("1회 더", "1회", True),
        ("16mm", "6mm", False),
        ("치주낭 6mm", "6mm", True),
    ],
)
def test_contains_needs_no_digit_before(text, word, hit):
    assert llm_jobs.contains(text, word) is hit


def test_pending_skips_unimported_results_and_running_batch(paths):
    """들여오기 전 결과 파일·진행 중 배치에 있는 작업도 다시 채우지 않는다 (비용 두 번 방지)"""
    _export(paths, items=("a", "b", "c"))
    a, b, c = (j["key"] for j in _jobs(paths))
    _result(paths, "toy.jsonl", [{"key": a, "output": {"text": "사과"}, "source": "x"}])
    (paths["llm_jobs"] / "toy.batch.json").write_text(
        json.dumps({"batch_id": "b1", "keys": [b]}), encoding="utf-8"
    )
    assert [j["key"] for j in llm_jobs.pending(paths, "toy")] == [c]


def test_import_prefers_passing_result(paths):
    """같은 키에 실패·통과 결과가 둘 다 있으면 통과를 남긴다"""
    _export(paths, items=("a",))
    key = _jobs(paths)[0]["key"]
    _result(paths, "toy-000.jsonl", [{"key": key, "output": {"text": "배"}, "source": "x"}])
    _result(paths, "toy-001.jsonl", [{"key": key, "output": {"text": "사과"}, "source": "y"}])
    assert llm_jobs.import_results(paths)["toy"] == {
        "ok": 1,
        "failed": 0,
        "unknown": 0,
        "duplicate": 1,
        "bad": 0,
    }
    assert _export(paths, items=("a",))[0]["a"] == {"text": "사과"}


def test_non_object_output_not_stored(paths):
    _export(paths, items=("a",))
    _result(
        paths,
        "toy.jsonl",
        [{"key": _jobs(paths)[0]["key"], "output": "라벨이 든 문자열", "source": "x"}],
    )
    assert llm_jobs.import_results(paths)["toy"]["failed"] == 1
    assert "라벨" not in (paths["llm_cache"] / "toy.jsonl").read_text(encoding="utf-8")


def test_forbidden_words_ignore_spaces():
    """금지 어휘는 띄어쓰기를 무시 — '예약 변경' 금지가 '예약변경'도 잡는다(verify 라벨 어휘 경고와 같은 기준)"""
    checks = {"field": "text", "forbidden_words": ["예약 변경"]}
    assert llm_jobs.problems(SCHEMA, checks, {"text": "예약변경 하려고요"})
    assert llm_jobs.has_word("예약변경", "예약 변경") and not llm_jobs.has_word("13회", "3회")
    assert not llm_jobs.has_word(
        "필요 건수", "요건"
    )  # 띄어쓰기 없는 말은 낱말 경계를 넘어 맞추지 않는다


def test_forbidden_words_ignore_longer_numbers():
    """금지 어휘도 꼭 넣을 말처럼 앞에 숫자가 붙으면 다른 말 — '3회' 금지가 '13회'에 걸리지 않게"""
    checks = {"field": "text", "forbidden_words": ["3회"]}
    assert llm_jobs.problems(SCHEMA, checks, {"text": "13회 내원"}) == []
    assert llm_jobs.problems(SCHEMA, checks, {"text": "3회 내원"})


def test_instructions_include_forbidden_topics():
    """API도 Claude Code와 같은 금지 토픽을 본다"""
    got = llm_jobs.instructions({"field": "t", "forbidden_patterns": ["턱관절"], "length": [1, 2]})
    assert got == {
        "must_mention": [],
        "avoid_words": [],
        "avoid_patterns": ["턱관절"],
        "length_chars": [1, 2],
    }


def test_instructions_show_alternatives():
    """꼭 넣을 말에 별칭이 있으면 후보 목록 그대로 — 쓰는 쪽이 별칭을 알아야 쓴다. 하나뿐이면 문자열"""
    got = llm_jobs.instructions({"must_include": [["교정 월 체크", "교정 체크"], ["11월 24일"]]})
    assert got["must_mention"] == [["교정 월 체크", "교정 체크"], "11월 24일"]


@pytest.mark.parametrize(
    "body, problem",
    [
        ("# 문서\n## 가\n90일 기준\n## 나\n소견", None),
        ("# 문서\n### 가\n90일 기준\n## 나\n소견", "절 ## 가 0개"),
        ("# 문서\n## 가 기준\n90일\n## 나\n소견", "절 ## 가 0개"),
        ("# 문서\n## 가\n90일\n## 가\n## 나\n소견", "절 ## 가 2개"),
        ("90일 기준\n## 가\n설명\n## 나\n소견", "밖에 있는 말: 90일"),
    ],
)
def test_section_problems(body, problem):
    """청구 기준 절 — 소제목이 정확히 한 번, 기준 말은 그 절 안 (verify check_claim_sections와 같은 로직)"""
    got = llm_jobs.section_problems(body, [["## 가", ["90일"]], ["## 나", []]])
    assert (got == []) if problem is None else any(problem in p for p in got)


def _cache_lines(paths):
    return [
        json.loads(line)
        for line in (paths["llm_cache"] / "toy.jsonl").read_text(encoding="utf-8").splitlines()
    ]


def test_prune_keeps_only_entries_this_run_used(paths):
    """캐시 정리: 이번 실행이 찾아본 (건·프롬프트·입력)의 줄만 남긴다 — 입력·프롬프트가 바뀐 옛 줄은 지우고, 쓰는 항목의
    실패 시도 줄은 남긴다(시도 번호). 빈 건이 있으면 정리하지 않는다"""
    _export(paths, items=("a", "b"))
    a, b = _jobs(paths)
    _result(
        paths,
        "r.jsonl",
        [
            {"key": a["key"], "output": {"text": "사과"}, "source": "claude-code"},
            {
                "key": b["key"],
                "output": {"text": "귤"},
                "source": "claude-code",
            },  # 실패(꼭 넣을 말 없음)
        ],
    )
    llm_jobs.import_results(paths)
    _export(paths, items=("b",))  # b 2번째 시도
    (b2,) = _jobs(paths)
    _result(
        paths,
        "r2.jsonl",
        [{"key": b2["key"], "output": {"text": "능금"}, "source": "claude-code"}],
    )
    llm_jobs.import_results(paths)
    ctx = LLMContext(paths, fake=False)
    ctx.resolve(TOY, "a", {"item": "a", "v": 2}, CHECKS)  # a 입력이 바뀜 — 옛 a 줄은 안 쓰임
    assert ctx.prune() is None  # 빈 건(a)이 있으면 정리하지 않음
    assert len(_cache_lines(paths)) == 3
    ctx = LLMContext(paths, fake=False)
    assert ctx.resolve(TOY, "b", {"item": "b"}, CHECKS) == {"text": "능금"}
    assert ctx.prune() == {"toy": 1}  # a 줄만 지움
    assert [(e["item"], e["attempt"]) for e in _cache_lines(paths)] == [("b", 1), ("b", 2)]


# ── 작업별 별칭·건 지정 바로 채우기 (어려운 통화 설계 §3.5 — GPT 녹취, 샘플 확인) ─────────


def _export_task(paths, task, items):
    ctx = LLMContext(paths, fake=False)
    for i in items:
        ctx.resolve(task, i, {"item": i}, CHECKS)
    return ctx.finish()


def test_fill_api_uses_task_alias(paths):
    _export_task(paths, replace(TOY, alias="gen_hard"), ("a", "b"))
    assert {j["alias"] for j in _jobs(paths)} == {"gen_hard"}
    gateway = StubBatchGateway(lambda reqs: {})
    llm_jobs.fill_api(paths, "toy", gateway, sleep=lambda s: None)
    assert gateway.submitted[0] == "gen_hard" and gateway.status_alias == "gen_hard"


class StubCompleteGateway:
    def __init__(self):
        self.calls = []

    def complete(self, alias, *, task, messages, system=None, schema=None, prompt_version=None):
        self.calls.append((alias, messages[0]["content"]))
        return _llm_result(True, {"text": "사과"})


def test_fill_api_items_without_batch(paths):
    """건 지정 + 바로 부르기 — 고른 건만 complete로 채우고 같은 결과 파일 모양으로 쓴다(배치 상태 파일 없음)"""
    _export_task(paths, replace(TOY, alias="gen_hard"), ("a", "b", "c"))
    gateway = StubCompleteGateway()
    out = llm_jobs.fill_api(paths, "toy", gateway, items=["a", "c"], use_batch=False)
    assert [a for a, _ in gateway.calls] == ["gen_hard", "gen_hard"]
    assert len(gateway.calls) == 2 and not (paths["llm_jobs"] / "toy.batch.json").exists()
    assert out.exists()  # 들여오면 결과 파일은 옮겨진다
    assert llm_jobs.import_results(paths)["toy"]["ok"] == 2
    assert [j["item"] for j in llm_jobs.pending(paths, "toy")] == ["b"]
