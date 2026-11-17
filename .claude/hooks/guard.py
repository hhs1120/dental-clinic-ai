"""Claude Code 훅 — 이 저장소 규칙을 도구 호출 앞뒤에 지킨다(.claude/settings.json과 읽기 전용 에이전트 머리말이 부른다).

PreToolUse
- Bash 막기: `git add`는 파일 이름으로만(-A·--all·-u·`.`·폴더·글롭 금지 — 글롭 문자가 든 실제 파일 이름은 허용), `git commit -a` 금지 — CLAUDE.md '깃'.
  `git commit` 전에 `make lint`가 통과해야 한다 — CLAUDE.md '코드 컨벤션'. lint는 명령이 도는 저장소(worktree면 그 worktree)에서
- Bash 막기(지우기): 보호 경로(data/·ground_truth/·.env·.git·.claude)·저장소 통째·저장소 밖(임시 폴더 안 제외 — 임시 루트
  자체도 막음)을 지우는 rm·rmdir·unlink·shred·truncate·find -delete, 옮겨 없애는 mv 원본(-t·--target-directory 포함).
  글롭은 펼쳐서(*·./*는 숨김까지), $(…)·백틱은 무엇을 지울지 몰라 막는다. 같은 명령 안의 cd·git -C를 따라간다.
  .claude/worktrees/<이름>/ 안은 그 worktree를 저장소처럼 본다
- Bash 막기(되돌리기): git clean(-n 제외), git reset --hard, checkout·switch 강제(-f·--discard-changes), 폴더·`.`·:/·글롭을
  되돌리는 checkout·restore(--staged만은 통과)
- Bash 막기(비밀): .env를 읽는 명령(글롭·`<` 입력 포함, --env-file은 통과)·.env에 쓰는 cp·mv, 저장소 루트를 뒤지는 grep -r
  (--exclude가 .env·.env.local을 다 빼면 통과)·rg -uu/--hidden --no-ignore, env·printenv·export·set·declare 전체 출력,
  키 변수(이름에 KEY·TOKEN·SECRET·PASSWORD 단어) echo
- Bash 막기(정답·DB 쓰기): ground_truth/·data/*.sqlite로 리디렉션·sed -i·cp·mv·install·ln 대상(폴더 대상이면 그 안 이름)·
  tee·dd of=, sqlite3 쓰기 SQL(인자·-cmd·히어독·echo 파이프 — 문장 시작의 쓰기 키워드·점 명령·pragma 대입)과 파일·다른
  명령 출력으로 먹이는 SQL. -readonly·mode=ro는 통과. make data 같은 정식 명령은 통과
- Bash 확인 창: 과금·되돌리기 어려운 명령은 사용자 승인 — main으로(HEAD·@는 현재 브랜치)·강제·--mirror·--all push
  (feat 브랜치 push와 PR 만들기는 그냥), PR 머지, CLAUDE.md "(과금)" 명령(make 대상·backend·evaluation 일괄 모듈과 그
  스크립트 경로, 무료 모드 PLAN=·--plan·--stats·--help·-h는 제외 — 모듈 인자에서만), 모든 --split test 평가, 저장소 안 지우기(보호 경로 밖·worktree
  폴더 자체·필터로 거르는 find), xargs rm, git branch -D·stash drop·clear. 명령을 해석하지 못했는데 위험 단어가 있거나,
  검사 중 예외가 나거나, 훅 입력을 읽지 못하면 확인 창(Bash가 조용히 통과하지 않게)
- Read·Grep 막기: .env(비밀) — Grep은 glob 인자도 본다. 값이 대화에 남지 않게
- Edit·Write 확인 창: 안전장치(.claude/settings*.json·.claude/hooks/)와 관문 기준(evaluation/metrics.py GATE_*) 편집
- Edit·Write 막기: 생성물(LLM 캐시·DB·생성기가 쓰는 정답 JSON)은 손으로 고치지 않는다 — make data·make llm-import가 쓴다.
  backend/가 ground_truth·generator를 import하는 편집, 자동 개선 코드(evaluation/improve*)가 test 분할을 읽는 편집도 막는다
  — CLAUDE.md 절대 규칙. 편집 내용만 보는 얕은 검사이고 backend 쪽 근거는 tests/test_no_ground_truth_import.py가 지킨다
- --readonly(리뷰·문서 점검 에이전트): 저장소를 바꾸는 Bash(쓰는 git·make·rm·저장소 안 리디렉션 등)를 막는다.
  명령 문자열만 보므로 프로그램이 속에서 쓰는 것까지는 못 막는다 — 부르는 쪽이 git status 전후를 비교한다
- Bash 공통: bash·sh·zsh -c '…'와 eval은 안쪽 명령을 같은 규칙으로 본다(스크립트 파일·python -c 속은 못 본다)
PostToolUse
- 정형 생성 단계·생성 프롬프트를 고치면 캐시 미스 확인을 알린다 — CLAUDE.md 절대 규칙

입력은 훅 JSON(stdin). 막기·알림은 exit 2 + 이유(stderr — Claude에게 전달), 확인 창은 JSON(permissionDecision ask —
이유는 사용자가 읽는 한 줄 '[머리표] 무엇 · 걸린 부분: 단순 명령 하나 · 허용하면 …', _prompt),
아니면 exit 0. 표준 라이브러리만 쓴다.
"""

from __future__ import (
    annotations,
)  # 시스템 파이썬(3.9)으로 돌아도 주석 문법 때문에 죽지 않게 — 죽으면 훅이 조용히 꺼진다

import fnmatch
import glob
import itertools
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

GENERATED = (  # 저장소 기준 경로 — 디렉터리는 끝에 /
    "generator/llm/cache/",
    "generator/llm/jobs/",
    "generator/llm/results/",
    "data/llm_calls_gen.jsonl",
    "data/source.sqlite",
    "data/app.sqlite",
    "ground_truth/truth.sqlite",
    "ground_truth/call_labels.json",
    "ground_truth/doc_meta.json",
)
# 고친 뒤 캐시 미스 확인을 알릴 파일 — 생성 결과를 바꾸지 않는 생성기 파일은 뺀다
STAGE_FILES = re.compile(
    r"generator/(?!paths\.py|__main__\.py|__init__\.py)[^/]+\.py|prompts/gen_[^/]+\.md"
    r"|config/(?:generator\.yaml|holidays\.json|forbidden_topics\.json)|shared/source_schema\.sql"
)
READONLY_GIT = {
    "status",
    "diff",
    "log",
    "show",
    "blame",
    "rev-parse",
    "merge-base",
    "ls-files",
    "grep",
    "cat-file",
    "worktree",
    "branch",
    "describe",
    "shortlog",
}
READONLY_MAKE = {"test", "lint", "verify"}
_MAKE_OPTS_WITH_ARG = {"-C", "-f", "-j", "-I", "-o", "-W", "--directory", "--file"}
_PREFIX = {"env", "command", "sudo", "nice", "time", "exec"}  # 앞붙임 — 그 뒤가 진짜 명령
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}  # -c '…'로 감싸면 안쪽 명령을 본다
_REDIRECTS = {">", ">>", "&>", "&>>", ">|"}
ROOT = Path(__file__).resolve().parents[2]
# 히어독은 본문만 지우고 여는 줄은 남긴다(여는 줄 뒤의 '&& git push'·'> 파일'도 검사하게)
_HEREDOC = re.compile(r"(<<-?\s*['\"]?(\w+)['\"]?[^\n]*)\n.*?\n\2[ \t]*(?=\n|$)", re.S)
_SEPARATORS = {"&&", "||", ";", "|", "&", "\n", "(", ")"}
_BACKEND_IMPORT = re.compile(r"^\s*(?:from|import)\s+(?:ground_truth|generator)\b", re.M)
_TEST_SPLIT = re.compile(r"split\s*={1,2}\s*['\"]test['\"]")


