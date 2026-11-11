"""생성 실행 — 스키마 적용, truth_gen_meta, 단계 순서대로 실행, 다 쓴 뒤 교체, 정답 JSON. 단계는 가상 데이터 생성 설계 §5.1.
LLM 단계(7·9·9b)는 캐시만 읽는다 — 빈 건이 있으면 끝까지 돌아 작업을 내보내고 DB를 바꾸지 않은 채 LLMMissing(generator/llm_jobs.py)."""

import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from generator import (
    alerts,
    calls,
    cancels,
    chart,
    clinic,
    demand,
    documents,
    emergency,
    finance,
    hard_calls,
    messages,
    patients,
    pre_window,
    records,
    schedule,
    transcripts,
)
from generator.llm_jobs import LLMContext
from generator.master import (
    HARD_CALL_TYPES,
    HARD_CALLS_PER_TYPE,
    LEDGER_DEBT_CALLS,
    LEDGER_ERROR_KINDS,
    LINK_FAILURE_DAYS,
    NON_CLINICAL_MULTI_COUNT,
    RECON_MISMATCH_COUNTS,
    SETTLEMENT_ERROR_COUNTS,
    UNRECORDED_SETTLEMENT_COUNT,
)
from generator.paths import ROOT, output_paths
from generator.seeding import stage_faker, stage_rng

SOURCE_SCHEMA = ROOT / "shared" / "source_schema.sql"
TRUTH_SCHEMA = ROOT / "ground_truth" / "schema.sql"


@dataclass(frozen=True)
class Run:
    conn: sqlite3.Connection  # source.sqlite. truth.sqlite는 `truth` 스키마로 ATTACH
    cfg: dict
    seed: int
    range_start: date  # 생성 기간 (config range)
    range_end: date
    today: date
    holidays: frozenset  # 공휴일 (config holidays 파일)
    state: dict = field(
        default_factory=dict
    )  # 단계 사이에 넘기는 메모리 상태 (DB에 없는 것 — 리콜 기한 등)


# (단계 이름, 함수(run, rng, fake)). 이름이 난수 생성기·Faker 시드를 정한다 — 바꾸면 그 단계 결과가 바뀐다
STAGES: list = [
    ("master", clinic.stage),
    ("patients", patients.stage),
    ("demand", demand.stage),
    ("pre_window", pre_window.stage),
    ("appointments", schedule.stage),
    ("alerts", alerts.stage),
    ("treatments", records.stage_treatments),
    ("payments", records.stage_payments),
    ("claims", records.stage_claims),
    ("chart_facts", chart.stage_facts),
    ("chart_notes", chart.stage_notes),
    ("call_slots", calls.stage_slots),
    ("call_labels", calls.stage_labels),
    ("call_pools", calls.stage_pools),
    ("emergency_visits", emergency.stage),
    ("call_transcripts", transcripts.stage),
    ("messages", messages.stage),
    ("cancel_replies", cancels.stage),
    ("settlement_errors", finance.stage_settlement_errors),
    ("fin_income", finance.stage_income),
    ("fin_expenses", finance.stage_expenses),
    ("doc_plan", documents.stage_plan),
    ("documents", documents.stage_bodies),
    ("chart_narratives", chart.stage_narratives),
    ("master_values", clinic.stage_master_values),
    # 어려운 통화 hc- — 맨 끝(앞 단계 결과·캐시 그대로, 어려운 통화 설계 §3.1), 녹취는 LLM gen_hard(§3.5)
    ("hard_calls", hard_calls.stage_slots),
    ("hard_transcripts", hard_calls.stage_transcripts),
]


class LLMMissing(RuntimeError):
    """LLM 캐시에 없는 건이 있다 — 작업 파일을 썼고 DB는 그대로. exhausted = 시도를 다 쓴 건(작업으로 내보내지 않음)"""

    def __init__(self, missing: dict[str, int], exhausted: dict[str, list[str]] | None = None):
        self.missing, self.exhausted = missing, exhausted or {}
        super().__init__(
            ", ".join(
                [f"{task} {n}건" for task, n in missing.items()]
                + [f"{task} 시도 소진 {len(items)}건" for task, items in self.exhausted.items()]
            )
        )


