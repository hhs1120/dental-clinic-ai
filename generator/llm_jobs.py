"""LLM 작업 내보내기·들여오기 — 7·9·9b단계 공통 (가상 데이터 생성 설계 §5.1 'LLM 캐시'·'LLM 단계 채우기').
폴더는 generator/llm/(cache 커밋, jobs·results 일회용) — 입력·출력에 라벨이 들어 있어 앱이 읽는 data/ 밖에 둔다.
- 단계는 건마다 LLMContext.resolve(작업, 건 id, 입력, 검증 기준)를 부른다: 캐시에 통과한 출력이 있으면 그것,
  없으면 작업을 모아 두고 None. 생성이 끝나면 finish()가 빈 건을 jobs/<작업>.jsonl에 쓴다(build.py가 멈춤)
- 작업 한 줄은 완결: 시스템 프롬프트(prompts/<task>/<version>.md)·사용자 입력(JSON — 검증 기준에서 뽑은
  must_mention·avoid_words·length_chars 포함)·출력 스키마·검증 기준·캐시 키
- 채우기: API(fill-api — 게이트웨이 Batch) 또는 Claude Code(/fill-llm-jobs — split·check). 결과는 results/*.jsonl,
  파일 이름은 작업 이름으로 시작, 한 줄 = {key, output | error, source}. 이미 캐시에 있는 작업은 다시 채우지 않는다
- 들여오기(import): 건별 검증 → 캐시(cache/<작업>.jsonl, 한 줄 = 한 시도)에 ok/failed. 출력은 Task.store 필드만 남긴다
  (전사의 scenario는 라벨을 풀어 쓴 문장이라 버림). 실패는 다음 시도 번호로 다시, MAX_ATTEMPTS번 넘게 실패하면 멈춤
- 가짜 모드(테스트)는 FakeGateway로 채우고 캐시·검증을 건너뛴다. fake=False 작업(차트 서술)은 채우지 않는다(초안 유지)
CLI: python -m generator.llm_jobs {import, check FILE, split [--size N], fill-api --task T}
"""
import argparse
import hashlib
import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from generator.paths import ROOT, output_paths
from shared import llm

PROMPTS = ROOT / "prompts"
ALIAS = "gen"  # config/llm.yaml 별칭 — 생성기 7·9·9b
SIMPLIFY_AFTER = 2  # 이 횟수만큼 실패하면 다음 시도부터 단순화한 입력 (§4.1 절차 6)
MAX_ATTEMPTS = 3  # 이 횟수만큼 실패하면 더 내보내지 않고 make data가 멈춘다 — 프롬프트를 고치거나 캐시 줄을 지운다
CHUNK_SIZE = 25  # Claude Code 보조 에이전트 하나가 맡는 작업 수
POLL_SEC = 60
REPORT_KEYS = ("ok", "failed", "unknown", "duplicate", "bad")


@dataclass(frozen=True)
class Task:
    name: str  # 캐시·작업 파일 이름
    prompt: str  # 'gen_transcript/v1' — prompts/ 아래 파일 경로이자 prompt_version
    schema: dict  # 출력 JSON 스키마
    store: tuple[str, ...]  # 캐시에 남기는 출력 필드
    fake: bool = True  # 가짜 모드에서 FakeGateway로 채울지 (False면 그 건은 단계가 그대로 둔다)


