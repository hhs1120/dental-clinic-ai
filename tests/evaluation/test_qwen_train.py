"""Qwen 학습 이어 돌기 (evaluation/finetune/qwen/train.py) — 멈추면 마지막 체크포인트부터, 다음 에폭은 끝난 어댑터부터."""

import json

import pytest

from evaluation.finetune.qwen import train

ROWS = 1000


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    (tmp_path / "data" / "full").mkdir(parents=True)
    (tmp_path / "data" / "full" / "train.jsonl").write_text("{}\n" * ROWS)
    monkeypatch.setattr(train, "DATA", tmp_path / "data")
    monkeypatch.setattr(train, "ADAPTERS", tmp_path / "adapters")
    monkeypatch.setattr(train, "OUT", tmp_path / "out")
    monkeypatch.setattr(train, "config", lambda: {"batch_size": 1, "seed": 0})
    return tmp_path


class Fake:
    """부를 때마다 fail_at[i]가 있으면 그 단계까지 체크포인트를 남기고 code로 실패(기본 메모리 부족), 없으면 끝까지"""

    def __init__(self, *fail_at, code=None):
        self.fail_at = list(fail_at)
        self.code = train.OOM_EXIT if code is None else code
        self.calls = []

    def __call__(self, name, part_dir, iters, offset, seed, resume, overrides):
        self.calls.append({"iters": iters, "offset": offset, "seed": seed, "resume": resume})
        part_dir.mkdir(parents=True, exist_ok=True)
        stop = self.fail_at.pop(0) if self.fail_at else None
        for it in range(100, (stop or iters) + 1, 100):
            (part_dir / f"{it:07d}_adapters.safetensors").write_text(str(offset + it))
        if stop is not None:
            return self.code
        (part_dir / "adapters.safetensors").write_text("final")
        (part_dir / "adapter_config.json").write_text("{}")
        return 0


def _progress(d):
    return json.loads((d / "adapters" / "full" / "progress.json").read_text())


def test_fresh_success(dirs):
    fake = Fake()
    out = train.train("full", epochs=1, runner=fake)
    assert fake.calls == [{"iters": ROWS, "offset": 0, "seed": 0, "resume": None}]
    assert (out / "adapters.safetensors").read_text() == "final"
    assert (out / "adapter_config.json").exists()
    assert _progress(dirs)["done"] == ROWS == _progress(dirs)["total"]


def test_resume_from_checkpoint(dirs):
    fake = Fake(250)
    train.train("full", epochs=1, runner=fake)
    second = fake.calls[1]
    assert second["offset"] == 200 and second["iters"] == ROWS - 200
    assert second["resume"].endswith("part0/0000200_adapters.safetensors")
    assert second["seed"] != fake.calls[0]["seed"]  # 데이터 순서를 다시 섞는다
    assert _progress(dirs)["done"] == ROWS


def test_retry_without_checkpoint(dirs):
    fake = Fake(50)
    train.train("full", epochs=1, runner=fake)
    assert fake.calls[1]["offset"] == 0 and fake.calls[1]["resume"] is None


def test_retries_exhausted(dirs):
    fake = Fake(*[150] * (train.MAX_RETRIES + 1))
    with pytest.raises(SystemExit):
        train.train("full", epochs=1, runner=fake)
    assert len(fake.calls) == train.MAX_RETRIES + 1
    assert _progress(dirs)["done"] == 100 * (train.MAX_RETRIES + 1)


def test_continue_next_epoch(dirs):
    train.train("full", epochs=1, runner=Fake())
    fake = Fake()
    train.train("full", epochs=1, cont=True, runner=fake)
    call = fake.calls[0]
    assert call["offset"] == ROWS and call["iters"] == ROWS
    assert call["resume"].endswith("full/adapters.safetensors")
    assert _progress(dirs)["total"] == 2 * ROWS == _progress(dirs)["done"]


def test_fresh_refuses_existing(dirs):
    train.train("full", epochs=1, runner=Fake())
    with pytest.raises(SystemExit):
        train.train("full", epochs=1, runner=Fake())


def test_continue_needs_finished(dirs):
    with pytest.raises(SystemExit):
        train.train("full", epochs=1, cont=True, runner=Fake())


def _log(d, lines):
    (d / "out").mkdir(exist_ok=True)
    (d / "out" / "train_full.log").write_text("".join(json.dumps(x) + "\n" for x in lines))


def test_status(dirs):
    (dirs / "adapters" / "full").mkdir(parents=True)
    (dirs / "adapters" / "full" / "progress.json").write_text(
        json.dumps({"name": "full", "total": 1000, "done": 0, "parts": [{"dir": "part0"}]})
    )
    t0 = 1_000_000.0
    _log(dirs, [
        {"kind": "start", "part": "part0", "time": t0},
        {"kind": "val", "part": "part0", "iteration": 0, "val_loss": 0.5, "time": t0 + 10},
        {"kind": "train", "part": "part0", "iteration": 200, "train_loss": 0.3,
         "iterations_per_second": 0.1, "peak_memory": 11.5, "time": t0 + 2000},
        {"kind": "train", "part": "part0", "iteration": 400, "train_loss": 0.06,
         "iterations_per_second": 0.2, "peak_memory": 12.08, "time": t0 + 3000},
    ])  # fmt: skip
    text = train.status("full", now=t0 + 3600)
    assert "400 / 1,000" in text and "40.0%" in text
    assert "경과 1시간 0분" in text
    assert (
        "남은 예상 0시간 50분" in text
    )  # 최근 보고 사이 실제 속도 200단계/1,000초 → 남은 600단계
    assert "0.060" in text and "heldout 0.500" in text and "12.08GB" in text and "part0" in text