def _segments(command: str) -> list[list[str]]:
    """명령 문자열 → 단순 명령들(토큰 목록). 히어독 본문은 지운다(커밋 메시지의 따옴표가 파싱을 깨지 않게)"""
    lexer = shlex.shlex(_HEREDOC.sub(r"\1", command), posix=True, punctuation_chars=";&|()<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    segments, current = [], []
    for token in lexer:
        if token in _SEPARATORS or token and set(token) <= set(";&|()\n"):  # '' 인자는 구분자 아님
            segments.append(current)
            current = []
        else:
            current.append(token)
    out = []
    for seg in segments + [current]:
        tokens = _unprefix(seg)
        if seg and Path(seg[0]).name == "env" and (not tokens or tokens[0].startswith("-")):
            # 'env'·'env -0' — 뒤에 실행할 명령 없이 변수 전부 출력
            tokens = ["env"]
        if (inner := _shell_inner(tokens)) is not None:
            out.extend(_segments(inner))  # 감싼 명령도 같은 규칙으로
        elif tokens:
            out.append(tokens)
    return out


def _shell_inner(tokens: list[str]) -> str | None:
    """bash·sh·zsh -c '…'·eval … → 안쪽 명령 문자열(감싼 명령이 아니면 None)"""
    if not tokens:
        return None
    prog = Path(tokens[0]).name
    if prog == "eval":
        return " ".join(tokens[1:])
    if prog not in _SHELLS:
        return None
    for i, t in enumerate(tokens[1:], 1):
        if not t.startswith("-"):
            return None  # 스크립트 파일 — 문자열로 볼 수 없다
        if not t.startswith("--") and "c" in t[1:]:
            return tokens[i + 1] if i + 1 < len(tokens) else None
    return None


def _unprefix(tokens: list[str]) -> list[str]:
    """'env A=1 sudo git add .' → ['git', 'add', '.'] — 앞붙임과 변수 대입을 뗀다"""
    i = 0
    while i < len(tokens) and (
        Path(tokens[i]).name in _PREFIX or re.fullmatch(r"\w+=.*", tokens[i])
    ):
        i += 1
    return tokens[i:]


def _git_subcommand(tokens: list[str]) -> tuple[str, list[str]] | None:
    """['git', '-C', 'x', 'add', 'a'] → ('add', ['a'])"""
    if not tokens or Path(tokens[0]).name != "git":
        return None
    i = 1
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 2 if tokens[i] in ("-C", "-c") else 1
    return (tokens[i], tokens[i + 1 :]) if i < len(tokens) else None


def _bulk_add(args: list[str], cwd: Path) -> str | None:
    for a in args:
        if a in ("-A", "--all", "-u", "--update", "--no-ignore-removal") or re.fullmatch(
            r"-[a-zA-Z]*[Au][a-zA-Z]*", a
        ):
            return a
        if a == "--":
            continue
        # 글롭 문자가 있어도 그 이름의 파일이 있으면 파일 이름이다(예: Next.js 동적 경로 app/[screen]/page.tsx)
        glob = any(c in a for c in "*?[") and not (cwd / a).is_file()
        if not a.startswith("-") and (a == "." or a.startswith(":") or glob or (cwd / a).is_dir()):
            return a
    return None


def _repo_root(cwd: Path) -> Path:
    """명령이 도는 저장소 — worktree에서 커밋하면 그 worktree(원래 폴더의 다른 세션 작업 중 파일이 막지 않게),
    저장소 밖이면 훅이 놓인 저장소"""
    r = subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], capture_output=True, text=True
    )
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 else ROOT


def lint_failure(root: Path) -> str | None:
    """make lint가 실패하면 출력 끝부분, 통과면 None"""
    r = subprocess.run(["make", "-s", "lint"], cwd=root, capture_output=True, text=True)
    return None if r.returncode == 0 else (r.stdout + r.stderr).strip()[-1500:]


def check_bash(command: str, cwd: Path) -> str | None:
    try:
        segments = _segments(command)
    except ValueError:  # 따옴표가 안 닫힌 명령 — 판단하지 않고 통과
        return None
    commit = None
    for tokens, here in _walk(segments, cwd):
        sub = _git_subcommand(tokens)
        if not sub:
            continue
        name, args = sub
        if name == "add" and (bad := _bulk_add(args, _git_cwd(tokens, here))):
            return (
                f"`git add {bad}`는 막혀 있다 — git add는 파일 이름으로만(CLAUDE.md '깃'). "
                "바꾼 파일을 이름으로 나열하라(예: git add a.py b.md)"
            )
        if name == "commit":
            if any(a == "--all" or re.fullmatch(r"-[a-zA-Z]*a[a-zA-Z]*", a) for a in args):
                return "`git commit -a`는 막혀 있다 — 파일 이름으로 git add 한 뒤 커밋하라(CLAUDE.md '깃')"
            commit = _git_cwd(tokens, here)
    for tokens, here in _walk(segments, cwd):
        if why := _danger(tokens, here, command):
            return why
    if commit and (out := lint_failure(_repo_root(commit))):
        return (
            "커밋 전 `make lint` 실패(스테이징한 파일만이 아니라 작업 트리 전체 — CI와 같은 범위) — "
            f"`make fmt`로 정리하고 남은 것을 고친 뒤 커밋하라:\n{out}"
        )
    return None


def _in_repo(path: str, cwd: Path) -> bool:
    p = _expand(path, cwd)  # $TMPDIR·~를 펼친다 — 임시 폴더 사본이 저장소 안으로 보이지 않게
    try:
        p.relative_to(ROOT.resolve())
    except ValueError:
        return False
    return True