def new_run(conn: sqlite3.Connection, cfg: dict) -> Run:
    holidays = json.loads((ROOT / cfg["holidays"]).read_text(encoding="utf-8"))
    return Run(
        conn=conn,
        cfg=cfg,
        seed=cfg["seed"],
        range_start=cfg["range"]["start"],
        range_end=cfg["range"]["end"],
        today=cfg["today"],
        holidays=frozenset(date.fromisoformat(d) for d in holidays),
    )


def generate(
    source_path: Path,
    truth_path: Path,
    config_path: Path,
    fake_llm: bool = False,
    llm_paths: dict | None = None,
    rag_qa_path: Path | None = None,
    hard_labels_path: Path | None = None,
) -> Run:
    """임시 파일에 전부 쓴 뒤 교체 — 단계 실행 중에 실패하면 기존 DB는 그대로. 단계 상태(run.state)를 보려고 Run을 돌려준다.
    fake_llm: LLM 단계를 가짜 게이트웨이로(테스트). llm_paths: 캐시·작업 폴더(기본 generator/paths.py).
    rag_qa_path: 사람이 쓴 RAG 질문 JSON — 있으면 truth_rag_qa에 적재(make data만 준다. 테스트 생성은 문서·전사가 달라 넣지 않음)
    hard_labels_path: 어려운 통화 최종 라벨 JSON(evaluation/hard_labels.py finalize) — 있으면 hc- 생성 라벨을 덮어쓴다"""
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    tmp_src, tmp_truth = (p.with_name(p.name + ".tmp") for p in (source_path, truth_path))
    for p in (tmp_src, tmp_truth):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.unlink(missing_ok=True)
    # 스키마 파일의 DDL은 스키마 이름이 없어 truth는 따로 연결해 만든 뒤 ATTACH
    truth = sqlite3.connect(tmp_truth)
    truth.executescript(TRUTH_SCHEMA.read_text(encoding="utf-8"))
    truth.close()
    conn = sqlite3.connect(tmp_src)
    try:
        conn.executescript(SOURCE_SCHEMA.read_text(encoding="utf-8"))
        conn.execute("ATTACH DATABASE ? AS truth", (str(tmp_truth),))
        run = new_run(conn, cfg)
        run.state["llm"] = LLMContext(llm_paths or output_paths(), fake=fake_llm)
        # 생성 시각은 넣지 않는다 (같은 시드 2회 해시 비교)
        conn.executemany(
            "INSERT INTO truth.truth_gen_meta VALUES (?, ?)",
            [
                ("seed", str(run.seed)),
                ("today", run.today.isoformat()),
                ("range_start", run.range_start.isoformat()),
                ("range_end", run.range_end.isoformat()),
                ("call_slots", str(cfg["calls_with_transcript"])),
                ("eval_pool", str(cfg["eval_pool_size"])),
                ("blind_pool", str(cfg["blind_pool_size"])),
                ("hard_calls", str(len(HARD_CALL_TYPES) * HARD_CALLS_PER_TYPE)),
                ("documents_core", str(cfg["documents"]["core"])),
                ("documents_distractor", str(cfg["documents"]["distractor"])),
                ("link_failure_days", str(LINK_FAILURE_DAYS)),
                (
                    "ledger_errors",
                    json.dumps({k: RECON_MISMATCH_COUNTS[k] for k in LEDGER_ERROR_KINDS}),
                ),
                ("recon_mismatches", json.dumps(RECON_MISMATCH_COUNTS)),
                ("ledger_debt_calls", str(LEDGER_DEBT_CALLS)),
                ("unrecorded_settlements", str(UNRECORDED_SETTLEMENT_COUNT)),
                ("non_clinical_multi", str(NON_CLINICAL_MULTI_COUNT)),
                ("settlement_errors", json.dumps(SETTLEMENT_ERROR_COUNTS)),
            ],
        )
        for name, stage in STAGES:
            stage(run, stage_rng(run.seed, name), stage_faker(run.seed, name))
        if hard_labels_path and hard_labels_path.exists():
            apply_hard_labels(conn, json.loads(hard_labels_path.read_text(encoding="utf-8")))
        if rag_qa_path and rag_qa_path.exists():
            insert_rag_qa(
                conn, json.loads(rag_qa_path.read_text(encoding="utf-8")), schema="truth"
            )
        missing, exhausted = run.state["llm"].finish(), dict(run.state["llm"].exhausted)
        if not missing and not exhausted:
            conn.commit()
    finally:
        conn.close()
    if missing or exhausted:
        for p in (tmp_src, tmp_truth):
            p.unlink(missing_ok=True)
        raise LLMMissing(missing, exhausted)
    os.replace(tmp_src, source_path)
    os.replace(tmp_truth, truth_path)
    write_truth_json(truth_path)
    return run


