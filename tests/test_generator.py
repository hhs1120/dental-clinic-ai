"""생성기 뼈대 — 단계별 시드, DB 쓰기, truth_gen_meta, 결정성 (가상 데이터 생성 설계 §5.1·§5.3)."""
import os
import sqlite3
import subprocess
import sys
import yaml

from generator import verify
from generator.build import generate
from generator.paths import ROOT
from generator.seeding import stage_faker, stage_rng

CONFIG = ROOT / "config" / "generator.yaml"
CFG = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))


def test_stage_rng_same_stage_same_draws():
    assert (stage_rng(1, "patients").integers(1 << 30, size=5) == stage_rng(1, "patients").integers(1 << 30, size=5)).all()


def test_stage_rng_differs_by_stage_and_seed():
    base = stage_rng(1, "patients").integers(1 << 30, size=5).tolist()
    assert stage_rng(1, "demand").integers(1 << 30, size=5).tolist() != base
    assert stage_rng(2, "patients").integers(1 << 30, size=5).tolist() != base


def test_stage_faker_same_stage_same_names():
    assert [stage_faker(1, "patients").name() for _ in range(3)] == [stage_faker(1, "patients").name() for _ in range(3)]


def test_stage_faker_differs_by_stage():
    patients, staff = stage_faker(1, "patients"), stage_faker(1, "staff")
    assert [patients.name() for _ in range(5)] != [staff.name() for _ in range(5)]


def _generate(tmp_path, scale):
    app, truth = tmp_path / "app.sqlite", tmp_path / "truth.sqlite"
    generate(scale, app, truth, CONFIG)
    return app, truth


def test_gen_meta_records_seed_scale_and_range(tmp_path):
    for scale in ("small", "large"):
        _, truth = _generate(tmp_path / scale, scale)
        meta = dict(sqlite3.connect(truth).execute("SELECT key, value FROM truth_gen_meta"))
        assert meta == {
            "seed": str(CFG["seed"]),
            "scale": scale,
            "range_start": CFG["scale_presets"][scale]["start"].isoformat(),
            "range_end": CFG["range"]["end"].isoformat(),
        }


def test_verify_load_ctx_reads_generated_meta(tmp_path):
    app, truth = _generate(tmp_path, "small")
    conn = sqlite3.connect(app)
    conn.execute("ATTACH DATABASE ? AS truth", (str(truth),))
    ctx = verify.load_ctx(CONFIG, conn)
    assert ctx.range_start == CFG["scale_presets"]["small"]["start"]
    assert ctx.range_end == CFG["range"]["end"]


def test_schemas_applied_to_both_dbs(tmp_path):
    app, truth = _generate(tmp_path, "small")
    app_tables = {r[0] for r in sqlite3.connect(app).execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    truth_tables = {r[0] for r in sqlite3.connect(truth).execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"emr_patients", "emr_appointments", "ax_calls"} <= app_tables
    assert {"truth_gen_meta", "truth_claims"} <= truth_tables
    assert not any(t.startswith("truth_") for t in app_tables)  # 정답은 app.sqlite에 넣지 않는다


def test_regenerate_replaces_existing_db(tmp_path):
    app, truth = tmp_path / "app.sqlite", tmp_path / "truth.sqlite"
    for path in (app, truth):
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE leftover (x)")
        conn.commit()
        conn.close()
    generate("small", app, truth, CONFIG)
    for path in (app, truth):
        assert "leftover" not in {r[0] for r in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")}
    assert not list(tmp_path.glob("*.tmp"))


def _run_cli(out, hashseed):
    out.mkdir()
    env = {**os.environ, "PYTHONHASHSEED": str(hashseed)}
    subprocess.run([sys.executable, "-m", "generator", "--scale", "small",
                    "--db", str(out / "app.sqlite"), "--truth", str(out / "truth.sqlite")],
                   cwd=ROOT, env=env, check=True)
    conn = sqlite3.connect(out / "app.sqlite")
    conn.execute("ATTACH DATABASE ? AS truth", (str(out / "truth.sqlite"),))
    return verify.canonical_hash(conn), verify.canonical_hash(conn, "truth")


def test_same_seed_twice_same_hash_across_hashseeds(tmp_path):
    # PYTHONHASHSEED를 다르게 — 같으면 set 순회 순서에 기댄 버그가 가려진다 (verify docstring)
    assert _run_cli(tmp_path / "a", 0) == _run_cli(tmp_path / "b", 1)


def test_db_and_truth_must_be_given_together(tmp_path):
    # 하나만 주면 나머지가 기본 경로(large)로 떨어져 실제 DB를 덮어쓴다
    for flag in ("--db", "--truth"):
        r = subprocess.run([sys.executable, "-m", "generator", "--scale", "small", flag, str(tmp_path / "x.sqlite")],
                           cwd=ROOT, capture_output=True)
        assert r.returncode != 0
    assert not list(tmp_path.iterdir())
