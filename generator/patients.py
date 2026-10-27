"""2단계 환자 — 구환 적재 + 신환 후보 풀 (가상 데이터 생성 설계 §2.2·§2.6 (1)). 값은 generator/master.py.

구환: 창 이전 마지막 위생 내원일(run.state, 3단계가 리콜 기한을 정함)보다 앞서 등록. 차트 번호는 등록순.
가족: 보호자(성인)·소아·청소년은 가족 성, 다른 성인·노인(배우자·부모)은 자기 성. 소아는 보호자 번호를 같이 쓴다.
"""
from datetime import date, timedelta

from generator.master import (AGE_BANDS, FAMILY_SHARE, FAMILY_SIZE, GUARDIAN_MIN_AGE_GAP, MAX_AGE, MIN_VISIT_AGE,
                              PRIVATE_INSURANCE_SHARE, RECALL_CYCLE, RECALL_CYCLE_MONTHS, REGISTERED_FROM, add_years)
from generator.seeding import stage_faker, stage_rng

PHONES = 10_000  # 010-0000-xxxx 가상 번호 개수
FAMILY_SURNAME_BANDS = ("child", "teen")  # 보호자 외에 가족 성을 따르는 구간


def _birth(rng, band: str, today: date) -> date:
    """그 구간 안의 나이(TODAY 기준)를 균등 추첨 → 그 나이가 되는 생일 중 균등"""
    lo, hi = next((lo, hi) for name, lo, hi, _ in AGE_BANDS if name == band)
    age = int(rng.integers(lo, (hi if hi is not None else MAX_AGE) + 1))
    upper, lower = add_years(today, -age), add_years(today, -(age + 1))
    return upper - timedelta(days=int(rng.integers(0, (upper - lower).days)))


def _exact_bands(rng, n: int) -> list[str]:
    """구간별 정확히 비중 × n명(최대 나머지 방식으로 반올림) → 섞음"""
    raw = [share * n for *_, share in AGE_BANDS]
    counts = [int(x) for x in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - counts[i], reverse=True)[:n - sum(counts)]:
        counts[i] += 1
    bands = [name for (name, *_), k in zip(AGE_BANDS, counts) for _ in range(k)]
    return [bands[i] for i in rng.permutation(n)]


def _pick(rng, n: int, share: float) -> set[int]:
    return set(rng.choice(n, round(share * n), replace=False).tolist())


def _first_name(fake, sex: str) -> str:
    return fake.first_name_male() if sex == "M" else fake.first_name_female()


def _families(rng, people: list[dict]) -> list[list[int]]:
    """가족(첫 원소 = 보호자 성인) 목록. 소아는 전원 가족에 넣고, 가족 인원 합이 FAMILY_SHARE × n이 될 때까지 묶는다.
    보호자는 가족의 소아·청소년보다 GUARDIAN_MIN_AGE_GAP세 이상 많은 성인"""
    n = len(people)
    target = round(FAMILY_SHARE * n)
    order = rng.permutation(n).tolist()
    bands = [p["band"] for p in people]
    children = [i for i in order if bands[i] == "child"]
    adults = [i for i in order if bands[i] == "adult"]
    others = [i for i in order if bands[i] != "child"]

    def old_enough(guardian: int, young: int) -> bool:
        return people[guardian]["birth_date"] <= add_years(people[young]["birth_date"], -GUARDIAN_MIN_AGE_GAP)

    used: set[int] = set()
    families, total = [], 0
    while children or total < target:
        size = int(rng.integers(FAMILY_SIZE[0], FAMILY_SIZE[1] + 1))
        if not children:
            size = min(size, target - total)
            if size < FAMILY_SIZE[0]:
                break
        kids = children[:size - 1]
        children = children[size - 1:]
        guardian = next(g for g in adults if g not in used and all(old_enough(g, k) for k in kids))
        family = [guardian] + kids
        used.update(family)
        while len(family) < size:
            extra = next(i for i in others if i not in used and (bands[i] != "teen" or old_enough(guardian, i)))
            family.append(extra)
            used.add(extra)
        families.append(family)
        total += size
    return families


