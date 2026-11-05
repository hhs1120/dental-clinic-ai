"""dev 판정 풀·dev 블라인드 통화 조건 (앱 설계 §6). 판정 풀은 eval_sort_order 앞 절반(⌈eval_pool_size ÷ 2⌉)이 dev —
앱은 골드가 없어 분할을 이 정렬로만 안다. 반환은 source.ax_calls 별칭에 붙일 SQL 조건"""

import math

from shared.clinic import Clinic


def dev_eval_sql(clinic: Clinic, alias: str = "a") -> str:
    n = math.ceil(clinic.eval_pool_size / 2)
    return f"{alias}.in_eval_pool = 1 AND {alias}.eval_sort_order <= {n}"


def dev_blind_sql(clinic: Clinic, alias: str = "a") -> str:
    return f"{dev_eval_sql(clinic, alias)} AND {alias}.in_blind_pool = 1"
