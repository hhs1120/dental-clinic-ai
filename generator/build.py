"""생성 실행 — 스키마 적용, truth_gen_meta, 단계 순서대로 실행, 다 쓴 뒤 교체. 단계는 가상 데이터 생성 설계 §5.1."""
import json
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from generator import alerts, calls, chart, clinic, demand, patients, pre_window, records, schedule
from generator.paths import ROOT
from generator.seeding import stage_faker, stage_rng

APP_SCHEMA = ROOT / "shared" / "schema.sql"
TRUTH_SCHEMA = ROOT / "ground_truth" / "schema.sql"


@dataclass(frozen=True)
class Run:
    conn: sqlite3.Connection  # app.sqlite. truth.sqlite는 `truth` 스키마로 ATTACH
    cfg: dict
    seed: int
    range_start: date  # 생성 기간 (config range)
    range_end: date
    today: date
    holidays: frozenset  # 공휴일 (config holidays 파일)
    state: dict = field(default_factory=dict)  # 단계 사이에 넘기는 메모리 상태 (DB에 없는 것 — 리콜 기한 등)


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
]


def new_run(conn: sqlite3.Connection, cfg: dict) -> Run:
    holidays = json.loads((ROOT / cfg["holidays"]).read_text(encoding="utf-8"))
    return Run(conn=conn, cfg=cfg, seed=cfg["seed"], range_start=cfg["range"]["start"],
               range_end=cfg["range"]["end"], today=cfg["today"],
               holidays=frozenset(date.fromisoformat(d) for d in holidays))


def generate(app_path: Path, truth_path: Path, config_path: Path) -> Run:
    """임시 파일에 전부 쓴 뒤 교체 — 단계 실행 중에 실패하면 기존 DB는 그대로. 단계 상태(run.state)를 보려고 Run을 돌려준다"""
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    tmp_app, tmp_truth = (p.with_name(p.name + ".tmp") for p in (app_path, truth_path))
    for p in (tmp_app, tmp_truth):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.unlink(missing_ok=True)
    # 스키마 파일의 DDL은 스키마 이름이 없어 truth는 따로 연결해 만든 뒤 ATTACH
    truth = sqlite3.connect(tmp_truth)
    truth.executescript(TRUTH_SCHEMA.read_text(encoding="utf-8"))
    truth.close()
    conn = sqlite3.connect(tmp_app)
    try:
        conn.executescript(APP_SCHEMA.read_text(encoding="utf-8"))
        conn.execute("ATTACH DATABASE ? AS truth", (str(tmp_truth),))
        run = new_run(conn, cfg)
        # 생성 시각은 넣지 않는다 (같은 시드 2회 해시 비교)
        conn.executemany("INSERT INTO truth.truth_gen_meta VALUES (?, ?)", [
            ("seed", str(run.seed)),
            ("range_start", run.range_start.isoformat()), ("range_end", run.range_end.isoformat()),
            ("call_slots", str(cfg["calls_with_transcript"])), ("eval_pool", str(cfg["eval_pool_size"])),
            ("blind_pool", str(cfg["blind_pool_size"])),
        ])
        for name, stage in STAGES:
            stage(run, stage_rng(run.seed, name), stage_faker(run.seed, name))
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp_app, app_path)
    os.replace(tmp_truth, truth_path)
    return run