def _hash(obj) -> str:
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def user_message(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def procedure_names(conn) -> dict[str, list[str]]:
    """시술 이름 → [이름, 별칭...] — 꼭 넣을 시술은 이 중 하나"""
    return {name: [name, *json.loads(aliases)] for name, aliases in conn.execute("SELECT name, aliases FROM emr_procedures")}


def instructions(checks: dict) -> dict:
    """검증 기준 중 쓰는 쪽이 알아야 하는 것 — 입력에 넣어 API·Claude Code가 같은 기준을 본다"""
    out = {"must_mention": [alts[0] for alts in checks.get("must_include", [])],
           "avoid_words": checks.get("forbidden_words", [])}
    if checks.get("length"):
        out["length_chars"] = checks["length"]
    return out


class Cache:
    """작업 하나의 캐시 — 한 줄 = 한 시도. 저장은 (건 id, 시도) 순으로 다시 써 diff가 고정된다"""

    def __init__(self, path: Path):
        self.path = path
        self.keys, self.tries = set(), defaultdict(list)
        self.entries = []
        for e in _read_jsonl(path) if path.exists() else []:
            self._index(e)
        self.dirty = False

    def _index(self, e: dict) -> None:
        self.entries.append(e)
        self.keys.add(e["key"])
        self.tries[(e["item"], e["prompt_hash"], e["input_hash"])].append(e)

    def find(self, item: str, prompt_hash: str, input_hash: str) -> tuple[dict | None, int]:
        """(통과한 출력, 다음 시도 번호)"""
        tries = self.tries[(item, prompt_hash, input_hash)]
        ok = next((e["output"] for e in tries if e["status"] == "ok"), None)
        return ok, max((e["attempt"] for e in tries), default=0) + 1

    def add(self, entry: dict) -> None:
        self._index(entry)
        self.dirty = True

    def save(self) -> None:
        if self.dirty:
            _write_jsonl(self.path, sorted(self.entries, key=lambda e: (e["item"], e["attempt"])))


class _NullLog:
    def write(self, record) -> None:
        pass

    def spent(self, day, scope) -> float:
        return 0.0


class LLMContext:
    """한 생성 실행의 LLM 단계 상태 — build.py가 run.state["llm"]에 둔다"""

    def __init__(self, paths: dict, fake: bool):
        self.paths, self.fake = paths, fake
        self.caches: dict[str, Cache] = {}
        self.prompts: dict[str, str] = {}
        self.jobs: dict[str, list[dict]] = {}
        self.exhausted: dict[str, list[str]] = defaultdict(list)  # MAX_ATTEMPTS번 실패한 건
        self.gateway = llm.FakeGateway(_NullLog(), "generator") if fake else None

    def _cache(self, task: str) -> Cache:
        if task not in self.caches:
            self.caches[task] = Cache(self.paths["llm_cache"] / f"{task}.jsonl")
        return self.caches[task]

    def resolve(self, task: Task, item: str, payload: dict, checks: dict, simplify=None) -> dict | None:
        if task.prompt not in self.prompts:
            self.prompts[task.prompt] = (PROMPTS / f"{task.prompt}.md").read_text(encoding="utf-8")
        system = self.prompts[task.prompt]
        if self.fake:
            if not task.fake:
                return None
            r = self.gateway.complete(ALIAS, task=task.name, system=system, schema=task.schema,
                                      messages=[{"role": "user", "content": user_message(payload | instructions(checks))}])
            return r.data
        self.jobs.setdefault(task.name, [])
        prompt_hash, input_hash = _hash(system), _hash([payload, checks])
        output, attempt = self._cache(task.name).find(item, prompt_hash, input_hash)
        if output is not None:
            return output
        if attempt > MAX_ATTEMPTS:
            self.exhausted[task.name].append(item)
            return None
        used = simplify(payload) if simplify and attempt > SIMPLIFY_AFTER else payload
        self.jobs[task.name].append({
            "key": _hash([task.name, item, prompt_hash, input_hash, attempt]), "task": task.name, "item": item,
            "attempt": attempt, "prompt_version": task.prompt, "prompt_hash": prompt_hash, "input_hash": input_hash,
            "system": system, "user": user_message(used | instructions(checks)), "schema": task.schema,
            "checks": checks, "store": list(task.store),
        })
        return None

    def finish(self) -> dict[str, int]:
        """빈 건을 작업 파일로 쓰고 {작업: 건수}. 빈 건이 없는 작업은 옛 작업 파일을 지운다"""
        missing = {}
        for name, jobs in sorted(self.jobs.items()):
            path = self.paths["llm_jobs"] / f"{name}.jsonl"
            if jobs:
                _write_jsonl(path, sorted(jobs, key=lambda j: j["item"]))
                missing[name] = len(jobs)
            else:
                path.unlink(missing_ok=True)
        return missing


# ── 검증 ────────────────────────────────────────────────────

@lru_cache
def forbidden_patterns() -> list[str]:
    """금지 토픽 정규식 (config generator.yaml의 forbidden_topics 파일) — 생성물 전부에 검사"""
    cfg = yaml.safe_load((ROOT / "config" / "generator.yaml").read_text(encoding="utf-8"))
    topics = json.loads((ROOT / cfg["forbidden_topics"]).read_text(encoding="utf-8"))
    return [p for t in topics for p in t["patterns"]]


def contains(text: str, word: str) -> bool:
    """앞에 숫자가 붙지 않은 word가 있음 — '2월 5일'이 '12월 5일'에, '1회'가 '11회'에 맞지 않게"""
    return re.search(r"(?<!\d)" + re.escape(word), text) is not None


def spoken(text: str, speakers: list[str] | None) -> str:
    """화자 표시·공백을 뺀 발화"""
    lines = text.splitlines()
    if speakers:
        lines = [next((ln[len(s):] for s in speakers if ln.startswith(s)), ln) for ln in lines]
    return re.sub(r"\s", "", "".join(lines))


def problems(schema: dict, checks: dict, output) -> list[str]:
    """건별 검증 (§4.1 절차 6) — 빈 목록이면 통과. checks: field(검사할 출력 필드), must_include([[대안, ...], ...]),
    forbidden_words, forbidden_patterns(정규식), speakers(줄 머리 — 모두 한 번 이상), length([최소, 최대] 발화 글자 수)"""
    if not isinstance(output, dict):
        return ["출력이 JSON 객체가 아님"]
    empty = [k for k in schema.get("required", []) if not isinstance(output.get(k), str) or not output[k].strip()]
    if empty:
        return [f"필드 없음: {k}" for k in empty]
    text = output[checks["field"]]
    out = [f"꼭 넣을 사실 없음: {' / '.join(alts)}" for alts in checks.get("must_include", [])
           if not any(contains(text, a) for a in alts)]
    out += [f"금지 어휘: {w}" for w in checks.get("forbidden_words", []) if w in text]
    out += [f"금지 토픽: {p}" for p in checks.get("forbidden_patterns", []) if re.search(p, text)]
    speakers = checks.get("speakers")
    if speakers:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        bad = [ln for ln in lines if not ln.startswith(tuple(speakers))]
        if bad:
            out.append(f"화자 표시 없는 줄: {bad[0][:30]}")
        out += [f"화자 {s} 없음" for s in speakers if not any(ln.startswith(s) for ln in lines)]
    if checks.get("length"):
        lo, hi = checks["length"]
        n = len(spoken(text, speakers))
        if not lo <= n <= hi:
            out.append(f"길이 {n}자 — {lo}~{hi}자 밖")
    return out


# ── 들여오기·검사·나누기 ─────────────────────────────────────

def _jobs(paths: dict) -> dict[str, dict]:
    if not paths["llm_jobs"].exists():
        return {}
    return {j["key"]: j for f in sorted(paths["llm_jobs"].glob("*.jsonl")) for j in _read_jsonl(f)}


def _results(path: Path):
    """결과 파일의 줄마다 (줄 번호, 결과 또는 None, 형식 문제)"""
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            yield n, None, "JSON 아님"
            continue
        if not isinstance(r, dict) or not isinstance(r.get("key"), str) or not isinstance(r.get("source"), str) \
                or ("output" in r) == ("error" in r):
            yield n, None, "형식 — {key, source, output 또는 error}"
            continue
        yield n, r, None


def _file_task(path: Path) -> str:
    """결과 파일 이름의 작업 이름 — 'chart-003.claude-code.jsonl'·'transcript.api-x.jsonl'"""
    return path.name.split(".")[0].rsplit("-", 1)[0]


def import_results(paths: dict) -> dict[str, dict]:
    """결과 파일 전부를 검증해 캐시에 넣고 지운다. {작업: {ok, failed, unknown, duplicate, bad}}.
    unknown = 지금 작업 파일에 없는 키(작업을 다시 내보내 키가 바뀐 옛 결과), bad = 형식이 깨진 줄"""
    jobs = _jobs(paths)
    files = sorted(paths["llm_results"].glob("*.jsonl")) if paths["llm_results"].exists() else []
    report = defaultdict(lambda: dict.fromkeys(REPORT_KEYS, 0))
    caches: dict[str, Cache] = {}
    for f in files:
        for _, r, bad in _results(f):
            job = jobs.get(r["key"]) if r else None
            if job is None:
                report[_file_task(f)]["bad" if bad else "unknown"] += 1
                continue
            cache = caches.setdefault(job["task"], Cache(paths["llm_cache"] / f"{job['task']}.jsonl"))
            if r["key"] in cache.keys:
                report[job["task"]]["duplicate"] += 1
                continue
            errors = [r["error"]] if "error" in r else problems(job["schema"], job["checks"], r["output"])
            status = "failed" if errors else "ok"
            output = r.get("output")
            if isinstance(output, dict):
                output = {k: v for k, v in output.items() if k in job["store"]}
            cache.add({k: job[k] for k in ("key", "task", "item", "attempt", "prompt_version", "prompt_hash", "input_hash")}
                      | {"status": status, "source": r["source"], "output": output, "errors": errors})
            report[job["task"]][status] += 1
    for cache in caches.values():
        cache.save()
    for f in files:
        f.unlink()
    return dict(report)


def check_results(paths: dict, path: Path) -> dict[str, list[str]]:
    """결과 파일 하나를 들여오지 않고 검증만 — {키(형식이 깨진 줄은 '줄 N'): 문제}. Claude Code 보조 에이전트가 끝내기 전에 돌린다"""
    jobs = _jobs(paths)
    out = {}
    for n, r, bad in _results(path):
        if bad:
            out[f"줄 {n}"] = [bad]
            continue
        job = jobs.get(r["key"])
        errors = ["모르는 키"] if job is None else \
            [r["error"]] if "error" in r else problems(job["schema"], job["checks"], r["output"])
        if errors:
            out[r["key"]] = errors
    return out


def pending(paths: dict, task: str) -> list[dict]:
    """작업 파일 중 아직 캐시에 없는 것 — 들여온 뒤 make data 전에 다시 채워도 같은 건을 두 번 내지 않게"""
    path = paths["llm_jobs"] / f"{task}.jsonl"
    if not path.exists():
        return []
    done = Cache(paths["llm_cache"] / f"{task}.jsonl").keys
    return [j for j in _read_jsonl(path) if j["key"] not in done]


def split_jobs(paths: dict, size: int = CHUNK_SIZE) -> list[Path]:
    """남은 작업을 size건씩 jobs/chunks/<작업>-NNN.jsonl로 (옛 조각은 지운다)"""
    out_dir = paths["llm_jobs"] / "chunks"
    for old in out_dir.glob("*.jsonl"):
        old.unlink()
    chunks = []
    for f in sorted(paths["llm_jobs"].glob("*.jsonl")):
        rows = pending(paths, f.stem)
        for i in range(0, len(rows), size):
            chunks.append(out_dir / f"{f.stem}-{i // size:03d}.jsonl")
            _write_jsonl(chunks[-1], rows[i:i + size])
    return chunks


# ── API 채우기 (Batch) ──────────────────────────────────────

class StaleBatch(RuntimeError):
    """저장된 배치가 지금 작업 파일과 맞지 않음 (프롬프트·입력이 바뀌어 make data가 작업을 다시 냄)"""


def fill_api(paths: dict, task: str, gateway, sleep=time.sleep, poll_sec: int = POLL_SEC) -> Path | None:
    """남은 작업을 Batch로 제출하고 끝날 때까지 기다려 결과 파일을 쓴다. 배치 번호·제출 키를 저장해 두어 끊긴 뒤 다시
    부르면 이어서 기다린다. 거절·잘림·JSON 오류는 error 줄(실패 시도), 처리 안 된 건(오류·만료)은 줄 없음(같은 작업 그대로)"""
    state = paths["llm_jobs"] / f"{task}.batch.json"
    jobs = {j["key"]: j for j in _read_jsonl(paths["llm_jobs"] / f"{task}.jsonl")} \
        if (paths["llm_jobs"] / f"{task}.jsonl").exists() else {}
    if state.exists():
        saved = json.loads(state.read_text(encoding="utf-8"))
        batch_id, keys = saved["batch_id"], saved["keys"]
        if any(k not in jobs for k in keys):
            raise StaleBatch(f"배치 {batch_id}는 옛 작업입니다 — {state}를 지우고 다시 채우세요")
    else:
        todo = pending(paths, task)
        if not todo:
            print(f"{task}: 채울 작업 없음 (make data가 내보낸 작업이 없거나 전부 들여옴)")
            return None
        requests = [{"custom_id": j["key"], "system": j["system"], "schema": j["schema"],
                     "messages": [{"role": "user", "content": j["user"]}]} for j in todo]
        batch_id = gateway.submit_batch(ALIAS, requests, task=task, prompt_version=todo[0]["prompt_version"])
        keys = [j["key"] for j in todo]
        state.write_text(json.dumps({"batch_id": batch_id, "keys": keys}), encoding="utf-8")
        print(f"{task}: 배치 {batch_id} 제출 ({len(todo)}건)")
    while (status := gateway.batch_status(batch_id)) != "ended":
        print(f"{task}: 배치 {batch_id} {status} — {poll_sec}초 뒤 다시 확인")
        sleep(poll_sec)
    first = jobs[keys[0]]
    results = gateway.batch_results(batch_id, ALIAS, task=task, prompt_version=first["prompt_version"],
                                    schema=first["schema"])
    rows = [{"key": key, "source": r.model} | ({"output": r.data} if r.ok else {"error": r.error})
            for key, r in sorted(results.items()) if r is not None]
    out = paths["llm_results"] / f"{task}.api-{batch_id}.jsonl"
    _write_jsonl(out, rows)
    state.unlink()
    print(f"{task}: 결과 {len(rows)}건 → {out} (처리 안 됨 {len(results) - len(rows)}건)")
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("import", help="결과를 검증해 캐시에 넣는다")
    check = sub.add_parser("check", help="결과 파일 하나를 검증만")
    check.add_argument("file", type=Path)
    split = sub.add_parser("split", help="남은 작업을 Claude Code용 조각으로")
    split.add_argument("--size", type=int, default=CHUNK_SIZE)
    fill = sub.add_parser("fill-api", help="작업 하나의 남은 작업을 Batch API로 채운다")
    fill.add_argument("--task", required=True)
    args = parser.parse_args(argv)
    paths = output_paths()
    if args.cmd == "import":
        report = import_results(paths)
        for task, counts in sorted(report.items()):
            print(f"{task}: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
        print("들여올 결과 없음" if not report else "다음: make data")
        return 0
    if args.cmd == "check":
        bad = check_results(paths, args.file)
        for key, errors in bad.items():
            print(f"{key}: {'; '.join(errors)}")
        print(f"실패 {len(bad)}건" if bad else "전부 통과")
        return 1 if bad else 0
    if args.cmd == "split":
        for chunk in split_jobs(paths, args.size):
            print(chunk.relative_to(ROOT))
        return 0
    gateway = llm.Gateway(llm.load_config(), llm.JsonlLog(paths["llm_log"]), scope="generator")
    try:
        fill_api(paths, args.task, gateway)
    except StaleBatch as e:
        print(e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