def stage(run, rng, fake) -> None:
    n = run.cfg["returning_patients"]
    bands = _exact_bands(rng, n)
    people = []
    for band in bands:
        # 마지막 위생 내원 = 시뮬레이션 시작 전 한 리콜 주기에 고르게. 등록은 MIN_VISIT_AGE세 생일 ~ 그 전날
        last_hygiene = run.range_start - timedelta(days=int(rng.integers(1, RECALL_CYCLE.days + 1)))
        while True:
            birth = _birth(rng, band, run.today)
            lo, hi = max(REGISTERED_FROM, add_years(birth, MIN_VISIT_AGE)), last_hygiene - timedelta(days=1)
            if lo <= hi:
                break
        sex = "M" if rng.random() < 0.5 else "F"
        people.append({"band": band, "sex": sex, "birth_date": birth, "last_hygiene": last_hygiene,
                       "registered_at": lo + timedelta(days=int(rng.integers(0, (hi - lo).days + 1))),
                       "surname": fake.last_name(), "first_name": _first_name(fake, sex),
                       "family": None, "guardian": None})

    families = _families(rng, people)
    for fid, family in enumerate(families, start=1):
        guardian = people[family[0]]
        for i in family:
            people[i]["family"] = fid
            if i != family[0] and people[i]["band"] in FAMILY_SURNAME_BANDS:
                people[i]["surname"] = guardian["surname"]
            if people[i]["band"] == "child":
                people[i]["guardian"] = family[0]

    phones = iter(f"010-0000-{k:04d}" for k in rng.permutation(PHONES).tolist())
    for p in people:
        if p["guardian"] is None:
            p["phone"] = next(phones)
    for p in people:
        if p["guardian"] is not None:
            p["phone"] = people[p["guardian"]]["phone"]
    insured = _pick(rng, n, PRIVATE_INSURANCE_SHARE)

    # 차트 번호 = 등록순
    ranked = sorted(range(n), key=lambda i: (people[i]["registered_at"], i))
    pid = {i: k + 1 for k, i in enumerate(ranked)}
    run.conn.executemany("INSERT INTO emr_families VALUES (?, ?)",
                         [(fid, people[f[0]]["surname"]) for fid, f in enumerate(families, start=1)])
    run.conn.executemany(
        "INSERT INTO emr_patients (id, chart_no, name, birth_date, sex, phone, family_id, guardian_patient_id,"
        " registered_at, recall_cycle_months, has_private_insurance) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(pid[i], f"C-{pid[i]:05d}", p["surname"] + p["first_name"], p["birth_date"].isoformat(), p["sex"],
          p["phone"], p["family"], pid[p["guardian"]] if p["guardian"] is not None else None,
          p["registered_at"].isoformat(), RECALL_CYCLE_MONTHS, int(i in insured))
         for i in ranked for p in [people[i]]])
    run.state["patients"] = [{"id": pid[i], "birth_date": people[i]["birth_date"], "family_id": people[i]["family"],
                              "last_hygiene": people[i]["last_hygiene"]} for i in ranked]
    # 4단계 신환 소아의 보호자 = 상한 미만 가족의 보호자 (§2.2)
    run.state["families"] = {fid: {"guardian": pid[f[0]], "members": [pid[i] for i in f]}
                             for fid, f in enumerate(families, start=1)}
    run.state["phones"] = phones  # 남은 번호 — 신환 후보가 이어서 쓴다


def new_patient_candidates(run):
    """신환 후보를 하나씩 — 4단계가 필요할 때 꺼내 등록한다(쓰지 않은 후보는 버림). 연령 구간은 AGE_BANDS 비중으로 추첨.
    자기 시드("new_patients")라 꺼내는 수가 달라도 앞 후보는 같다. 남은 번호(run.state["phones"])를 소비하므로 한 번만 만든다.
    4단계 계약: birth_date(TODAY 기준 나이)가 등록일 이후면 건너뛴다(출생 전 등록 금지).
    소아는 등록 때 보호자(구환 성인, GUARDIAN_MIN_AGE_GAP 충족)의 성·번호로 바꾼다"""
    rng, fake = stage_rng(run.seed, "new_patients"), stage_faker(run.seed, "new_patients")
    names, shares = [name for name, *_ in AGE_BANDS], [share for *_, share in AGE_BANDS]
    while True:
        band = names[int(rng.choice(len(names), p=shares))]
        sex = "M" if rng.random() < 0.5 else "F"
        insured = rng.random() < PRIVATE_INSURANCE_SHARE
        yield {"band": band, "sex": sex, "birth_date": _birth(rng, band, run.today), "surname": fake.last_name(),
               "first_name": _first_name(fake, sex), "phone": next(run.state["phones"]),
               "has_private_insurance": int(insured)}
