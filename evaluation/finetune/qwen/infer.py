"""Qwen 추론 — 학습과 같은 지시·입력, 탐욕 디코딩(분류기 설계 "Qwen 비교").
결과 out/pred_<name>.jsonl 한 줄 {id, set, text, labels, sec, adapter}. 끊겨도 다시 돌리면 이미 낸 id는 건너뛴다 —
같은 어댑터(지문)로 낸 파일일 때만. 어댑터를 바꿨으면 옛 파일을 옮기고 새로 낸다.
MLX는 함수 안에서만 import한다 — CI(finetune 그룹 없음)에서 이 모듈의 테스트가 돈다.
"""

import hashlib
import json
import time
from collections.abc import Callable
from pathlib import Path

from backend.classify import user_message
from evaluation.finetune.qwen.data import INSTRUCTION, parse_labels

MAX_TOKENS = 256  # 정답 JSON은 100토큰 안팎


def messages(row: dict) -> list[dict]:
    return [
        {"role": "system", "content": INSTRUCTION},
        {"role": "user", "content": user_message(row["transcript"], row["direction"])},
    ]


def render_prompt(tokenizer, row: dict) -> str:
    """학습 로더(mlx-lm ChatDataset)가 정답 앞에 그리는 프롬프트와 같은 문자열"""
    return tokenizer.apply_chat_template(
        messages(row), add_generation_prompt=True, enable_thinking=False, tokenize=False
    )


def adapter_fingerprint(adapter: Path | None) -> str:
    """어댑터 가중치 sha256 앞 16자(학습 전 기본 모델은 none)"""
    if adapter is None:
        return "none"
    return hashlib.sha256((adapter / "adapters.safetensors").read_bytes()).hexdigest()[:16]


def predict(rows: list[dict], out_path: Path, generate: Callable[[dict], str],
            adapter: str) -> None:  # fmt: skip
    done = set()
    if out_path.exists():
        old = [json.loads(line) for line in out_path.read_text().splitlines()]
        if any(r.get("adapter") != adapter for r in old):
            raise SystemExit(f"{out_path.name}는 다른 어댑터로 낸 예측이다 — 옮기거나 지우고 다시")
        done = {r["id"] for r in old}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a") as f:
        for row in rows:
            if row["id"] in done:
                continue
            t = time.perf_counter()
            text = generate(row)
            out = {"id": row["id"], "set": row.get("set"), "text": text,
                   "labels": parse_labels(text), "sec": round(time.perf_counter() - t, 3),
                   "adapter": adapter}  # fmt: skip
            f.write(json.dumps(out, ensure_ascii=False) + "\n")
            f.flush()
            done.add(row["id"])


def mlx_generate(model_id: str, adapter: Path | None) -> Callable[[dict], str]:
    from mlx_lm import generate, load

    model, tokenizer = load(model_id, adapter_path=str(adapter) if adapter else None)

    def one(row: dict) -> str:
        prompt = render_prompt(tokenizer, row)
        return generate(model, tokenizer, prompt=prompt, max_tokens=MAX_TOKENS)

    return one


def peak_memory_gb() -> float:
    import mlx.core as mx

    return mx.get_peak_memory() / 1e9
