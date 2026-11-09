"""Qwen LoRA 학습 — mlx-lm lora를 lora.yaml로 돌린다(분류기 설계 "Qwen 비교").
데이터 data/<name>/{train,valid}.jsonl(data.py), 어댑터 adapters/<name>/(커밋 안 함, 진행 progress.json), 로그 out/train_<name>.log(JSON 줄).
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import yaml

from evaluation.finetune.qwen.data import DATA, HERE

CONFIG = HERE / "lora.yaml"
ADAPTERS = HERE / "adapters"
OUT = HERE / "out"


def config() -> dict:
    return yaml.safe_load(CONFIG.read_text())


def iters_for(rows: int, epochs: int, batch: int) -> int:
    return -(-rows * epochs // batch)


MAX_RETRIES = 3  # 메모리 부족·외부 종료로 멈추면 마지막 체크포인트부터 다시 도는 횟수
OOM_EXIT = 3  # 자식 프로세스가 MLX 메모리 부족을 잡으면 이 코드로 끝난다 — 그 밖의 오류는 다시 시도하지 않는다


def parse_override(raw: str):
    """명령의 키=값 — 숫자를 먼저(YAML 1.1은 '1e-4'를 문자열로 읽는다), 아니면 YAML"""
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            pass
    return yaml.safe_load(raw)


def latest_checkpoint(part_dir: Path) -> tuple[int, Path] | None:
    """mlx-lm이 save_every마다 남긴 0000100_adapters.safetensors 중 가장 늦은 것"""
    found = [(int(p.name.split("_")[0]), p) for p in part_dir.glob("*_adapters.safetensors")]
    return max(found) if found else None


def train(name: str, epochs: int = 1, cont: bool = False, overrides: dict | None = None,
          runner=None, resume: bool = False) -> Path:  # fmt: skip
    """→ adapters/<name>/(adapters.safetensors·adapter_config.json). 진행은 adapters/<name>/progress.json.
    cont면 끝난 어댑터에서 epochs만큼 더(에폭 경계에서 끊어 이어 가기). 한 번의 학습은 part<k>/ 폴더 하나 —
    멈추면 그 part의 마지막 체크포인트에서 남은 단계만 새 part로 다시 돈다(MAX_RETRIES번까지).
    resume이면 부모까지 죽어(재부팅·터미널 종료) 끝나지 않은 채 남은 학습을 마지막 part의 체크포인트부터 잇는다.
    이어 가면 가중치만 이어지고 옵티마이저 상태·데이터 순서는 새로 시작한다(시드는 part마다 바꾼다)"""
    runner = runner or _subprocess_runner
    root = ADAPTERS / name
    path = root / "progress.json"
    if resume:
        if not path.exists():
            raise SystemExit(f"{root}에 이어 갈 학습이 없다")
        prog = json.loads(path.read_text())
        if prog["done"] == prog["total"]:
            raise SystemExit(f"{name} 학습은 끝났다 — 더 하려면 continue")
        part = prog["parts"][-1]
        part.setdefault("status", "interrupted")
        last = latest_checkpoint(root / part["dir"])
        prog["done"], resume_from = (
            (part["offset"] + last[0], str(last[1])) if last else (part["offset"], part["resume"])
        )
        return _run(name, root, prog, resume_from, runner)
    rows = len((DATA / name / "train.jsonl").read_text().splitlines())
    add = iters_for(rows, epochs, config()["batch_size"])
    if cont:
        if not path.exists() or not (root / "adapters.safetensors").exists():
            raise SystemExit(f"{root}에 끝난 학습이 없다 — continue 없이 먼저 학습")
        prog = json.loads(path.read_text())
        if prog["done"] != prog["total"]:
            raise SystemExit(
                f"{name} 학습이 끝나지 않았다({prog['done']}/{prog['total']}) — resume"
            )
        prog["total"] += add
        prog["overrides"] = overrides or {}
        resume_from = str(root / "adapters.safetensors")
    else:
        if path.exists():
            raise SystemExit(
                f"{root}가 이미 있다 — 이어 가려면 continue·resume, 새로 하려면 폴더를 지운다"
            )
        prog = {"name": name, "rows": rows, "total": add, "done": 0, "parts": [],
                "overrides": overrides or {}}  # fmt: skip
        resume_from = None
        (OUT / f"train_{name}.log").unlink(
            missing_ok=True
        )  # 같은 이름 옛 학습 기록이 status에 섞이지 않게
    root.mkdir(parents=True, exist_ok=True)
    return _run(name, root, prog, resume_from, runner)


def _run(name: str, root: Path, prog: dict, resume: str | None, runner) -> Path:
    """남은 단계를 part로 돌린다 — 메모리 부족·신호 종료면 마지막 체크포인트부터 새 part로(MAX_RETRIES번까지)"""
    path = root / "progress.json"
    for attempt in range(MAX_RETRIES + 1):
        k = len(prog["parts"])
        part = {"dir": f"part{k}", "offset": prog["done"], "iters": prog["total"] - prog["done"],
                "seed": config()["seed"] + k, "resume": resume}  # fmt: skip
        prog["parts"].append(part)
        path.write_text(json.dumps(prog, ensure_ascii=False, indent=1))
        part_dir = root / part["dir"]
        code = runner(name, part_dir, part["iters"], part["offset"], part["seed"], resume,
                      prog.get("overrides", {}))  # fmt: skip
        if code == 0:
            part["status"] = "ok"
            prog["done"] = prog["total"]
            for f in ("adapters.safetensors", "adapter_config.json"):
                shutil.copy(part_dir / f, root / f)
            path.write_text(json.dumps(prog, ensure_ascii=False, indent=1))
            return root
        last = latest_checkpoint(part_dir)
        part["status"] = f"failed (exit {code})"
        if code != OOM_EXIT and code >= 0:  # 음수 = 자식만 신호로 종료(메모리 압박 강제 종료 등)
            path.write_text(json.dumps(prog, ensure_ascii=False, indent=1))
            raise SystemExit(
                f"part{k} 오류로 멈춤(exit {code}) — 다시 시도하지 않는다. 위 출력을 본다"
            )
        if last:
            prog["done"] = part["offset"] + last[0]
            resume = str(last[1])
        path.write_text(json.dumps(prog, ensure_ascii=False, indent=1))
        if attempt < MAX_RETRIES:
            print(f"part{k} 멈춤 — {prog['done']}/{prog['total']}부터 다시 ({attempt + 1}/{MAX_RETRIES})",
                  flush=True)  # fmt: skip
    raise SystemExit(f"{name}: {MAX_RETRIES}번 다시 돌아도 멈춤 — {prog['done']}/{prog['total']}")


def _subprocess_runner(name, part_dir, iters, offset, seed, resume, overrides) -> int:
    """메모리 부족 뒤 MLX 상태가 깨질 수 있어 part마다 새 프로세스"""
    spec = {"name": name, "part_dir": str(part_dir), "iters": iters, "offset": offset,
            "seed": seed, "resume": resume, "overrides": overrides}  # fmt: skip
    cmd = [sys.executable, "-m", "evaluation.finetune.qwen", "_part", json.dumps(spec)]
    return subprocess.run(cmd).returncode


def part_main(spec: str) -> int:
    """_part 자식 프로세스 본체 — 메모리 부족이면 OOM_EXIT, 그 밖의 예외는 그대로(종료 코드 1)"""
    try:
        run_part(**json.loads(spec))
    except RuntimeError as e:
        if "Insufficient Memory" in str(e) or "OutOfMemory" in str(e):
            print(f"메모리 부족: {e}", flush=True)
            return OOM_EXIT
        raise
    return 0


def run_part(name: str, part_dir: str, iters: int, offset: int, seed: int, resume: str | None,
             overrides: dict) -> None:  # fmt: skip
    """한 part를 같은 프로세스에서 mlx_lm.lora.run으로(화면 출력은 최대 메모리·it/s를 보이지 않는다).
    보고마다 out/train_<name>.log에 JSON 한 줄을 덧붙인다 — iteration은 처음부터 센 단계(offset + part 안 단계)"""
    import types

    from mlx_lm import lora
    from mlx_lm.tuner.callbacks import TrainingCallback

    args = lora.CONFIG_DEFAULTS | config() | overrides
    args |= {"train": True, "data": str(DATA / name), "adapter_path": part_dir, "iters": iters,
             "seed": seed, "resume_adapter_file": resume}  # fmt: skip
    OUT.mkdir(exist_ok=True)
    part = Path(part_dir).name
    with (OUT / f"train_{name}.log").open("a") as log:

        def write(kind: str, info: dict) -> None:
            if "iteration" in info:
                info = info | {"iteration": offset + info["iteration"]}
            row = {"kind": kind, "part": part, "time": time.time(), **info}
            log.write(json.dumps(row, ensure_ascii=False) + "\n")
            log.flush()

        class Log(TrainingCallback):
            def on_train_loss_report(self, info: dict) -> None:
                write("train", info)

            def on_val_loss_report(self, info: dict) -> None:
                write("val", info)

        write("start", {"offset": offset, "iters": iters, "seed": seed, "resume": resume,
                        "overrides": overrides})  # fmt: skip
        tic = time.perf_counter()
        # mlx-lm 0.32.2 lora.run은 넘긴 콜백을 get_reporting_callbacks 결과(report_to 없으면 None)로 덮는다
        callback = Log()
        lora.get_reporting_callbacks = lambda *a, **k: callback
        lora.run(types.SimpleNamespace(**args))
        write("end", {"seconds": round(time.perf_counter() - tic, 1)})


def _hm(seconds: float) -> str:
    return f"{int(seconds // 3600)}시간 {int(seconds % 3600 // 60)}분"


def status(name: str, now: float | None = None) -> str:
    """진행 한 줄 요약 — progress.json과 학습 로그. 남은 시간은 같은 part의 최근 보고 5개 사이 실제 시계 속도
    (heldout 계산 시간 포함)"""
    path, log = ADAPTERS / name / "progress.json", OUT / f"train_{name}.log"
    if not path.exists() or not log.exists():
        return f"{name}: 학습 기록 없음"
    prog = json.loads(path.read_text())
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    now = time.time() if now is None else now
    starts = [r["time"] for r in rows if r["kind"] == "start" and "time" in r]
    ends = [r["time"] for r in rows if r["kind"] == "end"]
    if prog["done"] == prog["total"] and ends:
        now = ends[-1]  # 끝난 학습은 끝난 시각까지
    elapsed = f"경과 {_hm(now - starts[0])}" if starts else "경과 —"
    part = prog["parts"][-1]["dir"]
    trains = [r for r in rows if r["kind"] == "train"]
    vals = [r for r in rows if r["kind"] == "val"]
    if prog["done"] == prog["total"]:
        head = f"{name}  끝남 {prog['total']:,}단계  {elapsed}  part {len(prog['parts'])}개"
    else:
        mine = [r for r in trains if r["part"] == part]
        step = (
            mine[-1]["iteration"] if mine else prog["done"]
        )  # 재시도 직후 옛 part 보고는 체크포인트보다 앞선다
        recent = [r for r in mine if "time" in r][-5:]
        eta = "남은 예상 —"
        if not mine:  # 첫 보고(steps_per_report) 전 — 로딩·첫 heldout 계산
            eta = "모델 불러오는 중·첫 기록 대기"
        if len(recent) >= 2 and recent[-1]["time"] > recent[0]["time"]:
            rate = (recent[-1]["iteration"] - recent[0]["iteration"]) / (
                recent[-1]["time"] - recent[0]["time"]
            )
            eta = f"남은 예상 {_hm((prog['total'] - step) / rate)}"
        pct = 100 * step / prog["total"]
        head = f"{name}  {step:,} / {prog['total']:,}단계 ({pct:.1f}%)  {elapsed} · {eta}"
    loss = f"학습 손실 {trains[-1]['train_loss']:.3f}" if trains else "학습 손실 —"
    held = (
        f"heldout {vals[-1]['val_loss']:.3f} ({vals[-1]['iteration']:,}단계)"
        if vals
        else "heldout —"
    )
    peak = f"최대 메모리 {max(r['peak_memory'] for r in trains):.2f}GB" if trains else ""
    return f"{head}\n{loss} · {held} · {peak} · {part}"


def token_lengths(paths: list[Path]) -> dict:
    """mlx 행(messages)의 채팅 템플릿 토큰 수 — 최대가 max_seq_length를 넘으면 정답이 잘린 채 학습된다"""
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(config()["model"])
    lens = sorted(
        len(tok.apply_chat_template(json.loads(line)["messages"], return_dict=False))
        for p in paths
        for line in p.read_text().splitlines()
    )
    return {"n": len(lens), "p99": lens[int(len(lens) * 0.99)], "max": lens[-1]}
