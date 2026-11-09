"""골드 점검 — 판정기와 골드가 다른 통화를 중재 판정기(LLM)가 가리고 사람이 표본으로 확인한다.
원본은 평가 설계 §2.3 "골드 점검", 이 모듈이 정한 세부는 docs/superpowers/specs/2026-11-06-gold-audit-design.md.
골드를 보므로 evaluation/에서만 돌고, 기록·호출 로그도 evaluation/에 쓴다(app.sqlite에 쓰지 않는다 — CLAUDE.md 절대 규칙).
"""

import argparse
import hashlib
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from backend import prompts
from backend.classify import REASONING_MAX_TOKENS  # 추론 토큰도 max_tokens 안(앱 설계 §7.2)
from evaluation.classification import _calls, _judgments, _usable, judge_version
from evaluation.metrics import GOLD_AUDIT_SAMPLE
from shared.call_labels import COUNTERPART_KO, FLAG_KO, PURPOSE_KO, differing_items
from shared.llm import BudgetExceeded, Gateway, JsonlLog, load_config

ROOT = Path(__file__).resolve().parents[1]
TASK = "gold_audit"  # prompts/gold_audit/<버전>.md — 머리말 effort가 추론 단계(앱 설계 §7.2)
PROMPT_VERSION = "v1"
AUDIT_PATH = ROOT / "evaluation" / "gold_audit.jsonl"
LLM_LOG = (
    ROOT / "evaluation" / "gold_audit_llm.jsonl"
)  # 중재 입력에 골드가 들어 있어 ax_llm_calls에 쓰지 않는다
ALIAS = "gold_audit"
BATCH_STATE = (
    ROOT / "evaluation" / "gold_audit_batch.json"
)  # 제출한 배치 id — 끊겨도 다시 제출하지 않게(커밋 안 함)

ITEM_KO = {
    "primary_purpose": "용건",
    "counterpart": "통화 상대",
    "urgency": "급한 정도",
    **FLAG_KO,
}
THREE_WAY = (
    "primary_purpose",
    "counterpart",
)  # 둘 다 틀릴 수 있는 항목 — 나머지는 둘 중 하나가 반드시 맞다
SIDES = {"가", "나"}
NEITHER = "둘 다 아님"


def targets(conn, judge_version: str) -> list[dict]:
    """test 판정 풀이 아닌 생성 통화 중 판정기 라벨이 골드와 다른 통화(급함은 urgent 여부만 — differing_items)"""
    calls = _calls(conn)
    judged = _usable(_judgments(conn), judge_version, calls)
    direction = dict(conn.execute("SELECT call_uid, direction FROM source.ax_calls"))
    out = []
    for uid in sorted(judged):
        c = calls[uid]
        if c["split"] == "test":
            continue
        labels = judged[uid]["labels"]
        diff = differing_items(labels, c["gold"])
        if not diff:
            continue
        out.append(
            {
                "call_uid": uid,
                "direction": direction[uid],
                "transcript": c["transcript"],
                "transcript_sha": transcript_sha(c["transcript"]),
                "items": {i: {"gold": c["gold"][i], "judge": labels[i]} for i in diff},
            }
        )
    return out


def transcript_sha(transcript: str) -> str:
    return hashlib.sha256(transcript.encode()).hexdigest()


def gold_side(call_uid: str, item: str) -> str:
    """골드를 가·나 중 어디에 둘지 — 통화·항목마다 해시로 섞는다(평가 설계 §2.3)"""
    return "가" if hashlib.sha256(f"{call_uid}:{item}".encode()).digest()[0] % 2 == 0 else "나"


def choices(item: str) -> tuple[str, ...]:
    return ("가", "나", NEITHER) if item in THREE_WAY else ("가", "나")


