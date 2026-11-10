"""기본 설정(config/generator.yaml) 생성 1회를 테스트 실행 전체가 나눠 쓴다 — 모듈마다 같은 생성을 다시 돌지 않게.
xdist 작업자 사이에는 파일 잠금으로 한 작업자만 만들고, 나머지는 끝난 결과를 받는다. 모듈은 DB 복사본과
새로 읽은 단계 상태(run.state)를 받아 다른 모듈의 쓰기·변경이 넘어오지 않는다. 설정을 바꾸는 테스트는 직접 생성한다"""

import pickle
import shutil
from types import SimpleNamespace

import pytest
from filelock import FileLock

from generator import build
from generator.paths import ROOT

CONFIG = ROOT / "config" / "generator.yaml"
FILES = ("source.sqlite", "truth.sqlite")


@pytest.fixture(scope="session")
def generated(tmp_path_factory, worker_id):
    """generated(이름) → (복사본 폴더, run) — run은 state만 있다(pickle 안 되는 값은 빠진다)"""
    base = tmp_path_factory.getbasetemp()
    root = base if worker_id == "master" else base.parent
    cache = root / "generated"
    with FileLock(str(root / "generated.lock")):
        if not (cache / "state.pickle").exists():
            run = build.generate(cache / FILES[0], cache / FILES[1], CONFIG, fake_llm=True)
            state = {}
            for k, v in run.state.items():
                try:
                    state[k] = pickle.dumps(v)
                except TypeError:
                    continue
            (cache / "state.pickle").write_bytes(pickle.dumps(state))
    state = pickle.loads((cache / "state.pickle").read_bytes())

    def copy(name: str):
        out = tmp_path_factory.mktemp(name)
        for f in FILES:
            shutil.copy(cache / f, out / f)
        return out, SimpleNamespace(state={k: pickle.loads(v) for k, v in state.items()})

    return copy
