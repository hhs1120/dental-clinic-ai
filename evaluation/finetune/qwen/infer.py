"""Qwen 추론 — 학습과 같은 지시·입력, 탐욕 디코딩(분류기 설계 "Qwen 비교").
결과 out/pred_<name>.jsonl 한 줄 {id, set, text, labels, sec}. 끊겨도 다시 돌리면 이미 낸 id는 건너뛴다.
MLX는 함수 안에서만 import한다 — CI(finetune 그룹 없음)에서 이 모듈의 테스트가 돈다.
"""

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


def predict(rows: list[dict], out_path: Path, generate: Callable[[dict], str]) -> None:
    done = set()
    if out_path.exists():
        done = {json.loads(line)["id"] for line in out_path.read_text().splitlines()}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("a") as f:
        for row in rows:
            if row["id"] in done:
                continue
            t = time.perf_counter()
            text = generate(row)
            out = {"id": row["id"], "set": row.get("set"), "text": text,
                   "labels": parse_labels(text), "sec": round(time.perf_counter() - t, 3)}  # fmt: skip
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
