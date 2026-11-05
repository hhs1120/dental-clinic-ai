"""분류기(모델 A)·판정기(모델 B) — 입력은 녹취록과 방향뿐(앱 설계 §1, 분류기 설계 "분류기"·"판정기").
분류기는 호출 둘: 라벨(별칭 classify)과 요약·할 일(별칭 summarize). 둘 중 하나라도 실패하면 failed.
라벨은 9항목이 다 있고 값이 목록 안일 때만 ok — 아니면 failed로 남겨 다음 실행이 다시 한다.
판정기는 라벨 한 호출(별칭 judge). 저장은 ax_call_classifications·ax_model_judgments, 같은 (통화, 별칭, 버전)은 덮어쓴다.
labels JSON의 플래그는 0/1(shared/app_schema.sql)
"""

import json
import re
from dataclasses import dataclass

from backend import prompts
from shared.call_labels import (
    COUNTERPARTS,
    FLAGS,
    ITEMS,
    LABEL_SCHEMA,
    PURPOSES,
    SUMMARY_SCHEMA,
    SUMMARY_SCHEMA_KINDS,
    URGENCIES,
)
from shared.llm import LLMResult

MAX_TOKENS = (
    3000  # Haiku 기본 생각 토큰 포함 — 1,000에 잘린 요약을 다시 보낸 최대 1,486(AI 실험 기록 §11)
)
REASONING_MAX_TOKENS = 16000  # 추론을 켜면 추론 토큰도 max_tokens 안(앱 설계 §7)


@dataclass(frozen=True)
class Classification:
    ok: bool
    labels: dict | None
    summary: str | None
    todo: list[str] | None
    label: LLMResult
    summary_call: LLMResult


@dataclass(frozen=True)
class Judgment:
    ok: bool
    labels: dict | None
    call: LLMResult


def user_message(transcript: str, direction: str) -> str:
    return f"방향: {direction}\n\n{transcript}"


def normalize_summary(text: str) -> str:
    """요약 문체 누출 방지(평가 설계 §2.4) — 줄머리 글머리 기호·#·**를 지우고 줄바꿈·연속 공백을 공백 하나로"""
    lines = text.replace("**", "").splitlines()
    lines = [re.sub(r"^\s*(?:[-*•]|\d+[.)]|#+)\s*", "", line) for line in lines]
    return re.sub(r"\s+", " ", " ".join(lines)).strip()


def label_request(transcript: str, direction: str, version: str) -> dict:
    """라벨 호출 인자(system·messages·schema) — 일반 호출과 Batch 요청이 같이 쓴다"""
    return {
        "system": prompts.load("classify", version)["labels"],
        "messages": [{"role": "user", "content": user_message(transcript, direction)}],
        "schema": LABEL_SCHEMA,
    }


def summary_request(transcript: str, direction: str, version: str) -> dict:
    """머리말 todo_kinds 버전은 할 일 종류 스키마"""
    return {
        "system": prompts.load("classify", version)["summary"],
        "messages": [{"role": "user", "content": user_message(transcript, direction)}],
        "schema": SUMMARY_SCHEMA_KINDS if prompts.summary_kinds(version) else SUMMARY_SCHEMA,
    }


def judge_request(transcript: str, direction: str, version: str) -> dict:
    """프롬프트 머리말에 effort가 있으면 effort와 늘린 max_tokens도 넣는다"""
    req = {
        "system": prompts.load("judge", version)["system"],
        "messages": [{"role": "user", "content": user_message(transcript, direction)}],
        "schema": LABEL_SCHEMA,
    }
    effort = prompts.effort("judge", version)
    if effort is not None:
        req |= {"effort": effort, "max_tokens": REASONING_MAX_TOKENS}
    return req


ENUMS = {"primary_purpose": PURPOSES, "counterpart": COUNTERPARTS, "urgency": URGENCIES}


def valid_labels(data) -> bool:
    """9항목이 정확히 있고 enum 값은 목록 안, 플래그는 참/거짓(0/1)"""
    if not isinstance(data, dict) or set(data) != set(ITEMS):
        return False
    return all(data[k] in v for k, v in ENUMS.items()) and all(
        type(data[f]) in (bool, int) and data[f] in (0, 1) for f in FLAGS
    )


def judgment_from(call: LLMResult) -> Judgment:
    ok = call.ok and valid_labels(call.data)
    return Judgment(ok, call.data if ok else None, call)


def classification_from(label: LLMResult, summary_call: LLMResult) -> Classification:
    if not (label.ok and summary_call.ok and valid_labels(label.data)):
        return Classification(False, None, None, None, label, summary_call)
    s = summary_call.data
    return Classification(
        True, label.data, normalize_summary(s["summary"]), list(s["todo"]), label, summary_call
    )


def classify(gw, transcript: str, direction: str, version: str) -> Classification:
    label = gw.complete(
        "classify",
        task="classify",
        max_tokens=MAX_TOKENS,
        prompt_version=version,
        **label_request(transcript, direction, version),
    )
    summary_call = gw.complete(
        "summarize",
        task="summarize",
        max_tokens=MAX_TOKENS,
        prompt_version=version,
        **summary_request(transcript, direction, version),
    )
    return classification_from(label, summary_call)


def judge(gw, transcript: str, direction: str, version: str) -> Judgment:
    call = gw.complete(
        "judge",
        task="judge",
        prompt_version=version,
        **{"max_tokens": MAX_TOKENS, **judge_request(transcript, direction, version)},
    )
    return judgment_from(call)


def _labels_json(labels: dict | None) -> str | None:
    if labels is None:
        return None
    return json.dumps(
        {k: int(v) if k in FLAGS else v for k, v in labels.items()}, ensure_ascii=False
    )


def save_classification(conn, call_uid: str, version: str, c: Classification) -> None:
    conn.execute(
        "INSERT INTO ax_call_classifications (call_uid, model_alias, model_actual, prompt_version,"
        " status, labels, summary, todo, llm_call_id, summary_model_actual, summary_llm_call_id)"
        " VALUES (?, 'classify', ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT (call_uid, model_alias, prompt_version) DO UPDATE SET"
        " model_actual = excluded.model_actual, status = excluded.status,"
        " labels = excluded.labels, summary = excluded.summary, todo = excluded.todo,"
        " llm_call_id = excluded.llm_call_id,"
        " summary_model_actual = excluded.summary_model_actual,"
        " summary_llm_call_id = excluded.summary_llm_call_id",
        (
            call_uid,
            c.label.model,
            version,
            "ok" if c.ok else "failed",
            _labels_json(c.labels),
            c.summary,
            None if c.todo is None else json.dumps(c.todo, ensure_ascii=False),
            c.label.log_id,
            c.summary_call.model,
            c.summary_call.log_id,
        ),
    )
    conn.commit()


def save_judgment(conn, call_uid: str, version: str, j: Judgment) -> None:
    conn.execute(
        "INSERT INTO ax_model_judgments (call_uid, model_alias, model_actual, prompt_version,"
        " status, labels, llm_call_id) VALUES (?, 'judge', ?, ?, ?, ?, ?)"
        " ON CONFLICT (call_uid, model_alias, prompt_version) DO UPDATE SET"
        " model_actual = excluded.model_actual, status = excluded.status,"
        " labels = excluded.labels, llm_call_id = excluded.llm_call_id",
        (
            call_uid,
            j.call.model,
            version,
            "ok" if j.ok else "failed",
            _labels_json(j.labels),
            j.call.log_id,
        ),
    )
    conn.commit()