def _prompt(why: tuple[str, str, str], part: str = "") -> str:
    """확인 창 이유 한 줄 — '[머리표] 무엇 · 걸린 부분: … · 허용하면 …'(긴 명령에서 걸린 곳을 찾지 않게)"""
    tag, what, then = why
    if len(part) > PROMPT_PART_MAX:
        part = part[: PROMPT_PART_MAX - 1] + "…"
    return " · ".join(
        [f"[{tag}] {what}", *([f"걸린 부분: {part}"] if part else []), f"허용하면 {then}"]
    )


def check_ask(command: str, cwd: Path = ROOT) -> str | None:
    """사용자 확인을 받을 명령이면 이유(_prompt 한 줄)"""
    try:
        segments = _segments(command)
    except ValueError:  # 해석 못 하는 명령 — 위험 단어가 있으면 그냥 넘기지 않는다
        if _RISKY_WORDS.search(command):
            return _prompt(
                (
                    "해석 못 함",
                    "따옴표가 안 닫혀 명령을 읽지 못했다",
                    "무엇을 할지 모른 채 실행된다(지우기·비밀·push 단어가 있음)",
                )
            )
        return None
    for tokens, here in _walk(segments, cwd):
        sub = _git_subcommand(tokens)
        text = " ".join(tokens)
        if why := _ask_why(tokens, sub, here, text):
            return _prompt(why, text)
    return None


def _ask_why(tokens: list[str], sub, here: Path, text: str) -> tuple[str, str, str] | None:
    if sub and sub[0] == "push" and (why := _push_risk(sub[1], _git_cwd(tokens, here))):
        return why
    if why := _ask_more(tokens, sub, here):
        return why
    if tokens[:3] == ["gh", "pr", "merge"]:
        return ("되돌리기 어려움", "PR 머지", "main에 들어간다")
    if (
        Path(tokens[0]).name == "make"
        and "llm-fill-api" in tokens
        or "fill-api" in tokens
        and any("llm_jobs" in t for t in tokens)
    ):
        return (
            "과금",
            "LLM API 채우기(배치)",
            "하루 상한 밖으로 비용이 나간다 — 건수는 make data가 알려 준다(기능 정의 §7)",
        )
    if "evaluation" in text and re.search(r"--split[ =]test\b", text):
        return (
            "test 평가",
            "--split test 평가",
            "묶음마다 한 번뿐인 test를 쓴다(평가 설계 §1 원칙 2)",
        )
    return None


def check_readonly(command: str, cwd: Path) -> str | None:
    """읽기 전용 에이전트가 저장소를 바꾸는 명령이면 이유"""
    try:
        segments = _segments(command)
    except ValueError:
        return "명령을 해석할 수 없다(따옴표) — 읽기 전용 에이전트는 해석되는 명령만 돌린다"
    for tokens in segments:
        why = _readonly_problem(tokens, cwd)
        if why:
            return why
    return None


def _make_targets(args: list[str]) -> list[str]:
    """make 인자 → 대상(옵션·변수 대입·리디렉션 뒤는 뺀다)"""
    targets, skip = [], False
    for i, a in enumerate(args):
        nxt = args[i + 1] if i + 1 < len(args) else ""
        if skip:
            skip = False
        elif (
            any(c in a for c in "<>") or a.isdigit() and any(c in nxt for c in "<>")
        ):  # '2>&1'의 2
            break
        elif a in _MAKE_OPTS_WITH_ARG:
            skip = True
        elif not a.startswith("-") and "=" not in a:
            targets.append(a)
    return targets


def _readonly_problem(tokens: list[str], cwd: Path) -> str | None:
    for i, t in enumerate(tokens):  # 저장소 안을 향한 리디렉션
        if t in _REDIRECTS and i + 1 < len(tokens) and tokens[i + 1] != "/dev/null":
            if _in_repo(tokens[i + 1], cwd):
                return f"저장소 안 파일로 리디렉션({tokens[i + 1]})"
    names = [Path(t).name for t in tokens]
    sub = _git_subcommand(tokens)
    if sub:
        name, args = sub
        if name not in READONLY_GIT:
            return f"git {name}은 저장소를 바꾼다"
        if name == "branch":
            listing = any(
                a in ("--list", "-l", "--contains", "--merged", "--no-merged") for a in args
            )
            changing = any(
                a
                in (
                    "-d",
                    "-D",
                    "-m",
                    "-M",
                    "-c",
                    "-C",
                    "-f",
                    "--delete",
                    "--move",
                    "--copy",
                    "--force",
                    "-u",
                    "--set-upstream-to",
                    "--unset-upstream",
                )
                for a in args
            )
            if changing or not listing and any(not a.startswith("-") for a in args):
                return "git branch는 목록 보기만(만들기·삭제·이름 변경 금지)"
        if name == "worktree":
            if args[:1] not in (["list"], ["add"]):
                return "git worktree는 list와 저장소 밖 add만"
            paths = [a for a in args[1:] if not a.startswith("-")]
            if args[:1] == ["add"] and (not paths or _in_repo(paths[0], cwd)):
                return "git worktree add는 저장소 밖(임시 폴더)에만"
        return None
    prog = names[0]
    if prog == "make":
        targets = _make_targets(tokens[1:])
        if not targets or any(t not in READONLY_MAKE for t in targets):
            return f"{' '.join(tokens)} — 읽기 전용 에이전트는 make test·lint·verify만(대상 없는 make는 make data)"
    if any(n in ("rm", "mv", "rmdir") for n in names):
        return "rm·mv는 쓰지 않는다"
    if prog == "find" and any(t in ("-delete", "-exec", "-execdir") for t in tokens):
        return "find -delete·-exec는 쓰지 않는다"
    if prog in ("cp", "touch", "mkdir", "tee", "ln") and any(
        _in_repo(a, cwd) for a in tokens[1:][-1:] if not a.startswith("-")
    ):
        return f"{prog}로 저장소 안에 쓴다 — 쓰기는 임시 폴더에만"
    if prog == "sed" and any(a.startswith("-i") or a.startswith("--in-place") for a in tokens[1:]):
        return "sed -i는 파일을 고친다"
    if (
        prog == "uv"
        and len(tokens) > 1
        and tokens[1] in ("add", "remove", "sync", "lock", "pip", "venv")
    ):
        return f"uv {tokens[1]}는 의존성·환경을 바꾼다"
    if "ruff" in tokens and ("format" in tokens and "--check" not in tokens or "--fix" in tokens):
        return "ruff 정리는 파일을 고친다(검사는 ruff check·ruff format --check)"
    text = " ".join(tokens)
    if ("evaluation.eval" in text or "evaluation/eval.py" in text) and not any(
        t == "--reports-dir" or t.startswith("--reports-dir=") for t in tokens
    ):
        return "eval.py는 --reports-dir로 임시 폴더를 줘야 한다(기본은 저장소 reports/)"
    # 생성기·LLM 작업 명령(python -m generator…·generator/*.py 실행) — 파이썬으로 돌릴 때만(cat·grep 같은 읽기는 아님)
    module = next((tokens[i + 1] for i, t in enumerate(tokens[:-1]) if t == "-m"), "")
    runs_python = any(n.startswith("python") for n in names)
    runs_generator = runs_python and (
        module.split(".")[0] == "generator"
        or any(t.startswith("generator/") and t.endswith(".py") for t in tokens)
    )
    verify_only = module == "generator.verify" or any(
        t.startswith("generator/verify/") for t in tokens
    )
    elsewhere = module == "generator" and all(  # 저장소 밖에 생성 — DB·작업 파일이 그 옆으로 간다
        _flag_outside(tokens, flag, cwd) for flag in ("--db", "--truth")
    )
    if runs_generator and not verify_only and not elsewhere:
        return "생성기·LLM 작업 명령은 DB·작업·캐시를 쓴다(make data 금지 — 생성은 --db·--truth를 임시 폴더로)"
    return None


