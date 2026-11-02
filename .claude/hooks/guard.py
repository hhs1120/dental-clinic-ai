"""Claude Code 훅 — 이 저장소 규칙을 도구 호출 앞뒤에 지킨다(.claude/settings.json과 읽기 전용 에이전트 머리말이 부른다).

PreToolUse
- Bash 막기: `git add`는 파일 이름으로만(-A·--all·-u·`.`·폴더·글롭 금지), `git commit -a` 금지 — CLAUDE.md '깃'.
  `git commit` 전에 `make lint`가 통과해야 한다 — CLAUDE.md '코드 컨벤션'
- Bash 확인 창: 과금·되돌리기 어려운 명령(push·PR 머지·LLM API 채우기·test 평가)은 사용자 승인
- Edit·Write 막기: 생성물(LLM 캐시·DB·생성기가 쓰는 정답 JSON)은 손으로 고치지 않는다 — make data·make llm-import가 쓴다.
  backend/가 ground_truth·generator를 import하는 편집, 자동 개선 코드(evaluation/improve*)가 test 분할을 읽는 편집도 막는다
  — CLAUDE.md 절대 규칙. 편집 내용만 보는 얕은 검사이고 근거는 tests/test_no_ground_truth_import.py가 지킨다
- --readonly(리뷰·문서 점검 에이전트): 저장소를 바꾸는 Bash(쓰는 git·make·rm·저장소 안 리디렉션 등)를 막는다.
  명령 문자열만 보므로 프로그램이 속에서 쓰는 것까지는 못 막는다 — 부르는 쪽이 git status 전후를 비교한다
PostToolUse
- 정형 생성 단계·생성 프롬프트를 고치면 캐시 미스 확인을 알린다 — CLAUDE.md 절대 규칙

입력은 훅 JSON(stdin). 막기·알림은 exit 2 + 이유(stderr — Claude에게 전달), 확인 창은 JSON(permissionDecision ask),
아니면 exit 0. 표준 라이브러리만 쓴다.
"""

from __future__ import (
    annotations,
)  # 시스템 파이썬(3.9)으로 돌아도 주석 문법 때문에 죽지 않게 — 죽으면 훅이 조용히 꺼진다

import json
import re
import shlex
import subprocess
import sys
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
        if token in _SEPARATORS or set(token) <= set(";&|()\n"):
            segments.append(current)
            current = []
        else:
            current.append(token)
    return [_unprefix(s) for s in segments + [current] if _unprefix(s)]


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
        if not a.startswith("-") and (
            a == "." or a.startswith(":") or any(c in a for c in "*?[") or (cwd / a).is_dir()
        ):
            return a
    return None


def lint_failure(root: Path) -> str | None:
    """make lint가 실패하면 출력 끝부분, 통과면 None"""
    r = subprocess.run(["make", "-s", "lint"], cwd=root, capture_output=True, text=True)
    return None if r.returncode == 0 else (r.stdout + r.stderr).strip()[-1500:]


def check_bash(command: str, cwd: Path) -> str | None:
    try:
        segments = _segments(command)
    except ValueError:  # 따옴표가 안 닫힌 명령 — 판단하지 않고 통과
        return None
    commit = False
    for tokens in segments:
        sub = _git_subcommand(tokens)
        if not sub:
            continue
        name, args = sub
        if name == "add" and (bad := _bulk_add(args, cwd)):
            return (
                f"`git add {bad}`는 막혀 있다 — git add는 파일 이름으로만(CLAUDE.md '깃'). "
                "바꾼 파일을 이름으로 나열하라(예: git add a.py b.md)"
            )
        if name == "commit":
            if any(a == "--all" or re.fullmatch(r"-[a-zA-Z]*a[a-zA-Z]*", a) for a in args):
                return "`git commit -a`는 막혀 있다 — 파일 이름으로 git add 한 뒤 커밋하라(CLAUDE.md '깃')"
            commit = True
    if commit and (out := lint_failure(ROOT)):
        return (
            "커밋 전 `make lint` 실패(스테이징한 파일만이 아니라 작업 트리 전체 — CI와 같은 범위) — "
            f"`make fmt`로 정리하고 남은 것을 고친 뒤 커밋하라:\n{out}"
        )
    return None


def _in_repo(path: str, cwd: Path) -> bool:
    p = Path(path) if path.startswith("/") else cwd / path
    try:
        p.resolve().relative_to(ROOT.resolve())
    except ValueError:
        return False
    return True


def check_ask(command: str) -> str | None:
    """사용자 확인을 받을 명령이면 이유"""
    try:
        segments = _segments(command)
    except ValueError:
        return None
    for tokens in segments:
        sub = _git_subcommand(tokens)
        text = " ".join(tokens)
        if sub and sub[0] == "push":
            return "git push — 밖으로 나가는 일이라 확인한다(커밋·푸시는 요청 시에만)"
        if tokens[:3] == ["gh", "pr", "merge"]:
            return "PR 머지 — 되돌리기 어려워 확인한다"
        if (
            Path(tokens[0]).name == "make"
            and "llm-fill-api" in tokens
            or "fill-api" in tokens
            and any("llm_jobs" in t for t in tokens)
        ):
            return "LLM API 채우기 — 과금되는 배치라 확인한다(하루 상한 밖 — 건수는 make data가 알려 준다, 기능 정의 §7)"
        if ("evaluation.eval" in text or "evaluation/eval.py" in text) and re.search(
            r"--split[ =]test\b", text
        ):
            return "test 평가 — 묶음마다 한 번뿐이라 확인한다(평가 설계 §1 원칙 2)"
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


def main(argv: list[str]) -> int:
    event = json.load(sys.stdin)
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
        if not msg and (ask := check_ask(args.get("command", ""))):
            decision = {
                "hookEventName": "PreToolUse",
                "permissionDecision": "ask",
                "permissionDecisionReason": ask,
            }
            print(json.dumps({"hookSpecificOutput": decision}, ensure_ascii=False))
            return 0
    elif tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        text = args.get("content") or args.get("new_string") or ""
        msg = check_edit(path, ROOT) or check_content(path, text, ROOT)
    else:
        msg = None
    if msg:
        print(msg, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
