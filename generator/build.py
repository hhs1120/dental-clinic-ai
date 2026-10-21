"""생성 실행 — 스키마 적용, truth_gen_meta, 단계 순서대로 실행, 다 쓴 뒤 교체. 단계는 가상 데이터 생성 설계 §5.1."""
import os
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from generator.paths import ROOT
from generator.seeding import stage_rng

APP_SCHEMA = ROOT / "shared" / "schema.sql"
TRUTH_SCHEMA = ROOT / "ground_truth" / "schema.sql"


@dataclass(frozen=True)
class Run:
    conn: sqlite3.Connection  # app.sqlite. truth.sqlite는 `truth` 스키마로 ATTACH
    cfg: dict
    seed: int
    scale: str
    range_start: date  # 규모별 시작일 (config scale_presets)
    range_end: date


# (단계 이름, 함수(run, rng)). 이름이 난수 생성기를 정한다 — 바꾸면 그 단계 결과가 바뀐다
STAGES: list = []


def generate(scale: str, app_path: Path, truth_path: Path, config_path: Path) -> None:
    """임시 파일에 전부 쓴 뒤 교체 — 중간에 실패하면 기존 DB는 그대로."""
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
        run = Run(conn=conn, cfg=cfg, seed=cfg["seed"], scale=scale,
                  range_start=cfg["scale_presets"][scale]["start"], range_end=cfg["range"]["end"])
        # 생성 시각은 넣지 않는다 (같은 시드 2회 해시 비교)
        conn.executemany("INSERT INTO truth.truth_gen_meta VALUES (?, ?)", [
            ("seed", str(run.seed)), ("scale", scale),
            ("range_start", run.range_start.isoformat()), ("range_end", run.range_end.isoformat()),
        ])
        for name, stage in STAGES:
            stage(run, stage_rng(run.seed, name))
        conn.commit()
    finally:
        conn.close()
    os.replace(tmp_app, app_path)
    os.replace(tmp_truth, truth_path)
