"""단계별 독립 시드 (가상 데이터 생성 설계 §5.1). 단계마다 `seed`와 단계 이름으로 난수 생성기를 따로 만든다 —
전역 난수 생성기를 이어 쓰면 단계를 추가·삭제할 때 다른 단계의 결과까지 바뀐다.
단계 코드는 문자열 set·dict 순회 순서에 기대지 말고 정렬해서 뽑는다 (make data는 PYTHONHASHSEED=0으로도 막음)."""
import zlib

import numpy as np
from faker import Faker


def _stage_key(stage: str) -> int:
    # 내장 hash()는 문자열에 실행마다 다른 솔트가 붙어 쓰지 않는다
    return zlib.crc32(stage.encode())


def stage_rng(seed: int, stage: str) -> np.random.Generator:
    return np.random.default_rng([seed, _stage_key(stage)])


def stage_faker(seed: int, stage: str) -> Faker:
    """전역 Faker.seed가 아니라 단계별 인스턴스에 seed_instance. 이름은 Faker 버전에 따라 바뀌어 uv.lock 커밋 필수."""
    fake = Faker("ko_KR")
    fake.seed_instance((seed << 32) | _stage_key(stage))
    return fake
