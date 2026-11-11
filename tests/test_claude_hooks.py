"""Claude Code 훅(.claude/hooks/guard.py) — 막을 것만 막고 나머지는 통과시키는지 (CLAUDE.md '깃'·생성물 규칙)."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / ".claude" / "hooks" / "guard.py"
spec = importlib.util.spec_from_file_location("guard", GUARD)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


@pytest.mark.parametrize(
    "command",
    [
        "git add -A",
        "git add --all",
        "git add .",
        "git add -u",
        "git add evaluation/",
        "git add 'tests/*.py'",
        "git add 'frontend/app/(portal)/[s]creen'",  # 있는 파일이 아닌 글롭
        "cd /tmp && git add -A",
        "git -C /x add .",
        "git status; git add .",
        "git commit -am 'msg'",
        "git commit -a -m msg",
        "git commit --all -m msg",
        "git status\ngit add -A",  # 여러 줄 명령의 둘째 줄
        "git add :/docs",  # pathspec 매직
        "git add :.",
        "env git add -A",
        "sudo git add .",
        "command git add -A",
    ],
)
def test_bulk_add_blocked(command):
    assert guard.check_bash(command, ROOT)


@pytest.mark.parametrize(
    "command",
    [
        "git add evaluation/eval.py tests/evaluation/test_rag.py",
        'git add "docs/치과 AX - 평가 설계.md"',
        # 대괄호·괄호가 든 실제 파일 이름(Next.js 동적 경로) — 글롭이 아니다
        "git add 'frontend/app/(portal)/[screen]/page.tsx'",
        "git diff --name-only -z | xargs -0 git add --",
        'echo "git add -A 는 금지"',
        "git commit -m 'a 와 b'",
        "git commit -q -F - <<'EOF'\n제목: 'add -A' 금지 안내\n\n- 본문 'quote\nEOF",
        "git status --short",
        "uv run pytest -q",
    ],
)
def test_other_commands_pass(command, monkeypatch):
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    assert guard.check_bash(command, ROOT) is None


def test_multiline_commit_still_linted(monkeypatch):
    monkeypatch.setattr(guard, "lint_failure", lambda root: "E501")
    assert guard.check_bash("git add a.py\ngit commit -m x", ROOT)


def test_commit_blocked_when_lint_fails(monkeypatch):
    monkeypatch.setattr(guard, "lint_failure", lambda root: "F401 unused import")
    msg = guard.check_bash("git commit -m x", ROOT)
    assert msg and "make fmt" in msg and "F401" in msg
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    assert guard.check_bash("git commit -m x", ROOT) is None


def test_commit_lints_the_worktree_it_runs_in(monkeypatch, tmp_path):
    """worktree에서 커밋하면 그 worktree를 lint한다 — 원래 폴더(다른 세션의 작업 중 파일)가 아니라"""
    import subprocess

    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    (wt / "sub").mkdir()
    seen = []
    monkeypatch.setattr(guard, "lint_failure", lambda root: seen.append(root))
    guard.check_bash("git commit -m x", wt / "sub")
    assert seen == [wt.resolve()]
    seen.clear()
    guard.check_bash("git commit -m x", tmp_path)  # 저장소 밖이면 훅이 놓인 저장소
    assert seen == [ROOT]


@pytest.mark.parametrize(
    "path",
    [
        "generator/llm/cache/transcript.jsonl",
        "ground_truth/truth.sqlite",
        "data/source.sqlite",
        "data/app.sqlite",
        "ground_truth/call_labels.json",
        "ground_truth/doc_meta.json",
        "generator/llm/jobs/transcript.jsonl",
        "data/llm_calls_gen.jsonl",
    ],
)
def test_generated_files_blocked(path):
    assert guard.check_edit(str(ROOT / path), ROOT)


@pytest.mark.parametrize(
    "path",
    [
        "ground_truth/rag_qa.json",
        "ground_truth/schema.sql",
        "docs/치과 AX - 평가 설계.md",
        "/tmp/x.py",
    ],
)
def test_source_files_pass(path):
    assert guard.check_edit(str(ROOT / path) if not path.startswith("/") else path, ROOT) is None


def test_main_blocks_with_exit_2():
    """훅 입력(JSON stdin) → 막을 때 exit 2 + stderr 이유, 통과면 exit 0"""

    def run(payload):
        return subprocess.run(
            [sys.executable, str(GUARD)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=ROOT,
        )

    r = run({"tool_name": "Bash", "tool_input": {"command": "git add -A"}, "cwd": str(ROOT)})
    assert r.returncode == 2 and "파일 이름" in r.stderr
    r = run(
        {
            "tool_name": "Write",
            "tool_input": {"file_path": str(ROOT / "ground_truth/truth.sqlite")},
            "cwd": str(ROOT),
        }
    )
    assert r.returncode == 2
    assert (
        run({"tool_name": "Read", "tool_input": {"file_path": "x"}, "cwd": str(ROOT)}).returncode
        == 0
    )


# ── 4. 과금·되돌리기 어려운 명령은 확인 창 ─────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin feat/x",
        "gh pr merge 12 --merge",
        "make llm-fill-api TASK=transcript",
        "uv run python -m generator.llm_jobs fill-api --task chart",
        "uv run python -m evaluation.eval --split test --section retrieval",
        "uv run python -m evaluation.eval --section chat --split=test",
        "git status\ngit push",
        "git commit -F - <<'EOF' && git push\nm\nEOF",  # 히어독 여는 줄 뒤의 명령
        "make -C . llm-fill-api TASK=x",
        "make LIMIT=3 llm-fill-api TASK=x",
    ],
)
def test_costly_commands_ask(command):
    assert guard.check_ask(command)


@pytest.mark.parametrize(
    "command",
    ["uv run python -m evaluation.eval --split dev", "gh pr view 12", "git status"],
)
def test_other_commands_do_not_ask(command):
    assert guard.check_ask(command) is None


def test_main_asks_with_json():
    r = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "git push"},
                "cwd": str(ROOT),
            }
        ),
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    out = json.loads(r.stdout)["hookSpecificOutput"]
    assert (
        r.returncode == 0
        and out["permissionDecision"] == "ask"
        and "push" in out["permissionDecisionReason"]
    )


# ── 5. 정형 생성 단계·생성 프롬프트를 고친 뒤 알림 ──────────────


@pytest.mark.parametrize(
    "path",
    [
        "generator/calls.py",
        "generator/master.py",
        "prompts/gen_transcript.md",
        "config/generator.yaml",
        "config/holidays.json",
        "config/forbidden_topics.json",
        "shared/source_schema.sql",
    ],
)
def test_stage_edit_warns(path):
    assert "make data" in guard.check_post_edit(str(ROOT / path))


@pytest.mark.parametrize(
    "path", ["generator/verify/calls.py", "generator/paths.py", "evaluation/eval.py", "docs/a.md"]
)
def test_other_edit_no_warning(path):
    assert guard.check_post_edit(str(ROOT / path)) is None


def test_main_post_edit_feeds_back_with_exit_2():
    r = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Edit",
                "tool_input": {"file_path": str(ROOT / "generator/calls.py")},
                "cwd": str(ROOT),
            }
        ),
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert r.returncode == 2 and "make data" in r.stderr


# ── 7A. 리뷰·문서 점검 에이전트(읽기 전용)의 Bash ───────────────

SCRATCH = "/private/tmp/claude-501/x/scratchpad"


@pytest.mark.parametrize(
    "command",
    [
        "make data",
        "make fmt",
        "make llm-import",
        "git checkout main",
        "git reset --hard",
        "git stash",
        "git commit -m x",
        "rm -rf reports",
        "mv a b",
        "echo x > notes.md",
        "uv run pytest -q >> out.log",
        "uv run ruff format .",
        "uv run ruff check --fix .",
        "sed -i '' 's/a/b/' CLAUDE.md",
        f"cp {SCRATCH}/a.sqlite data/app.sqlite",
        "uv run python -m evaluation.eval --split dev",
        "git branch -D feat/x",
        "make",  # 첫 대상이 data
        "ls\nrm -rf reports",
        "cat <<EOF > notes.md\nx\nEOF",
        "git branch newname",
        "git branch -f main HEAD~1",
        "git worktree add wt HEAD",
        "git worktree remove /private/tmp/x",
        "uv add requests",
        "uv sync",
        "find . -name '*.pyc' -delete",
        "git ls-files | xargs rm",
        "sed --in-place s/a/b/ CLAUDE.md",
        "uv run pytest &>> out.log",
        "uv run python generator/__main__.py",
        "uv run python -m generator.llm_jobs import",
        "uv run python -m generator.llm_jobs split",
        "uv run python -m generator --fake-llm",
        f"uv run python -m generator --fake-llm --db {SCRATCH}/s.sqlite --truth data/t.sqlite",
    ],
)
def test_readonly_blocks_writes(command):
    assert guard.check_readonly(command, ROOT)


@pytest.mark.parametrize(
    "command",
    [
        "git status --short",
        "git diff HEAD -- docs",
        "git log --oneline -5",
        "git show HEAD:CLAUDE.md",
        "git branch -a",
        f"git worktree add {SCRATCH}/wt HEAD",
        "make test",
        "make lint",
        "make verify",
        "uv run pytest tests/evaluation -q",
        "uv run python -m generator.verify",
        "grep -rn foo docs | head",
        f"cp data/app.sqlite {SCRATCH}/app.sqlite",
        f"uv run python -m evaluation.eval --split dev --reports-dir {SCRATCH}/r --db {SCRATCH}/a.sqlite",
        f"echo x > {SCRATCH}/out.txt",
        "uv run pytest -q 2>&1 | tail -3",
        "ls > /dev/null",
        "make test 2>&1 | tail -3",
        "make lint > /private/tmp/x.log",
        f"uv run python -m evaluation.eval --split dev --reports-dir={SCRATCH}/r",
        "git branch --list 'feat/*'",
        "git branch -vv",
        "git worktree list",
        "cat generator/schedule.py | head",
        "sed -n 1,20p generator/records.py",
        "grep -n def generator/calls.py",
        f"uv run python -m generator --fake-llm --db {SCRATCH}/s.sqlite --truth {SCRATCH}/t.sqlite",
    ],
)
def test_readonly_allows_reading(command):
    assert guard.check_readonly(command, ROOT) is None


def test_main_readonly_flag():
    r = subprocess.run(
        [sys.executable, str(GUARD), "--readonly"],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "make data"},
                "cwd": str(ROOT),
            }
        ),
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert r.returncode == 2 and "읽기 전용" in r.stderr


@pytest.mark.parametrize(
    "path, text",
    [
        ("backend/harness/x.py", "from ground_truth import labels"),
        ("backend/x.py", "import generator.master"),
        ("backend/x.py", "from generator import master"),
        ("evaluation/improve.py", "rows = q(\"WHERE split = 'test'\")"),
        ("evaluation/improve_gate.py", 'load(split="test")'),
    ],
)
def test_content_check_blocks(path, text):
    assert guard.check_content(path, text, ROOT)


@pytest.mark.parametrize(
    "path, text",
    [
        ("backend/x.py", "from shared import llm"),
        ("evaluation/eval.py", "WHERE split = 'test'"),  # 사람이 돌리는 평가는 test를 읽는다
        ("evaluation/improve.py", "WHERE split = 'dev'"),
        ("tests/backend/x.py", "from ground_truth import labels"),
    ],
)
def test_content_check_allows(path, text):
    assert guard.check_content(path, text, ROOT) is None