def _flag_outside(tokens: list[str], flag: str, cwd: Path) -> bool:
    """--flag 값(--flag=x 또는 --flag x)이 있고 저장소 밖인지"""
    for i, t in enumerate(tokens):
        if t.startswith(flag + "="):
            return not _in_repo(t.split("=", 1)[1], cwd)
        if t == flag and i + 1 < len(tokens):
            return not _in_repo(tokens[i + 1], cwd)
    return False


# ── 지우기·되돌리기·비밀·정답 보호 ────────────────────────────
PROTECTED = (
    "data",
    "ground_truth",
    ".env",
    ".git",
    ".claude",
)  # 지우면 안 되는 저장소 경로(사람 판정·정답·비밀)
WORKTREES = re.compile(
    r"\.claude/worktrees/[^/]+(?:/(.+))?"
)  # 저장소 안 worktree — 그 안을 저장소처럼 본다
TRUTH_WRITE = re.compile(r"(?:^|/)(?:ground_truth(?:/.+)?|data/[^/]+\.sqlite[^/]*)$")
_TEMP_ROOTS = (
    "/tmp",
    "/private/tmp",
)  # 그 밖은 $TMPDIR(tempfile.gettempdir()) — /var/folders 전체는 아니다
_SECRET_READERS = {
    "cat", "less", "more", "head", "tail", "grep", "egrep", "fgrep", "rg", "sed", "awk", "source",
    ".", "cp", "mv", "base64", "xxd", "od", "strings", "open", "code", "vi", "vim", "nano", "bat",
    "sort", "cut", "tr", "diff", "cmp", "comm", "paste", "nl", "tac", "rev", "fold", "column",
}  # fmt: skip
_SECRET_WORDS = {"KEY", "TOKEN", "SECRET", "PASSWORD", "APIKEY"}
_VAR = re.compile(r"\$\{?(\w+)\}?")
_WRITE_SQL = re.compile(  # 문장 시작(줄 처음·; 뒤·WITH 절의 ) 뒤·따옴표 시작)의 쓰기 키워드만 — 문자열 안 'DELETE'는 아니다
    r"(?:^|;|\)|[\s(]['\"])\s*(?:UPDATE|INSERT|DELETE|DROP|ALTER|CREATE|REPLACE|VACUUM|ATTACH|DETACH)\b",
    re.I | re.M,
)
_DOT_SQL = re.compile(r"(?:^|\s|['\"])\.(?:read|import|restore|clone)\b", re.M)
_PRAGMA_SET = re.compile(r"\bpragma\s+\w+\s*=", re.I)
_SQLITE_VALUE_OPTS = ("-cmd", "-separator", "-newline", "-nullvalue", "-init", "-vfs", "-maxsize")
_PIPE_INTO_SQLITE = re.compile(r"(?:^|[;&|\n(]\s*)([\w./-]+)[^|;&\n]*\|\s*(?:\S*/)?sqlite3\b")
_FIND_FILTERS = {
    "-name",
    "-iname",
    "-path",
    "-ipath",
    "-regex",
    "-iregex",
    "-type",
    "-newer",
    "-mtime",
}
_RISKY_WORDS = re.compile(
    r"\brm\b|\.env|ground_truth|data/|sqlite3|reset|clean|push|truncate|unlink"
)
# CLAUDE.md "(과금)" make 대상 — 무료인 judge(로컬 Qwen)·eval·data·test 등은 뺀다
PAID_MAKE = {
    "classify", "blind-judge", "gold-arbitrate", "hard-label", "prompt-gate", "followups",
    "action-label", "followup-eval", "prep", "report", "claim-check", "claim-omission",
    "claim-evidence", "claim-qa-draft", "claim-appeal-dev", "claim-omission-cases",
    "claim-omission-synth", "claim-check-sample", "claim-check-dev", "recon", "expense-ai",
    "index", "nightly", "gate", "trust-eval", "improve", "assistant-eval",
}  # fmt: skip
# 같은 일을 모듈로 직접 부를 때 — 값은 과금 하위 명령(None이면 전부). --plan·--stats·--help·-h는 무료
# (모듈마다 main이 다른 일보다 먼저 argparse로 인자를 읽어 --help면 설명만 찍고 끝난다)
PAID_MODULES = {
    "backend.nightly": None, "backend.prep": None, "backend.report": None,
    "backend.claim_check": None, "backend.claim_omission": None, "backend.claim_evidence": None,
    "backend.recon_agent": None, "backend.expense_agent": None, "backend.followup": None,
    "backend.rag_index": None, "backend.classify_batch": {"classify", "blind"},
    "evaluation.hard_labels": {"label"}, "evaluation.call_actions": {"label"},
    "evaluation.followup": {"run"}, "evaluation.gold_audit": {"arbitrate"},
    "evaluation.prompt_gate": None, "evaluation.claim_qa": {"draft"},
    "evaluation.claim_omission": {"cases", "synth", "real"},
    "evaluation.claim_check": {"sample", "run"}, "evaluation.claim_appeal": {"run"},
    "evaluation.input_trust": {"run"}, "evaluation.langgraph_recon": {"run"},
    "evaluation.improve": {"run", "hard-check"}, "evaluation.assistant": {"run"},
}  # fmt: skip
FREE_FLAGS = {"--plan", "--stats", "--help", "-h"}
# 확인 창에 모듈 이름 대신 보일 말
MODULE_LABELS = {
    "backend.nightly": "야간 일괄 실행", "backend.prep": "진료 준비표 제안",
    "backend.report": "경영 리포트 해석", "backend.claim_check": "청구 점검",
    "backend.claim_omission": "청구 누락 점검", "backend.claim_evidence": "청구 근거 문장",
    "backend.recon_agent": "일계표 불일치 조사", "backend.expense_agent": "지출 증빙 탐색",
    "backend.followup": "통화 후속 처리안", "backend.rag_index": "검색 색인(임베딩)",
    "backend.classify_batch": "통화 분류·블라인드 일괄", "evaluation.hard_labels": "어려운 통화 정답 라벨",
    "evaluation.call_actions": "처리 정답 라벨", "evaluation.followup": "후속 처리 평가",
    "evaluation.gold_audit": "골드 점검 중재", "evaluation.prompt_gate": "프롬프트 관문 판정",
    "evaluation.claim_qa": "청구 기준 질문 초안", "evaluation.claim_omission": "청구 누락 합성 평가",
    "evaluation.claim_check": "청구 점검 평가", "evaluation.claim_appeal": "재심 사유서 초안 평가",
    "evaluation.input_trust": "입력 신뢰 경계 평가", "evaluation.langgraph_recon": "랭그래프 비교 실행",
    "evaluation.improve": "자동 개선(개선 에이전트·관문)", "evaluation.assistant": "AI 비서 시나리오 평가",
}  # fmt: skip
PROMPT_PART_MAX = 80  # 확인 창의 "걸린 부분" 글자 수
PAID_THEN = "LLM 호출 비용이 나간다"
_PYTHON = re.compile(r"python[\d.]*")
_SCRIPT = re.compile(r"(?:\./)?(backend|evaluation)/(\w+)\.py")
_UV_VALUE_OPTS = {
    "--env-file", "--group", "--only-group", "--no-group", "--extra", "--with",
    "--with-requirements", "--python", "-p", "--package", "--directory", "--project", "--index",
}  # fmt: skip


