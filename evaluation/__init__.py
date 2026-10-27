"""평가 — docs/치과 AX - 평가 설계.md. 실행은 evaluation/eval.py"""


class EvalRefused(ValueError):
    """평가를 돌리지 않는 사용법·데이터 거부 — eval.py main이 한 줄로 알리고 exit 1 (그 밖의 오류는 그대로 터진다)"""
