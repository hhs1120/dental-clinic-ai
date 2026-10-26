---
description: LLM 작업(generator/llm/jobs/)을 Claude Code 보조 에이전트로 채우고 들여온다 (가상 데이터 생성 설계 §5.1)
argument-hint: "[작업 이름(transcript·chart) — 없으면 전부] [최대 조각 수 — 시험용]"
---

`make data`가 내보낸 LLM 작업을 채운다. 인자: $ARGUMENTS

1. `uv run python -m generator.llm_jobs split`으로 남은 작업(이미 캐시에 있는 건 제외)을 조각(`generator/llm/jobs/chunks/<작업>-NNN.jsonl`, 25건씩)으로 나눈다. 인자로 작업 이름을 받았으면 그 작업의 조각만, 최대 조각 수를 받았으면 그만큼만 처리한다.
2. 조각마다 보조 에이전트(Agent 도구, general-purpose) 하나에 아래 지시를 그대로 맡긴다. 한 번에 8개까지 동시에 띄우고, 끝나는 대로 다음 조각을 맡긴다.
3. 모든 조각이 끝나면 `make llm-import`를 돌리고, 작업별 ok·failed·unknown·duplicate·bad 건수를 사용자에게 보고한다. 실패가 있으면 다음 `make data`가 시도 번호를 올려 다시 내보낸다고 알린다. 결과 파일·캐시를 손으로 고치지 않는다.

보조 에이전트 지시 (`<조각>`을 조각 파일 경로로 바꿔 넘긴다):

> `<조각>`은 JSONL이고 한 줄이 LLM 작업 하나다. 줄마다 `system`이 작성 지시, `user`가 입력(JSON), `schema`가 출력 JSON 스키마, `checks`가 통과 기준이다(`must_include`의 각 묶음 중 하나는 글자 그대로 넣기, `forbidden_words`·`forbidden_patterns`는 쓰지 않기, `speakers`가 있으면 줄마다 그 머리로 시작하고 모두 한 번 이상, `length`가 있으면 화자 머리·공백을 뺀 글자 수가 그 범위 안).
> 1. 작업마다 `system` 지시대로 `user` 입력에 맞는 출력 객체를 직접 쓴다(코드나 템플릿으로 찍어내지 않는다 — 건마다 다른 자연스러운 글).
> 2. `generator/llm/results/<조각 파일 이름에서 .jsonl을 뺀 것>.claude-code.jsonl`에 작업마다 한 줄 `{"key": <작업의 key>, "output": <출력 객체>, "source": "claude-code"}`를 쓴다(조각의 작업 전부, 순서대로).
> 3. `uv run python -m generator.llm_jobs check <결과 파일>`로 검사하고, 실패한 키의 줄만 고쳐 다시 검사한다(최대 2번). 그래도 실패한 건은 그대로 둔다.
> 4. 결과 파일 경로, 작업 수, 마지막 검사의 실패 수만 짧게 보고한다. 다른 파일은 건드리지 않는다.