def is_env_file(path: str) -> bool:
    """.env·.env.local 같은 비밀 파일 — 견본(.example·.sample·.template)은 아니다"""
    name = Path(path).name
    return (name == ".env" or name.startswith(".env.")) and not name.endswith(
        (".example", ".sample", ".template")
    )


def env_like(path: str) -> bool:
    """비밀 파일이거나 비밀 파일에 맞는 글롭(.env*·.e*·*.local)"""
    name = Path(path).name
    if is_env_file(name):
        return True
    return any(c in name for c in "*?[") and any(
        fnmatch.fnmatch(e, name) for e in (".env", ".env.local")
    )


def _expand(path: str, cwd: Path) -> Path:
    path = path.replace("${TMPDIR}", "$TMPDIR")
    if path.startswith("$TMPDIR") and "TMPDIR" not in os.environ:
        path = tempfile.gettempdir() + path[len("$TMPDIR") :]
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    return (p if p.is_absolute() else cwd / p).resolve()


def _is_temp(path: str, cwd: Path) -> bool:
    """임시 폴더 안(루트 자체는 아니다) — 펼쳐서 판정해 $TMPDIR/../.. 탈출을 막는다"""
    p = _expand(path, cwd)
    roots = {Path(r).resolve() for r in (*_TEMP_ROOTS, tempfile.gettempdir())}
    return any(p != r and p.is_relative_to(r) for r in roots)


def _repo_rel(p: Path) -> str | None:
    """저장소 기준 경로 — .claude/worktrees/<이름>/ 안이면 그 worktree 기준. 밖이면 None"""
    root = ROOT.resolve()
    if not p.is_relative_to(root):
        return None
    rel = p.relative_to(root).as_posix()
    m = WORKTREES.fullmatch(rel)
    if m:
        return m.group(1) or ""  # "" = worktree 폴더 자체
    return rel


def _risk_of(p: Path) -> str | None:
    """'block'(보호·저장소 밖·저장소 통째) · 'ask'(저장소 안 나머지) · None(임시 폴더)"""
    root = ROOT.resolve()
    if p == root or root.is_relative_to(p):
        return "block"
    rel = _repo_rel(p)
    if rel is None:
        return "block"
    if rel == "":
        return "ask"
    if any(rel == q or rel.startswith(q + "/") for q in PROTECTED) or is_env_file(rel):
        return "block"
    return "ask"


def _rm_risk(target: str, cwd: Path) -> str | None:
    if target in ("/", "~", "~/", "..", "$HOME", "${HOME}", "$HOME/", "${HOME}/"):
        return "block"
    if target == "$" or "$(" in target or "`" in target:  # 명령 치환 — 무엇을 지울지 모른다
        return "block"
    if any(c in target for c in "*?["):
        pattern = str(_expand(os.path.dirname(target) or ".", cwd) / os.path.basename(target))
        hits = glob.glob(pattern)
        if Path(target).name.startswith("*"):  # '*'·'./*'는 숨김 파일(.env·.git)까지 지운다고 본다
            hits += glob.glob(str(Path(pattern).parent / ("." + Path(target).name)))
        risks = [_risk_of(Path(h).resolve()) for h in hits if not _is_temp(h, cwd)]
        return (
            "block" if "block" in risks else "ask" if risks or not _is_temp(target, cwd) else None
        )
    if _is_temp(target, cwd):
        return None
    return _risk_of(_expand(target, cwd))


def _positional(args: list[str], with_value: tuple[str, ...] = ()) -> list[str]:
    """옵션과 그 값을 뺀 인자"""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in with_value:
            skip = True
        elif a == "--" or a.startswith("-") and a != "-":
            continue
        else:
            out.append(a)
    return out


def _target_dir(tokens: list[str]) -> str | None:
    """cp·mv·install·ln의 대상 폴더 옵션(-t X·--target-directory X·--target-directory=X)"""
    for i, t in enumerate(tokens):
        if t.startswith("--target-directory="):
            return t.split("=", 1)[1]
        if t in ("-t", "--target-directory") and i + 1 < len(tokens):
            return tokens[i + 1]
    return None


def _moved(tokens: list[str]) -> tuple[str, list[str]]:
    """(대상, 원본들) — 대상 폴더 옵션이 있으면 위치 인자가 모두 원본"""
    args = _positional(tokens[1:], ("-t", "--target-directory", "-m", "-o", "-g", "-S"))
    target = _target_dir(tokens)
    if target is not None:
        return target, args
    return (args[-1] if args else ""), args[:-1]


def _delete_risk(tokens: list[str], target: str, cwd: Path) -> str | None:
    """지우기 판정 — find가 -name 같은 필터로 걸러 지우면 저장소 통째가 아니라 확인으로 낮춘다"""
    risk = _rm_risk(target, cwd)
    if (
        risk == "block"
        and Path(tokens[0]).name == "find"
        and _FIND_FILTERS & set(tokens)
        and _expand(target, cwd) == ROOT.resolve()
    ):
        return "ask"
    return risk


def _rm_targets(tokens: list[str]) -> list[str]:
    prog = Path(tokens[0]).name
    if prog in ("rm", "rmdir", "unlink", "shred"):
        return _positional(tokens[1:])
    if prog == "truncate":
        return _positional(tokens[1:], ("-s", "--size", "-r", "--reference"))
    if prog == "find" and any(t in ("-delete", "-exec", "-execdir") for t in tokens):
        return [t for t in tokens[1:] if not t.startswith(("-", "(", "!"))][:1] or ["."]
    return []