HARD_LABEL_COLS = (
    "primary_purpose",
    "accepted_purposes",
    "counterpart",
    "urgency",
    "emergency",
    "complaint",
    "price_inquiry",
    "wants_appt",
    "appt_confirmed",
    "has_todo",
)


def apply_hard_labels(conn: sqlite3.Connection, rows: list[dict]) -> None:
    """hard_call_labels.json 행 → hc- 생성 라벨을 최종 라벨로, truth_hard_calls.label_source(어려운 통화 설계 §4 저장).
    DB에 없는 call_uid가 있으면 옛 파일이라 멈춘다"""
    have = {r[0] for r in conn.execute("SELECT call_uid FROM truth.truth_hard_calls")}
    if stale := sorted({r["call_uid"] for r in rows} - have):
        raise ValueError(f"최종 라벨의 hc- 통화가 DB에 없음(옛 파일): {stale[:5]}")
    for r in rows:
        labels = r["labels"]
        conn.execute(
            f"UPDATE truth.truth_call_labels SET {', '.join(f'{c} = ?' for c in HARD_LABEL_COLS)}"
            " WHERE call_uid = ?",
            [
                json.dumps(labels[c]) if c == "accepted_purposes" else labels[c]
                for c in HARD_LABEL_COLS
            ]
            + [r["call_uid"]],
        )
        conn.execute(
            "UPDATE truth.truth_hard_calls SET label_source = ? WHERE call_uid = ?",
            (r["label_source"], r["call_uid"]),
        )


RAG_QA_JSON_COLS = ("answer_doc_ids", "expected_tools", "expected_args")


def insert_rag_qa(conn: sqlite3.Connection, qa: list[dict], schema: str = "main") -> None:
    """rag_qa.json 행 → truth_rag_qa. 목록·객체 칸은 JSON 문자열"""
    cols = (
        "id",
        "type",
        "question",
        "answer_text",
        "answer_doc_ids",
        "answer_chunk_hint",
        "expected_tools",
        "expected_args",
        "expect_refusal",
        "split",
        "answer_sql",
    )
    conn.executemany(
        f"INSERT INTO {schema}.truth_rag_qa ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [
            tuple(
                json.dumps(q[c], ensure_ascii=False)
                if c in RAG_QA_JSON_COLS
                else int(q[c])
                if c == "expect_refusal"
                else q.get(c)
                for c in cols
            )
            for q in qa
        ],
    )


# 정답 JSON (평가 설계 §6 — git diff·리뷰용, truth.sqlite 옆). 파일 → (테이블, 정렬 키, JSON 문자열 컬럼)
TRUTH_JSON = {
    "call_labels.json": ("truth_call_labels", "call_uid", ("accepted_purposes", "used_fact")),
    "doc_meta.json": ("truth_doc_meta", "doc_id", ("avoid_doc_ids", "planted_facts")),
    "hard_calls.json": ("truth_hard_calls", "call_uid", ()),
}


def write_truth_json(truth_path: Path) -> None:
    conn = sqlite3.connect(truth_path)
    conn.row_factory = sqlite3.Row
    try:
        for name, (table, key, json_cols) in TRUTH_JSON.items():
            rows = [
                {k: json.loads(r[k]) if k in json_cols else r[k] for k in r.keys()}
                for r in conn.execute(f"SELECT * FROM {table} ORDER BY {key}")
            ]
            (truth_path.parent / name).write_text(
                json.dumps(rows, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
            )
    finally:
        conn.close()