def show(item: str, value, codes: bool = False) -> str:
    """라벨 값을 우리말로 — 급함은 대상이 urgent 여부로 정해져 두 값으로만 보인다.
    codes면 프롬프트 정의와 맞게 코드 값을 앞에(중재 입력용)"""
    if item == "urgency":
        return "급함" if value == "urgent" else "급하지 않음"
    if item in THREE_WAY:
        ko = (PURPOSE_KO if item == "primary_purpose" else COUNTERPART_KO)[value]
        return f"{value} ({ko})" if codes else ko
    ko = "예" if value else "아니오"
    return f"{'true' if value else 'false'} ({ko})" if codes else ko


def pair(target: dict, item: str, codes: bool = False) -> dict[str, str]:
    """{가: 값, 나: 값}"""
    v = target["items"][item]
    side = gold_side(target["call_uid"], item)
    other = "나" if side == "가" else "가"
    return {side: show(item, v["gold"], codes), other: show(item, v["judge"], codes)}


def arbiter_request(target: dict) -> tuple[str, dict]:
    """중재 판정기 입력(user 메시지)과 구조화 출력 스키마 — 어느 쪽이 골드인지 드러내지 않는다"""
    lines = [
        f"방향: {target['direction']}",
        "",
        "녹취록:",
        target["transcript"],
        "",
        "고를 항목:",
    ]
    for item in target["items"]:
        p = pair(target, item, codes=True)
        lines.append(f"- {item} ({ITEM_KO[item]}): 가 = {p['가']} / 나 = {p['나']}")
    schema = {
        "type": "object",
        "properties": {i: {"type": "string", "enum": list(choices(i))} for i in target["items"]},
        "required": list(target["items"]),
        "additionalProperties": False,
    }
    return "\n".join(lines), schema


def decode(target: dict, answer: dict) -> dict[str, str]:
    """가·나·둘 다 아님 → gold·judge·neither. 항목이 빠졌거나 선택지 밖이면 ValueError"""
    out = {}
    for item in target["items"]:
        a = answer.get(item)
        if a not in choices(item):
            raise ValueError(f"{target['call_uid']} {item}: {a!r}")
        out[item] = (
            "neither"
            if a == NEITHER
            else ("gold" if a == gold_side(target["call_uid"], item) else "judge")
        )
    return out


def load(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def save(path: Path, records: list[dict]) -> None:
    """통째로 다시 쓴다 — (통화, 판정기 버전) 순"""
    records = sorted(records, key=lambda r: (r["call_uid"], r["judge_version"]))
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8"
    )


def _todo(conn, path: Path, version: str, limit: int | None) -> list[dict]:
    """지금 맞는 기록이 없는 대상 — 해시 순, limit이면 앞에서 N통화"""
    cur, _ = current(load(path), conn, version)
    have = {r["call_uid"] for r in cur}
    return sorted(
        (t for t in targets(conn, version) if t["call_uid"] not in have),
        key=lambda t: _hash_order(t["call_uid"]),
    )[:limit]


def _request(t: dict, effort: str | None) -> dict:
    """단건·배치가 같은 입력·설정을 쓴다"""
    msg, schema = arbiter_request(t)
    return {
        "system": prompts.load(TASK, PROMPT_VERSION)["system"],
        "schema": schema,
        "messages": [{"role": "user", "content": msg}],
        **({"effort": effort, "max_tokens": REASONING_MAX_TOKENS} if effort else {}),
    }


def _to_record(t: dict, r, version: str, effort: str | None) -> dict | None:
    """실패·폴백·선택지 밖 답이면 None — 기록하지 않아 다음 실행에서 다시 돈다"""
    if (
        r is None or not r.ok or r.fallback_used
    ):  # 폴백은 다른 모델의 중재라 섞이지 않게(평가 설계 §1 원칙 5)
        return None
    try:
        picked = decode(t, r.data)
    except (ValueError, AttributeError):
        return None
    return {
        "call_uid": t["call_uid"],
        "judge_version": version,
        "transcript_sha": t["transcript_sha"],
        "prompt_version": PROMPT_VERSION,
        "arbiter_model": r.model,
        "effort": effort,
        "items": {
            i: {**v, "gold_side": gold_side(t["call_uid"], i), "arbiter": picked[i], "human": None}
            for i, v in t["items"].items()
        },
    }