def _truth_target(path: str, cwd: Path) -> bool:
    """정답(ground_truth/)·DB(data/*.sqlite)를 가리키는지 — sqlite URI(file:…?mode=)도"""
    if path.startswith("file:"):
        path = path[5:].split("?", 1)[0]
    rel = _repo_rel(_expand(path, cwd))
    return bool(rel and TRUTH_WRITE.search(rel))


def _writes_sql(text: str) -> bool:
    return bool(_WRITE_SQL.search(text) or _DOT_SQL.search(text) or _PRAGMA_SET.search(text))


def _shorts(args: list[str]) -> str:
    """묶인 짧은 옵션 글자들 — '-SW' → 'SW'"""
    return "".join(a[1:] for a in args if re.fullmatch(r"-[A-Za-z]+", a))


def _holds_secret(p: Path) -> bool:
    """재귀 검색 대상이 비밀 파일을 품는지 — 저장소 루트·그 위, 또는 바로 안에 .env*가 있는 폴더"""
    root = ROOT.resolve()
    if p == root or root.is_relative_to(p):
        return True
    return p.is_dir() and any(is_env_file(c.name) for c in p.iterdir())


def _wide_path(a: str, cwd: Path) -> bool:
    """폴더·작업 트리 전체를 가리키는 경로 — '.'·폴더·pathspec 매직(:/)·파일이 아닌 글롭"""
    glob_ = any(c in a for c in "*?[") and not (cwd / a).is_file()
    return a == "." or a.startswith(":") or glob_ or (cwd / a).is_dir()


def _git_danger(name: str, args: list[str], cwd: Path) -> str | None:
    if name == "clean" and not any(a in ("-n", "--dry-run") for a in args):
        return "git clean은 추적 안 하는 작업(.superpowers 등)과 -x면 .env·DB까지 지운다 — 사용자에게 맡겨라"
    if name == "reset" and "--hard" in args:
        return "git reset --hard는 커밋 안 한 작업을 날린다 — 사용자에게 맡겨라"
    shorts = _shorts(args)
    wide = "폴더·작업 트리 전체를 되돌린다 — 파일 이름으로 하거나 사용자에게 맡겨라"
    if name == "restore":
        staged = "--staged" in args or "S" in shorts
        worktree = "--worktree" in args or "W" in shorts
        paths = _positional(args, ("-s", "--source"))
        if (worktree or not staged) and any(_wide_path(a, cwd) for a in paths):
            return f"git restore로 {wide}"
    if name in ("checkout", "switch") and (
        any(a in ("--force", "--discard-changes") for a in args) or "f" in shorts
    ):
        return (
            f"git {name} 강제 — 커밋 안 한 작업을 날린다(reset --hard와 같다) — 사용자에게 맡겨라"
        )
    if name == "checkout" and not any(a in ("-b", "-B", "--orphan") for a in args):
        paths = args[args.index("--") + 1 :] if "--" in args else _positional(args)
        if any(_wide_path(a, cwd) for a in paths):
            return f"git checkout으로 {wide}"
    return None


def _secret_danger(tokens: list[str], prog: str, cwd: Path) -> str | None:
    if prog == "env" and len(tokens) == 1 or prog == "printenv":
        return "환경 변수 출력은 API 키가 대화에 남는다 — 하지 않는다"
    if (
        prog == "set"
        and len(tokens) == 1
        or (
            prog in ("export", "declare", "typeset") and all(a.startswith("-") for a in tokens[1:])
        )
    ):
        return "변수 전체 출력은 API 키가 대화에 남는다 — 하지 않는다"
    if prog in ("echo", "printf", "print") and any(
        _SECRET_WORDS & set(name.upper().split("_"))
        for t in tokens[1:]
        for name in _VAR.findall(t)
    ):
        return "키 변수를 출력하지 않는다(값이 대화에 남는다)"
    for i, t in enumerate(tokens):
        if t == "<" and i + 1 < len(tokens) and env_like(tokens[i + 1]):
            return ".env(API 키)를 읽지 않는다"
    if prog in ("cp", "mv", "install") and env_like(_moved(tokens)[0]):
        return ".env(API 키 파일)에 쓰지 않는다 — 키 설정은 사용자에게 맡겨라"
    if prog in _SECRET_READERS:
        for i, t in enumerate(tokens[1:], 1):
            if tokens[i - 1] == "--env-file" or t.startswith("--exclude"):
                continue
            value = (
                t.split("=", 1)[1] if t.startswith(("--include=", "--glob=", "--iglob=")) else t
            )
            if value.startswith("!"):  # rg -g '!…'는 빼고 찾기 — .env를 읽지 않는다
                continue
            if env_like(value):
                return ".env(API 키)를 읽지 않는다 — 키가 필요하면 uv run --env-file .env로 실행만 한다"
    if prog in ("grep", "egrep", "fgrep"):
        recursive = any(a in ("--recursive", "--dereference-recursive") for a in tokens) or any(
            c in _shorts(tokens[1:]) for c in "rR"
        )
        patterns = [a.split("=", 1)[-1] for a in tokens[1:] if a.startswith("--exclude")]
        excluded = all(  # .env와 .env.local을 모두 빼야 뺀 것
            any(fnmatch.fnmatch(e, v) for v in patterns) for e in (".env", ".env.local")
        )
        if recursive and not excluded:
            pattern_given = any(a in ("-e", "-f") or a.startswith(("-e", "-f")) for a in tokens)
            paths = _positional(tokens[1:], ("-e", "-f", "-m", "-A", "-B", "-C"))
            paths = paths if pattern_given else paths[1:]
            if any(_holds_secret(_expand(p, cwd)) for p in paths or ["."]):
                return (
                    "grep -r이 .env(API 키)까지 읽는다 — rg(숨김·gitignore 파일 기본 제외)를 쓰거나 "
                    "--exclude='.env*'를 붙여라"
                )
    if prog == "rg":
        shorts = _shorts(tokens[1:])
        no_ignore = shorts.count("u") >= 1 or any(a.startswith("--no-ignore") for a in tokens)
        hidden = shorts.count("u") >= 2 or "--hidden" in tokens or "." in shorts
        if no_ignore and hidden:
            pattern_given = any(a in ("-e", "-f") or a.startswith(("-e", "-f")) for a in tokens)
            paths = _positional(tokens[1:], ("-e", "-f", "-g", "--glob", "-t", "-T", "-m"))
            paths = paths if pattern_given else paths[1:]
            if any(_holds_secret(_expand(p, cwd)) for p in paths or ["."]):
                return "rg에 숨김·무시 파일까지(-uu·--hidden --no-ignore) 읽게 하면 .env(API 키)도 읽는다 — 빼고 검색하라"
    return None