def test_status_done_and_missing(dirs):
    assert "학습 기록 없음" in train.status("full")
    (dirs / "adapters" / "full").mkdir(parents=True)
    (dirs / "adapters" / "full" / "progress.json").write_text(
        json.dumps({"name": "full", "total": 10, "done": 10, "parts": [{"dir": "part0"}]})
    )
    _log(dirs, [{"kind": "start", "part": "part0", "time": 0.0},
                {"kind": "end", "part": "part0", "time": 3600.0}])  # fmt: skip
    text = train.status("full", now=99999.0)
    assert "끝남" in text and "경과 1시간 0분" in text


def test_killed_by_signal_retries(dirs):
    fake = Fake(150, code=-9)
    train.train("full", epochs=1, runner=fake)
    assert len(fake.calls) == 2 and fake.calls[1]["offset"] == 100


def test_other_error_stops_at_once(dirs):
    fake = Fake(0, code=1)  # 설정 오류 등 — 다시 돌아도 같은 오류
    with pytest.raises(SystemExit, match="다시 시도하지 않는다"):
        train.train("full", epochs=1, runner=fake)
    assert len(fake.calls) == 1


@pytest.mark.parametrize(
    "raw, value",
    [("1e-4", 1e-4), ("2.0e-5", 2e-5), ("100", 100), ("true", True), ("abc", "abc")],
)
def test_parse_override(raw, value):
    out = train.parse_override(raw)
    assert out == value and type(out) is type(value)


def test_status_waiting_first_report(dirs):
    (dirs / "adapters" / "full").mkdir(parents=True)
    (dirs / "adapters" / "full" / "progress.json").write_text(
        json.dumps({"name": "full", "total": 1000, "done": 0, "parts": [{"dir": "part0"}]})
    )
    _log(dirs, [{"kind": "start", "part": "part0", "time": 0.0}])
    assert "모델 불러오는 중·첫 기록 대기" in train.status("full", now=60.0)


def test_resume_after_parent_died(dirs):
    """부모까지 죽어 progress.json에 끝나지 않은 part가 남으면 resume이 마지막 체크포인트부터 잇는다"""

    class Die(Fake):
        def __call__(self, *a):
            super().__call__(*a)
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        train.train("full", epochs=1, runner=Die(250))
    fake = Fake()
    train.train("full", resume=True, runner=fake)
    assert fake.calls[0]["offset"] == 200 and fake.calls[0]["iters"] == ROWS - 200
    assert fake.calls[0]["resume"].endswith("part0/0000200_adapters.safetensors")
    prog = _progress(dirs)
    assert prog["done"] == ROWS and prog["parts"][0]["status"] == "interrupted"


def test_resume_without_checkpoint_restarts_part(dirs):
    class Die(Fake):
        def __call__(self, *a):
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        train.train("full", epochs=1, runner=Die())
    fake = Fake()
    train.train("full", resume=True, runner=fake)
    assert fake.calls[0]["offset"] == 0 and fake.calls[0]["resume"] is None


def test_resume_refuses_finished_or_missing(dirs):
    with pytest.raises(SystemExit):
        train.train("full", resume=True, runner=Fake())
    train.train("full", epochs=1, runner=Fake())
    with pytest.raises(SystemExit, match="끝났다"):
        train.train("full", resume=True, runner=Fake())


def test_fresh_train_clears_old_log(dirs):
    _log(dirs, [{"kind": "start", "part": "part0", "time": 0.0}])
    train.train("full", epochs=1, runner=Fake())
    assert not (dirs / "out" / "train_full.log").exists()  # Fake는 로그를 쓰지 않는다


def test_status_after_retry_uses_current_part(dirs):
    """재시도 뒤 새 part의 첫 보고 전에는 옛 part의 보고가 아니라 progress의 done을 진행으로 본다"""
    (dirs / "adapters" / "full").mkdir(parents=True)
    (dirs / "adapters" / "full" / "progress.json").write_text(json.dumps(
        {"name": "full", "total": 1000, "done": 200, "parts": [{"dir": "part0"}, {"dir": "part1"}]}
    ))  # fmt: skip
    _log(dirs, [
        {"kind": "start", "part": "part0", "time": 0.0},
        {"kind": "train", "part": "part0", "iteration": 240, "train_loss": 0.1,
         "iterations_per_second": 0.1, "peak_memory": 12.0, "time": 100.0},
        {"kind": "start", "part": "part1", "time": 200.0},
    ])  # fmt: skip
    assert "200 / 1,000" in train.status("full", now=300.0)
