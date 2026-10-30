# 와이어프레임

앱 화면을 클릭해 보는 HTML 한 장(`index.html`)과 그 데이터를 뽑는 스크립트(`export.py`). 화면 구성의 근거는 원본 글 1~4편, 기능 범위는 [[치과 AX - 기능 정의]].

- 데이터: `make data` 뒤에 `make wireframe` → `data.json`(생성 DB에서 화면별로 뽑음. AI 결과 칸은 정답 라벨로 채운 예시라 커밋하지 않는다. 통화는 silver만, 물어보기 예시는 dev 문항만 — 판정 풀·test 정답이 보이면 사람 판정·평가가 오염된다)
- 보기: `data.json`을 읽어야 해서 파일을 바로 열지 말고 이 폴더에서 `python -m http.server`로 연다
- 공유본: claude.ai 아티팩트(비공개)에 `index.html`과 `data.json`을 올려 둔다 — 데이터가 바뀌면 다시 뽑아 같은 아티팩트에 올린다