def _write_danger(tokens: list[str], prog: str, cwd: Path, raw: str) -> str | None:
    msg = f"`{' '.join(tokens)}` — 정답(ground_truth/)·DB(data/*.sqlite)를 손으로 고치지 않는다"
    for i, t in enumerate(tokens):  # 정답·DB로 리디렉션
        if t in _REDIRECTS and i + 1 < len(tokens) and _truth_target(tokens[i + 1], cwd):
            return msg
    if prog == "dd" and any(t.startswith("of=") and _truth_target(t[3:], cwd) for t in tokens[1:]):
        return msg
    if prog == "sed" and any(a.startswith(("-i", "--in-place")) for a in tokens[1:]):
        if any(_truth_target(t, cwd) for t in _positional(tokens[1:], ("-e", "-f"))):
            return msg
    if prog == "tee" and any(_truth_target(t, cwd) for t in _positional(tokens[1:])):
        return msg
    if prog in ("cp", "mv", "install", "ln"):
        dest, sources = _moved(tokens)
        if _truth_target(dest, cwd):
            return msg
        d = _expand(dest, cwd)
        if d.is_dir() and any(_truth_target(str(d / Path(src).name), cwd) for src in sources):
            return msg
    if prog == "sqlite3":
        args = [a for a in tokens[1:] if a not in ("<<", "<<-", "<<<")]
        positional = _positional(args, _SQLITE_VALUE_OPTS)
        db = positional[0] if positional else ""
        readonly = "-readonly" in args or "mode=ro" in db
        if db and _truth_target(db, cwd) and not readonly:
            sql = positional[1:] + [
                args[i + 1] for i, a in enumerate(args[:-1]) if a == "-cmd"
            ]  # -cmd 값도 SQL
            producer = _PIPE_INTO_SQLITE.search(raw)
            if (
                "<" in tokens
                or "-init" in args
                or (producer and Path(producer.group(1)).name not in ("echo", "printf"))
            ):  # 파일·다른 명령에서 SQL을 먹인다 — 내용을 모른다
                return f"{db}에 파일·명령 출력으로 SQL을 먹인다 — 정답·DB는 손으로 고치지 않는다(읽기만)"
            stdin_driven = not positional[1:] or any(t in ("<<", "<<-", "<<<") for t in tokens)
            if _writes_sql("\n".join(sql)) or stdin_driven and _writes_sql(raw):
                return f"{db}에 쓰는 SQL — 정답·DB는 손으로 고치지 않는다(읽기만)"
    return None


def _danger(tokens: list[str], cwd: Path, raw: str = "") -> str | None:
    """막을 명령이면 이유 — 지우기·되돌리기·비밀·정답 쓰기. raw는 원문(히어독 본문 검사용)"""
    prog = tokens[0] if tokens[0] == "." else Path(tokens[0]).name
    deleted = _rm_targets(tokens)
    if prog == "mv":  # 옮기면 원래 자리에서 사라진다
        deleted = _moved(tokens)[1]
    for t in deleted:
        risk = _delete_risk(tokens, t, cwd)
        if risk == "block" and (prog != "mv" or _repo_rel(_expand(t, cwd)) is not None):
            return (
                f"`{' '.join(tokens)}` — 보호 경로(data/·ground_truth/·.env·.git·.claude)나 저장소 밖을 지운다. "
                "data/app.sqlite에는 사람 판정·확정이 있어 다시 만들 수 없다 — 필요하면 사용자에게 맡겨라"
            )
    sub = _git_subcommand(tokens)
    if sub and (why := _git_danger(sub[0], sub[1], cwd)):
        return why
    return _secret_danger(tokens, prog, cwd) or _write_danger(tokens, prog, cwd, raw)


def _walk(segments: list[list[str]], cwd: Path):
    """(명령, 그 명령의 위치) — 같은 명령 안의 cd를 따라간다"""
    for tokens in segments:
        if Path(tokens[0]).name == "cd":
            cwd = _expand(tokens[1], cwd) if len(tokens) > 1 else Path.home()
            continue
        yield tokens, cwd


def _git_cwd(tokens: list[str], cwd: Path) -> Path:
    """git -C 경로가 있으면 그 저장소"""
    for i, t in enumerate(tokens[:-1]):
        if t == "-C":
            return _expand(tokens[i + 1], cwd)
    return cwd


def _current_branch(cwd: Path) -> str:
    r = subprocess.run(
        ["git", "-C", str(cwd), "branch", "--show-current"], capture_output=True, text=True
    )
    return r.stdout.strip()


def _push_risk(args: list[str], cwd: Path) -> tuple[str, str, str] | None:
    """main으로·강제 push만 확인 — feat 브랜치 push는 PR 흐름이라 그냥(사용자 결정)"""
    if any(a == "--force" or a.startswith("--force") for a in args) or "f" in _shorts(args):
        return ("되돌리기 어려움", "강제 push", "원격 기록을 덮어쓴다")
    if any(a in ("--mirror", "--all") for a in args):
        return ("되돌리기 어려움", "push --mirror·--all", "main까지 밖으로 나갈 수 있다")
    refspecs = _positional(args, ("-o", "--push-option", "--repo"))[1:]
    if any(r.startswith("+") for r in refspecs):
        return ("되돌리기 어려움", "강제 push(+refspec)", "원격 기록을 덮어쓴다")
    current = _current_branch(cwd)
    dests = [r.split(":")[-1] for r in refspecs] or [current]
    dests = [current if d in ("HEAD", "@") else d for d in dests]
    if any(d in ("main", "refs/heads/main") for d in dests):
        return ("되돌리기 어려움", "main으로 push", "main에 바로 올라간다 — 보통은 PR로 머지한다")
    return None


def _module(tokens: list[str]) -> tuple[str, list[str]]:
    """python이 실행하는 -m 모듈 또는 backend/x.py·evaluation/x.py 스크립트 → (모듈 이름, 그 뒤 인자).
    경로를 읽기만 하는 명령(grep·sed·cat·git diff·ruff·pytest)은 실행이 아니라 ("", [])"""
    if tokens[:2] == ["uv", "run"]:  # uv run 옵션(--env-file .env 등)을 건너뛴 첫 프로그램
        i = 2
        while i < len(tokens) and tokens[i].startswith("-"):
            i += 2 if tokens[i] in _UV_VALUE_OPTS else 1
        tokens = tokens[i:]
    if tokens and (m := _SCRIPT.fullmatch(tokens[0])):
        return f"{m.group(1)}.{m.group(2)}", tokens[1:]
    if not tokens or not _PYTHON.fullmatch(Path(tokens[0]).name):
        return "", []
    for i, t in enumerate(tokens[1:], 1):
        if t == "-m":
            return (tokens[i + 1], tokens[i + 2 :]) if i + 1 < len(tokens) else ("", [])
        if not t.startswith("-"):  # 첫 위치 인자가 스크립트(-c 코드는 경로가 아니다)
            m = _SCRIPT.fullmatch(t)
            return (f"{m.group(1)}.{m.group(2)}", tokens[i + 1 :]) if m else ("", [])
    return "", []


