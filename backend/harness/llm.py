"""앱 게이트웨이 (앱 설계 §7.1). 호출 시간을 묶어 긴 호출 중에 실행 잠금이 만료되지 않게 한다 —
일반 재시도의 호출 최악 시간(타임아웃 × 시도 수 × 폴백 체인)이 RunStore.lock_ttl보다 짧아야 한다.
SDK가 따르는 Retry-After 대기는 상한이 없어 이 계산 밖이고, 그동안 잠금을 뺏기면 RunLocked로 멈춘다."""

import sqlite3
from datetime import datetime

import anthropic

from backend.harness.store import SqliteLlmLog
from shared import llm

APP_TIMEOUT = 120.0  # 초
APP_RETRIES = 1


def make_gateway(conn: sqlite3.Connection, clock=datetime.now, is_eval=False) -> llm.Gateway:
    client = anthropic.Anthropic(timeout=APP_TIMEOUT, max_retries=APP_RETRIES)
    return llm.Gateway(
        llm.load_config(), SqliteLlmLog(conn, is_eval), "runtime", client, clock=clock
    )