def _snapshot(t: dict) -> dict:
    """대상의 녹취록 해시와 항목별 (골드, 판정기) — 제출 때와 받을 때가 같은지 본다"""
    return {
        "sha": t["transcript_sha"],
        "items": {i: [v["gold"], v["judge"]] for i, v in t["items"].items()},
    }


def _put(records: list[dict], rec: dict) -> list[dict]:
    """같은 (통화, 판정기 버전)의 옛 줄을 새 줄로"""
    key = (rec["call_uid"], rec["judge_version"])
    return [r for r in records if (r["call_uid"], r["judge_version"]) != key] + [rec]


def arbitrate(
    gw,
    conn,
    path: Path,
    version: str,
    workers: int = 4,
    limit: int | None = None,
    effort: str | None = None,
) -> dict:
    """그 판정기 버전에 지금 맞는 기록이 없는 대상만 중재한다(녹취록·값이 바뀐 낡은 줄은 새 줄로 바꾼다).
    실패·예외·폴백·선택지 밖 답은 기록하지 않아 다음 실행에서 다시 돈다. 끝나는 대로 저장한다.
    limit = 해시 순 앞에서 N통화(맛보기), effort = 프롬프트 머리말 대신 쓸 추론 단계"""
    todo = _todo(conn, path, version, limit)
    effort = effort or prompts.effort(TASK, PROMPT_VERSION)

    def one(t):
        try:
            r = gw.complete(
                ALIAS, task="gold-audit", prompt_version=PROMPT_VERSION, **_request(t, effort)
            )
        except BudgetExceeded:  # 하루 상한은 실패로 묻지 않고 알린다(끝난 결과는 이미 저장됨)
            raise
        except Exception:  # noqa: BLE001 — 한 건의 예외로 끝난 결과를 잃지 않게 실패로 센다
            return None, 0
        return _to_record(t, r, version, effort), r.cost_usd

    records, done, cost = load(path), 0, 0.0
    with ThreadPoolExecutor(workers) as ex:
        for fut in as_completed([ex.submit(one, t) for t in todo]):
            rec, c = fut.result()
            cost += c or 0
            if rec is not None:
                records = _put(records, rec)
                save(path, records)
                done += 1
    return {"done": done, "failed": len(todo) - done, "cost_usd": round(cost, 4)}