def _module_args(args: list[str]) -> list[str]:
    """모듈이 받는 인자 — '--'·리디렉션(>·<·>(…)) 앞까지. 다른 도구의 -h(sort -h)를 무료 플래그로 보지 않게"""
    return list(itertools.takewhile(lambda a: a != "--" and not set(a) & set("<>"), args))


def _ask_more(tokens: list[str], sub, cwd: Path) -> tuple[str, str, str] | None:
    """과금 명령·저장소 안 지우기·브랜치·stash 삭제"""
    prog = Path(tokens[0]).name
    if prog == "make":
        paid = [t for t in _make_targets(tokens[1:]) if t in PAID_MAKE]
        if paid and not (paid == ["nightly"] and any(t.startswith("PLAN=") for t in tokens)):
            free = " — 건수만 보려면 PLAN=1" if paid[0] == "nightly" else ""
            return ("과금", f"make {paid[0]}", PAID_THEN + free)
    module, args = _module(tokens)
    if module in PAID_MODULES and not FREE_FLAGS & set(_module_args(args)):
        subs = PAID_MODULES[module]
        if subs is None or subs & set(args):
            return (
                "과금",
                f"{MODULE_LABELS[module]}({module})",
                PAID_THEN + " — 인자 보기는 --help로 무료",
            )
    if any(_delete_risk(tokens, t, cwd) == "ask" for t in _rm_targets(tokens)):
        return ("지우기", "저장소 안 파일·폴더를 지운다", "커밋 안 한 변경은 되살릴 수 없다")
    if prog == "xargs" and any(
        Path(t).name in ("rm", "rmdir", "unlink", "shred") for t in tokens[1:]
    ):
        return ("지우기", "xargs rm", "무엇을 지울지 명령만으로 알 수 없다")
    if sub:
        name, args = sub
        if (
            name == "branch"
            and any(a in ("-D", "--delete", "-d") for a in args)
            and ("-D" in args or "--force" in args or "-f" in args)
        ):
            return ("되돌리기 어려움", "git branch -D", "머지 안 된 브랜치도 지운다")
        if name == "stash" and args[:1] in (["drop"], ["clear"]):
            return ("되돌리기 어려움", f"git stash {args[0]}", "맡겨 둔 작업이 사라진다")
    return None


def check_read(path: str, glob_: str | None = None) -> str | None:
    """Read·Grep으로 읽으면 안 되는 파일이면 이유 — Grep은 glob 인자도 본다"""
    if path and is_env_file(path) or glob_ and env_like(glob_):
        return ".env(API 키)는 읽지 않는다 — 값이 대화에 남는다"
    return None


def check_edit_ask(path: str, text: str) -> str | None:
    """확인을 받을 편집이면 이유 — 안전장치·관문 기준"""
    try:
        rel = Path(path).resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return None
    if re.fullmatch(r"\.claude/settings(?:\.local)?\.json|\.claude/hooks/.+", rel):
        return _prompt(("안전장치", "훅·권한 편집", "이 저장소의 막기·확인 규칙이 바뀐다"), rel)
    if rel == "evaluation/metrics.py" and "GATE_" in text:
        return _prompt(
            (
                "관문 기준",
                "GATE_* 수정",
                "프롬프트 관문 합격 기준이 바뀐다 — 사람만 고친다(평가 설계 §2.7)",
            ),
            rel,
        )
    return None


def check_post_edit(path: str) -> str | None:
    """고친 뒤 알릴 것 — 정형 생성 단계·생성 프롬프트"""
    try:
        rel = Path(path).resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return None
    if STAGE_FILES.fullmatch(rel):
        return (
            f"{rel}은 생성 결과에 영향을 준다 — 데이터 고정 뒤라 LLM 캐시가 미스 날 수 있다. 커밋 전에 make data로 다시"
            " 나갈 작업 수를 확인하라(동작이 안 바뀌는 정리면 캐시만으로 끝나는지) — CLAUDE.md 절대 규칙"
        )
    return None


def check_edit(path: str, root: Path) -> str | None:
    try:
        rel = Path(path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:  # 저장소 밖
        return None
    if any(rel == g or (g.endswith("/") and rel.startswith(g)) for g in GENERATED):
        return (
            f"{rel}은 생성물이라 손으로 고치지 않는다 — make data·make llm-import가 쓴다"
            " (가상 데이터 생성 설계 §5.1)"
        )
    return None


def check_content(path: str, text: str, root: Path) -> str | None:
    """편집할 내용 검사 — 절대 규칙"""
    try:
        rel = (root / path).resolve().relative_to(root.resolve()).as_posix()
    except ValueError:  # 저장소 밖
        return None
    if rel.startswith("backend/") and _BACKEND_IMPORT.search(text):
        return "backend/는 ground_truth·generator를 import하지 않는다 (CLAUDE.md 절대 규칙)"
    if rel.startswith("evaluation/improve") and _TEST_SPLIT.search(text):
        return "자동 개선 코드는 test 분할을 읽지 않는다 (평가 설계 §2.7)"
    return None


def _ask(reason: str) -> int:
    decision = {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": reason,
    }
    print(json.dumps({"hookSpecificOutput": decision}, ensure_ascii=False))
    return 0


def main(argv: list[str]) -> int:
    try:
        event = json.load(sys.stdin)
    except ValueError:
        return _ask(_prompt(("훅 오류", "훅 입력을 읽지 못했다", "검사 없이 실행된다")))
    try:
        return _run(event, argv)
    except Exception as e:  # noqa: BLE001 — 검사가 죽어도 Bash는 조용히 통과시키지 않는다
        if event.get("tool_name") == "Bash":
            return _ask(
                _prompt(("훅 오류", f"검사 중 오류({type(e).__name__})", "검사 없이 실행된다"))
            )
        return 0


def _run(event: dict, argv: list[str]) -> int:
    tool, args = event.get("tool_name"), event.get("tool_input") or {}
    cwd = Path(event.get("cwd") or ROOT)
    path = args.get("file_path") or args.get("notebook_path") or ""
    if event.get("hook_event_name") == "PostToolUse":
        msg = check_post_edit(path) if tool in ("Edit", "Write", "MultiEdit") else None
    elif (
        tool == "Bash"
        and "--readonly" in argv
        and (why := check_readonly(args.get("command", ""), cwd))
    ):
        msg = f"읽기 전용 에이전트라 막았다: {why}"
    elif tool == "Bash":
        msg = check_bash(args.get("command", ""), cwd)
        if not msg and (ask := check_ask(args.get("command", ""), cwd)):
            return _ask(ask)
    elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        text = args.get("content") or args.get("new_string") or ""
        msg = check_edit(path, ROOT) or check_content(path, text, ROOT)
        if not msg and (ask := check_edit_ask(path, text)):
            return _ask(ask)
    elif tool in ("Read", "Grep"):
        msg = check_read(path or args.get("path") or "", args.get("glob"))
    else:
        msg = None
    if msg:
        print(msg, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