def arbitrate_batch(
    gw,
    conn,
    path: Path,
    version: str,
    limit: int | None = None,
    effort: str | None = None,
    state_path: Path = BATCH_STATE,
    poll_seconds: int = 60,
    max_polls: int | None = None,
) -> dict:
    """arbitrate와 같은 대상·입력을 Batch로(반값, 느림). 제출한 배치 id·기록 파일·대상 값을 state_path에 남겨, 끊겨도
    다시 제출하지 않고 그 결과를 받는다(다시 실행할 때 --limit·--effort는 쓰지 않는다). 결과 없는 건과 제출 뒤 값이
    바뀐 대상의 답은 기록하지 않아 다음 실행에서 다시 돈다"""
    if state_path.exists():
        st = json.loads(state_path.read_text(encoding="utf-8"))
        if st["judge_version"] != version or st["out"] != str(path):
            raise SystemExit(
                f"판정기 {st['judge_version']}·기록 {st['out']}의 배치가 진행 중 — {state_path}"
            )
        bid, effort, sent = st["batch_id"], st["effort"], st["targets"]
    else:
        effort = effort or prompts.effort(TASK, PROMPT_VERSION)
        todo = _todo(conn, path, version, limit)
        if not todo:
            return {"done": 0, "failed": 0, "cost_usd": 0.0}
        reqs = [{"custom_id": t["call_uid"], **_request(t, effort)} for t in todo]
        bid = gw.submit_batch(ALIAS, reqs, task="gold-audit", prompt_version=PROMPT_VERSION)
        sent = {t["call_uid"]: _snapshot(t) for t in todo}
        state_path.write_text(
            json.dumps(
                {
                    "batch_id": bid,
                    "judge_version": version,
                    "effort": effort,
                    "out": str(path),
                    "targets": sent,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    polls = 0
    while gw.batch_status(bid) != "ended":
        polls += 1
        if max_polls and polls >= max_polls:
            raise TimeoutError(f"배치 {bid} 진행 중 — 다시 실행하면 이어서 받는다")
        time.sleep(poll_seconds)
    results = gw.batch_results(
        bid, ALIAS, task="gold-audit", prompt_version=PROMPT_VERSION, schema={"type": "object"}
    )
    now = {t["call_uid"]: t for t in targets(conn, version)}
    records, done = load(path), 0
    for uid, r in results.items():
        same = uid in now and _snapshot(now[uid]) == sent.get(
            uid
        )  # 제출 뒤 녹취록·값이 바뀌면 옛 답
        rec = _to_record(now[uid], r, version, effort) if same else None
        if rec is not None:
            records = _put(records, rec)
            done += 1
    save(path, records)
    state_path.unlink()
    cost = sum(r.cost_usd or 0 for r in results.values() if r is not None)
    return {"done": done, "failed": len(results) - done, "cost_usd": round(cost, 4)}


def current(records: list[dict], conn, version: str) -> tuple[list[dict], int]:
    """그 판정기 버전이고 녹취록·항목·골드·판정기 값이 지금과 같은 줄, 그리고 낡은 줄 수 — 재생성 뒤 해시로 다시 붙인다"""
    now = {t["call_uid"]: t for t in targets(conn, version)}

    def fresh(r):
        t = now.get(r["call_uid"])
        return (
            t is not None
            and t["transcript_sha"] == r["transcript_sha"]
            and {i: (v["gold"], v["judge"]) for i, v in r["items"].items()}
            == {i: (v["gold"], v["judge"]) for i, v in t["items"].items()}
        )

    mine = [r for r in records if r["judge_version"] == version]
    ok = [r for r in mine if fresh(r)]
    return ok, len(mine) - len(ok)


def group(record: dict) -> str:
    """표본 묶음 — 판정기 쪽 항목이 하나라도 있으면 judge, 없고 둘 다 아님이 있으면 neither, 그 밖 gold(평가 설계 §2.3)"""
    picked = {v["arbiter"] for v in record["items"].values()}
    return "judge" if "judge" in picked else "neither" if "neither" in picked else "gold"


def _hash_order(uid: str) -> str:
    return hashlib.sha256(uid.encode()).hexdigest()


def sample(records: list[dict], n: int = GOLD_AUDIT_SAMPLE) -> list[str]:
    """묶음마다 고르게 call_uid 해시 순으로 — 모자란 묶음의 몫은 남은 묶음에 돌아가며 채운다"""
    groups = {g: [] for g in ("judge", "neither", "gold")}
    for r in records:
        groups[group(r)].append(r["call_uid"])
    for g in groups.values():
        g.sort(key=_hash_order)
    quota = [n // 3 + (1 if i < n % 3 else 0) for i in range(3)]
    picked, rest = [], []
    for q, g in zip(quota, groups.values(), strict=True):
        picked += g[:q]
        rest.append(g[q:])
    while len(picked) < n and any(rest):  # 모자란 몫은 남은 묶음에 돌아가며
        for g in rest:
            if g and len(picked) < n:
                picked.append(g.pop(0))
    return picked


def pending(records: list[dict]) -> list[str]:
    """사람이 아직 안 본 표본 통화 — 해시 순(묶음 순으로 보이면 중재 결과가 미리 샌다)"""
    of = {r["call_uid"]: r for r in records}
    return sorted(
        (u for u in sample(records) if any(v["human"] is None for v in of[u]["items"].values())),
        key=_hash_order,
    )


def errors(records: list[dict]) -> set[tuple[str, str]]:
    """골드 오류 추정 (통화, 항목) — 중재가 판정기 쪽이나 둘 다 아님을 고른 항목. 사람 답은 쓰지 않는다(골드 점검 설계)"""
    return {
        (r["call_uid"], i)
        for r in records
        for i, v in r["items"].items()
        if v["arbiter"] in ("judge", "neither")
    }


def summarize(records: list[dict]) -> dict:
    """리포트 숫자 — 항목별 중재 선택 수, 중재 vs 사람 일치(사람이 고른 항목만, 항목별·중재 선택별), 표본 안 사람 기준 오류 추정(참고)"""
    arbiter, agree, by_arbiter = {}, {}, {}
    human_errors = {"judge": 0, "neither": 0}
    for r in records:
        for i, v in r["items"].items():
            arbiter.setdefault(i, {"gold": 0, "judge": 0, "neither": 0})[v["arbiter"]] += 1
            if v["human"] is None:
                continue
            a = agree.setdefault(i, {"k": 0, "n": 0})
            a["n"] += 1
            a["k"] += v["human"] == v["arbiter"]
            b = by_arbiter.setdefault(v["arbiter"], {"k": 0, "n": 0})
            b["n"] += 1
            b["k"] += v["human"] == v["arbiter"]
            if v["human"] in human_errors:
                human_errors[v["human"]] += 1
    of = {r["call_uid"]: r for r in records}
    pending = sum(
        any(v["human"] is None for v in of[u]["items"].values()) for u in sample(records)
    )
    return {
        "calls": len(records),
        "arbiter": arbiter,
        "agreement": agree,
        "agreement_total": {
            "k": sum(a["k"] for a in agree.values()),
            "n": sum(a["n"] for a in agree.values()),
        },
        "by_arbiter": by_arbiter,
        "human_errors": human_errors,
        "human_pending": pending,
    }


def _sections() -> dict[str, str]:
    """중재 프롬프트 본문을 절 제목(괄호 속 항목 이름 또는 '표시')별로 — 화면 안내를 프롬프트 정의와 같게"""
    body = prompts.load(TASK, PROMPT_VERSION)["system"]
    out = {}
    for part in re.split(r"^## ", body, flags=re.M)[1:]:
        head, _, text = part.partition("\n")
        m = re.search(r"\((\w+)\)", head)
        out["flags" if "표시" in head else (m[1] if m else head)] = text
    return out


def guide(item: str) -> list[str]:
    """사람 화면에서 그 항목의 라벨 전체와 뜻(프롬프트 정의 그대로, 값은 우리말)"""
    sec = _sections()

    def defs(text: str) -> dict[str, str]:
        return dict(re.findall(r"^- (\w+) — (.+)$", text, flags=re.M))

    if item == "primary_purpose":
        order = re.search(r"^판단 순서: .+$", sec["primary_purpose"], flags=re.M)
        return ([f"  ({order[0]})"] if order else []) + [
            f"  {PURPOSE_KO[k]} — {v}" for k, v in defs(sec["primary_purpose"]).items()
        ]
    if item == "counterpart":
        d = dict(re.findall(r"(\w+)\(([^)]*)\)", sec["counterpart"]))
        return [
            f"  {COUNTERPART_KO[k]} — {d.get(k, '위 어디에도 맞지 않음')}" for k in COUNTERPART_KO
        ]
    if item == "urgency":
        d = defs(sec["urgency"])
        return [f"  급함 — {d['urgent']}", "  급하지 않음 — 그 밖(보통·낮음)"]
    return [f"  예 = {defs(sec['flags'])[item]}"]


KEYS = {"1": "가", "2": "나", "3": NEITHER}


def audit(conn, path: Path, version: str, read=input, write=print) -> None:
    """사람 표본 판정 — 한 통화씩 녹취록과 가린 두 답을 보이고 항목마다 고른다. 통화마다 저장, 중재 결과는 저장 뒤에만"""
    by_uid = {t["call_uid"]: t for t in targets(conn, version)}
    records = load(path)
    cur, stale = current(records, conn, version)
    missing = set(by_uid) - {r["call_uid"] for r in cur}
    if missing:
        raise SystemExit(f"중재가 끝나지 않음({len(missing)}통화) — make gold-arbitrate 먼저")
    rec_of = {r["call_uid"]: r for r in cur}
    picks = sample(cur)
    todo = pending(cur)
    if stale:
        write(f"낡은 기록(녹취록·값이 바뀜) {stale}줄은 건너뜀")
    write(f"표본 {len(picks)}통화 중 남은 {len(todo)}통화")
    for k, uid in enumerate(todo, len(picks) - len(todo) + 1):
        t, rec = by_uid[uid], rec_of[uid]
        write(f"\n[{k}/{len(picks)}] {uid} · {t['direction']}\n")
        write(t["transcript"])
        answer = {}
        for item in t["items"]:
            p = pair(t, item)
            keys = " · ".join(f"{key} {KEYS[key]}" for key in list(KEYS)[: len(choices(item))])
            write(f"\n{ITEM_KO[item]} — 고를 수 있는 라벨")
            for line in guide(item):
                write(line)
            write(f"  ▶ 가: {p['가']} / 나: {p['나']}")
            while True:
                key = read(f"  {keys} > ").strip()
                if key in KEYS and KEYS[key] in choices(item):
                    answer[item] = KEYS[key]
                    break
                write("  다시 고르세요")
        for item, side in decode(t, answer).items():
            rec["items"][item]["human"] = side
        save(path, records)
        names = {"gold": "골드 쪽", "judge": "판정기 쪽", "neither": NEITHER}
        write(
            "저장 — 중재: "
            + ", ".join(f"{ITEM_KO[i]} {names[v['arbiter']]}" for i, v in rec["items"].items())
        )


def connect(app: Path, source: Path, truth: Path) -> sqlite3.Connection:
    """읽기 전용으로 연다 — 골드 점검은 앱·생성·정답 DB에 쓰지 않는다"""
    conn = sqlite3.connect(f"file:{app}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("ATTACH DATABASE ? AS source", (f"file:{source}?mode=ro",))
    conn.execute("ATTACH DATABASE ? AS truth", (f"file:{truth}?mode=ro",))
    return conn


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("arbitrate", "audit"))
    parser.add_argument("--judge-version", help="기본은 활성 판정기 버전")
    parser.add_argument("--limit", type=int, help="arbitrate: 해시 순 앞에서 N통화(맛보기)")
    parser.add_argument("--effort", help="arbitrate: 프롬프트 머리말 대신 쓸 추론 단계")
    parser.add_argument("--out", type=Path, default=AUDIT_PATH, help="기록 파일(맛보기는 따로)")
    parser.add_argument("--batch", action="store_true", help="arbitrate: Batch API(반값, 느림)")
    parser.add_argument("--db", type=Path, default=ROOT / "data" / "app.sqlite")
    parser.add_argument("--source", type=Path, default=ROOT / "data" / "source.sqlite")
    parser.add_argument("--truth", type=Path, default=ROOT / "ground_truth" / "truth.sqlite")
    args = parser.parse_args(argv)
    conn = connect(args.db, args.source, args.truth)
    version = args.judge_version or judge_version(conn)
    if not version:
        raise SystemExit("판정기 버전을 못 정함 — --judge-version")
    if args.command == "audit":
        try:
            audit(conn, args.out, version)
        except (EOFError, KeyboardInterrupt):
            print("\n중단 — 다시 실행하면 이어서 한다")
        return
    gw = Gateway(load_config(), JsonlLog(LLM_LOG), "runtime")
    run = arbitrate_batch if args.batch else arbitrate
    r = run(gw, conn, args.out, version, limit=args.limit, effort=args.effort)
    print(f"판정기 {version}: 중재 {r['done']}건, 실패 {r['failed']}건, ${r['cost_usd']}")


if __name__ == "__main__":
    main()
