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


def test_bracket_file_name_passes(tmp_path, monkeypatch):
    """대괄호·괄호가 든 실제 파일 이름(Next.js 동적 경로) — 있는 파일이면 글롭이 아니다"""
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    page = tmp_path / "frontend" / "app" / "(portal)" / "[screen]" / "page.tsx"
    page.parent.mkdir(parents=True)
    page.touch()
    assert guard.check_bash("git add 'frontend/app/(portal)/[screen]/page.tsx'", tmp_path) is None


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
        "git push -f origin feat/x",
        "git push origin main",
        "gh pr merge 12 --merge",
        "make llm-fill-api TASK=transcript",
        "uv run python -m generator.llm_jobs fill-api --task chart",
        "uv run python -m evaluation.eval --split test --section retrieval",
        "uv run python -m evaluation.eval --section chat --split=test",
        "git status\ngit push --force-with-lease",
        "git commit -F - <<'EOF' && git push origin HEAD:main\nm\nEOF",  # 히어독 여는 줄 뒤의 명령
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
                "tool_input": {"command": "git push --force"},
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


# ── 7. 지우기·되돌리기·비밀·정답 보호 (2026-11-16) ──────────────


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf data",
        "rm data/app.sqlite",  # 사람 판정·확정이 들어 있어 다시 만들 수 없다
        "rm -f data/source.sqlite",
        "rm -rf ground_truth/",
        "rm ground_truth/hard_call_labels.json",
        "rm .env",
        "rm -rf .git",
        "rm -rf .claude",
        "rm -rf /",
        "rm -rf ~",
        "rm -rf $HOME/x",
        "rm -rf .",
        "rm -rf *",
        "cd /tmp && rm -rf /Users/x",
        "find data -delete",
        "git clean -fdx",
        "git clean -fX",
        "git clean -fd",  # 추적 안 하는 작업(.superpowers 등)도 지운다
        "git reset --hard",
        "git reset --hard HEAD~1",
        "git checkout -- .",
        "git checkout .",
        "git restore .",
        "git restore --worktree docs",
    ],
)
def test_destructive_commands_blocked(command, monkeypatch):
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    assert guard.check_bash(command, ROOT)


@pytest.mark.parametrize(
    "command",
    [
        "cat .env",
        "head -3 .env",
        "grep KEY .env",
        "source .env",
        ". ./.env",
        "cp .env /tmp/x",
        "base64 < .env",
        "cat frontend/.env.local",
        "env",
        "printenv",
        "printenv ANTHROPIC_API_KEY",
        "echo $ANTHROPIC_API_KEY",
        "echo ${OPENAI_API_KEY}",
    ],
)
def test_secret_reads_blocked(command, monkeypatch):
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    assert guard.check_bash(command, ROOT)


@pytest.mark.parametrize(
    "command",
    [
        "uv run --env-file .env python -m backend.prep run",  # .env를 읽는 정식 경로(값을 보이지 않음)
        "env FOO=1 uv run pytest -q",
        "cat .env.example",
        "test -f .env && echo 있음",
        "ls -la .env",
        "rm -rf /private/tmp/claude-501/x/scratchpad/old",  # 저장소 밖 임시 폴더
        "rm -rf $TMPDIR/x",
        "git clean -n",
        "git reset --soft HEAD~1",
        "git reset HEAD a.py",
        "git checkout -b feat/x",
        "git checkout main",
        "git restore --staged a.py",
        "git restore a.py",
    ],
)
def test_safe_lookalikes_pass(command, monkeypatch):
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    assert guard.check_bash(command, ROOT) is None


@pytest.mark.parametrize(
    "command",
    [
        "sed -i '' 's/a/b/' ground_truth/rag_qa.json",
        "sed -i 's/a/b/' ground_truth/call_labels.json",
        "echo x > ground_truth/tune_qa.json",
        "cat a >> ground_truth/claim_qa.json",
        "cp /tmp/x.json ground_truth/hard_calls.json",
        "mv /tmp/x.json ground_truth/rag_qa.json",
        "tee ground_truth/x.json < a",
        "sqlite3 ground_truth/truth.sqlite \"UPDATE truth_call_labels SET primary_purpose='x'\"",
        "sqlite3 data/app.sqlite 'DELETE FROM ax_judgments'",
        'sqlite3 data/source.sqlite "DROP TABLE emr_patients"',
        "sqlite3 data/app.sqlite < fix.sql",
    ],
)
def test_truth_and_data_writes_blocked(command, monkeypatch):
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    assert guard.check_bash(command, ROOT)


@pytest.mark.parametrize(
    "command",
    [
        "sqlite3 data/app.sqlite 'SELECT count(*) FROM ax_jobs'",
        'sqlite3 -header data/source.sqlite ".schema emr_procedures" "select * from emr_procedures"',
        "cat ground_truth/rag_qa.json | head",
        'cp data/app.sqlite "$TMPDIR/app_copy.sqlite"',  # 사본을 떠서 읽기
        "make data",
        "make hard-review",
    ],
)
def test_truth_and_data_reads_pass(command, monkeypatch):
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    assert guard.check_bash(command, ROOT) is None


@pytest.mark.parametrize("path", [".env", "frontend/.env.local", "/Users/x/proj/.env"])
def test_read_tool_blocks_env(path):
    assert guard.check_read(path)


@pytest.mark.parametrize("path", [".env.example", "README.md", "backend/env_utils.py"])
def test_read_tool_allows_other(path):
    assert guard.check_read(path) is None


def test_main_blocks_read_of_env():
    r = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(
            {
                "tool_name": "Read",
                "tool_input": {"file_path": str(ROOT / ".env")},
                "cwd": str(ROOT),
            }
        ),
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert r.returncode == 2 and ".env" in r.stderr


# ── 8. 확인 창 넓히기 — test 분할·과금·지우기·안전장치 (2026-11-16) ──


@pytest.mark.parametrize(
    "command",
    [
        "uv run python -m evaluation.claim_check run --split test",
        "uv run python -m evaluation.claim_omission synth --split test",
        "uv run python -m evaluation.claim_omission real --split=test",
        "uv run python -m evaluation.claim_qa score --split test",
        "uv run python -m evaluation.claim_appeal run --split test",
        "uv run python -m evaluation.input_trust run --split test",
        "uv run python -m evaluation.recon score --split test",
        "uv run python -m evaluation.expense score --split test",
        "make classify V=v3",
        "make prep",
        "make claim-check SCOPE=all",
        "make recon",
        "make expense-ai MONTHS=2026-10",
        "make report",
        "make nightly",
        "make nightly NO_BATCH=1",
        "make index EMBEDDING=voyage-4",
        "make hard-label",
        "make gold-arbitrate",
        "make prompt-gate A=v2 B=v3 ITEM=purpose",
        "make followups",
        "make gate",
        "uv run python -m backend.nightly run",
        "uv run python -m backend.prep run --no-batch",
        "uv run python -m backend.recon_agent run",
        "uv run python -m backend.classify_batch classify --version v3",
        "uv run python -m backend.rag_index --embedding voyage-4",
        "rm -rf frontend/.next-e2e",  # 저장소 안 지우기(보호 경로 밖)는 확인
        "git branch -D feat/old",
        "git stash drop",
        "git stash clear",
    ],
)
def test_more_commands_ask(command):
    assert guard.check_ask(command, ROOT)


@pytest.mark.parametrize(
    "command",
    [
        "make nightly PLAN=1",
        "uv run python -m backend.nightly run --plan",
        "uv run --group finetune python -m evaluation.input_trust run --split dev --plan",
        "uv run --group finetune python -m evaluation.input_trust check-judge --split dev",
        "uv run --group langgraph python -m evaluation.langgraph_recon run --plan",
        "uv run --group langgraph python -m evaluation.langgraph_recon score",
        "uv run python -m evaluation.improve run --plan",
        "uv run python -m backend.rag_index --embedding voyage-4 --stats",
        "uv run python -m backend.report --help",  # argparse가 설명만 찍고 끝난다
        "python -m evaluation.prompt_gate -h",
        "uv run python -m backend.classify_batch classify --help",
        # 과금 모듈 경로를 읽기만 하는 명령 — 실행이 아니다
        "sed -n 100,125p backend/harness/rounds.py; grep -n run_batch backend/report.py",
        "grep -n has_todo evaluation/prompt_gate.py | head",
        "cat backend/nightly.py",
        "git diff main -- backend/report.py",
        "uv run ruff check backend/report.py",
        "uv run pytest tests/backend/test_report.py -q",
        'uv run python -c "import backend.report"',
        "uv run python -m evaluation.claim_qa score --split dev",
        "make test",
        "make verify",
        "make data",
        "make eval",
        "make judge V=v3",  # 로컬 Qwen — 과금 아님
        "gh pr create --title x --body y",
        "git branch -d feat/merged",
        "rm -rf $TMPDIR/x",
    ],
)
def test_free_commands_do_not_ask(command):
    assert guard.check_ask(command, ROOT) is None


def _repo_on(tmp_path, branch):
    subprocess.run(["git", "init", "-q", "-b", branch, str(tmp_path)], check=True)
    return tmp_path


def test_push_asks_only_for_main_or_force(tmp_path):
    """feat 브랜치 push는 확인 없이(PR 흐름), main으로·강제 push는 확인 — 사용자 결정 2026-11-16"""
    feat = _repo_on(tmp_path / "f", "feat/x")
    main = _repo_on(tmp_path / "m", "main")
    assert guard.check_ask("git push", feat) is None
    assert guard.check_ask("git push -u origin feat/x", feat) is None
    assert guard.check_ask("git push", main)
    assert guard.check_ask("git push origin main", feat)
    assert guard.check_ask("git push origin feat/x:main", feat)
    assert guard.check_ask("git push --force", feat)
    assert guard.check_ask("git push -f origin feat/x", feat)
    assert guard.check_ask("git push origin +feat/x", feat)


@pytest.mark.parametrize(
    "path, text",
    [
        (".claude/settings.json", "{}"),
        (".claude/settings.local.json", "{}"),
        (".claude/hooks/guard.py", "x = 1"),
        ("evaluation/metrics.py", "GATE_RUNS = 1"),
    ],
)
def test_safety_edits_ask(path, text):
    assert guard.check_edit_ask(str(ROOT / path), text)


@pytest.mark.parametrize(
    "path, text",
    [
        ("evaluation/metrics.py", "def wilson(k, n):"),  # 관문 기준이 아닌 곳
        ("backend/nightly.py", "GATE_RUNS = 1"),
        (".claude/agents/pj2-reviewer.md", "x"),
    ],
)
def test_other_edits_do_not_ask(path, text):
    assert guard.check_edit_ask(str(ROOT / path), text) is None


def test_main_asks_for_safety_edit():
    r = subprocess.run(
        [sys.executable, str(GUARD)],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Edit",
                "tool_input": {
                    "file_path": str(ROOT / ".claude/hooks/guard.py"),
                    "old_string": "a",
                    "new_string": "b",
                },
                "cwd": str(ROOT),
            }
        ),
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert (
        r.returncode == 0
        and json.loads(r.stdout)["hookSpecificOutput"]["permissionDecision"] == "ask"
    )


def test_settings_wiring():
    """PR 만들기는 확인 없이, Read·Grep도 훅을 지난다(.env)"""
    cfg = json.loads((ROOT / ".claude" / "settings.json").read_text(encoding="utf-8"))
    assert "Bash(gh pr create *)" in cfg["permissions"]["allow"]
    matchers = {h["matcher"] for h in cfg["hooks"]["PreToolUse"]}
    assert any("Read" in m and "Grep" in m for m in matchers)


# ── 9. 리뷰 반영 — 우회·오탐 (2026-11-16) ─────────────────────


@pytest.mark.parametrize(
    "command",
    [
        # 1. 작업 트리 되돌리기
        "git checkout HEAD .",
        "git checkout HEAD~3 .",
        "git checkout -f .",
        "git checkout HEAD docs",
        "git checkout frontend",  # 같은 이름 브랜치가 없으면 폴더를 되돌린다
        "git checkout -f main",
        "git checkout --force main",
        "git switch --discard-changes main",
        "git switch -f main",
        "git restore --staged --worktree .",
        "git restore --worktree --staged docs",
        "git restore -SW .",
        # 2. 글롭·명령 치환·cd
        "rm -rf ./*",
        "rm -rf data*",
        "rm -rf ground*",
        "rm -rf d?ta",
        "rm -rf $(git rev-parse --show-toplevel)",
        "rm -rf `pwd`",
        'rm -rf "$(pwd)"',
        "cd data && rm -f app.sqlite",
        # 3. .env 우회
        "grep -rn API_KEY .",
        "grep -R KEY",
        "grep -rin --include=.env* KEY .",
        "cat .env*",
        "cat .e*",
        "cat .en?",
        "< .env cat",
        "tr a a < .env",
        "diff .env .env.example",
        "export",
        "export -p",
        "set",
        "declare -p",
        "env -0",
        # 5. sqlite3 쓰기
        "sqlite3 data/app.sqlite <<'SQL'\nDELETE FROM ax_jobs;\nSQL",
        "echo 'DELETE FROM ax_jobs' | sqlite3 data/app.sqlite",
        "sqlite3 data/app.sqlite '.read x.sql'",
        "sqlite3 data/app.sqlite '.import x.csv ax_jobs'",
        "sqlite3 data/app.sqlite 'pragma user_version=3'",
        "sqlite3 'file:data/app.sqlite?mode=rw' 'DELETE FROM ax_jobs'",
        # 6. rm이 아닌 도구로 날리기
        "mv data/app.sqlite data/app.bak",
        "mv data /tmp/x",
        "unlink data/app.sqlite",
        "truncate -s0 data/app.sqlite",
        "shred data/app.sqlite",
        "dd if=/dev/zero of=data/app.sqlite",
        "ln -sf /tmp/x data/app.sqlite",
        "cp x.json ground_truth/",
        "cp -t ground_truth x.json",
        "cp /tmp/app.sqlite data/",
        # 8. 임시 루트·탈출
        "rm -f $TMPDIR/../../etc",
        "rm -rf /tmp",
        "rm -rf /var/folders",
    ],
)
def test_review_bypasses_blocked(command, monkeypatch):
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    assert guard.check_bash(command, ROOT)


@pytest.mark.parametrize(
    "command",
    [
        "git restore --staged -- .",  # 스테이징만 푼다
        "git restore --staged -- docs",
        "git restore -S .",
        "git checkout -- a.py",
        "git checkout -b feat/x origin/main",
        "git switch main",
        "git switch -c feat/x",
        "grep -rn foo backend/",
        "grep -r --exclude='.env*' KEY .",
        "rg API_KEY",
        "set -e",
        "set -euo pipefail",
        "export FOO=1",
        "echo $MONKEY",
        "echo $TOKENIZERS_PARALLELISM",
        "sqlite3 data/app.sqlite \"select replace(name,'a','b') from ax_jobs\"",
        "sqlite3 data/app.sqlite \"select * from ax_auto_actions where kind='DELETE'\"",
        "sqlite3 -readonly data/app.sqlite '.read q.sql'",
        "sqlite3 data/app.sqlite <<'SQL'\nSELECT count(*) FROM ax_jobs;\nSQL",
        "mv a.py b.py",
        "cp ground_truth/rag_qa.json /tmp/x.json",
        "rm -rf $TMPDIR/x",
        "rm -rf /private/tmp/claude-501/x/scratchpad/old",
    ],
)
def test_review_lookalikes_pass(command, monkeypatch):
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    assert guard.check_bash(command, ROOT) is None


@pytest.mark.parametrize(
    "command",
    [
        "ls | xargs rm",
        "echo x | xargs rm -rf",
        "uv run python -m evaluation.hard_labels label",
        "uv run python -m evaluation.call_actions label",
        "uv run python -m evaluation.followup run",
        "uv run python -m evaluation.gold_audit arbitrate",
        "uv run python -m evaluation.prompt_gate --a v2 --b v3 --item purpose",
        "uv run python -m evaluation.claim_qa draft",
        "uv run python -m evaluation.claim_omission cases",
        "uv run python -m evaluation.claim_omission synth --split dev",
        "uv run python -m evaluation.claim_check sample",
        "uv run python -m evaluation.claim_appeal run --split dev",
        "uv run python -m evaluation.input_trust run --split dev",
        "uv run --group langgraph python -m evaluation.langgraph_recon run",
        "make trust-eval",
        "make improve TASK=classify ITEM=has_todo",
        "uv run python -m evaluation.improve run --task classify --item has_todo",
        "uv run python backend/prep.py run",
        "python3 evaluation/prompt_gate.py --a v2 --b v3 --item purpose",
        "./backend/report.py",
        "uv run --env-file .env python -m backend.report",
        "uv run backend/nightly.py run",
        "rm -rf .claude/worktrees/feat-x",  # worktree 지우기는 확인(보호 경로에서 뺌)
        "rm -rf .claude/worktrees/feat-x/frontend/.next",
    ],
)
def test_review_more_asks(command):
    assert guard.check_ask(command, ROOT)


@pytest.mark.parametrize(
    "command",
    [
        "uv run python -m evaluation.claim_qa score --split dev",
        "uv run python -m evaluation.gold_audit audit",
        "uv run python -m evaluation.recon score --split dev",
    ],
)
def test_review_free_evaluation_do_not_ask(command):
    assert guard.check_ask(command, ROOT) is None


def test_worktree_inside_claude_keeps_own_protection(monkeypatch):
    """.claude/worktrees/<이름>/ 안은 그 worktree를 저장소처럼 본다 — 그 안 data/는 보호"""
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    assert guard.check_bash("rm -rf .claude/worktrees/feat-x/data", ROOT)
    assert guard.check_bash("rm -rf .claude/worktrees/feat-x", ROOT) is None
    assert guard.check_bash("rm -rf .claude/hooks", ROOT)


def test_push_head_and_mirror(tmp_path):
    main = _repo_on(tmp_path / "m", "main")
    feat = _repo_on(tmp_path / "f", "feat/x")
    assert guard.check_ask("git push origin HEAD", main)
    assert guard.check_ask("git push origin @", main)
    assert guard.check_ask("git push origin HEAD", feat) is None
    assert guard.check_ask("git push --mirror", feat)
    assert guard.check_ask("git push --all origin", feat)
    assert guard.check_ask("git push -fu origin feat/x", feat)
    assert guard.check_ask(f"git -C {main} push", feat)  # -C 저장소의 브랜치로
    assert guard.check_ask(f"cd {main} && git push", feat)


def test_unparsable_risky_command_asks():
    """따옴표를 해석 못 하는 명령 — 위험 단어가 있으면 확인으로(그냥 통과하지 않게)"""
    assert guard.check_ask("rm -rf data; echo $'it\\'s'", ROOT)
    assert guard.check_ask("echo 'unclosed", ROOT) is None


def test_main_asks_when_check_crashes(monkeypatch, capsys):
    """검사 중 예외 — Bash는 확인 창으로(조용히 통과하지 않게)"""
    import io

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(guard, "check_bash", boom)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})),
    )
    assert guard.main([]) == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "ask"


@pytest.mark.parametrize("glob_", [".env", ".env*", ".e*", "*.local"])
def test_grep_tool_glob_blocks_env(glob_):
    assert guard.check_read("", glob_)


def test_grep_tool_glob_allows_code():
    assert guard.check_read("", "*.py") is None


# ── 10. 후속 리뷰 반영 (2026-11-16) ─────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "mv -t /tmp data/app.sqlite",
        "mv --target-directory /tmp data/app.sqlite",
        "cp --target-directory ground_truth x.json",
        "cp --target-directory=ground_truth x.json",
        "sqlite3 -separator , data/app.sqlite 'delete from ax_jobs'",
        "sqlite3 -cmd 'delete from ax_jobs' data/app.sqlite 'select 1'",
        "cat fix.sql | sqlite3 data/app.sqlite",
        "sqlite3 data/app.sqlite 'with t as (select 1) delete from ax_jobs'",
        "git restore ':/'",
        "git restore '*.py'",
        "git checkout -- '*.py'",
        "cd frontend && git add app",  # 폴더 통째 add — cd를 따라가야 보인다
        "rg -uuu API_KEY",
        "rg --hidden --no-ignore KEY",
        "grep -r --exclude=.env KEY .",  # .env.local은 못 뺀다
    ],
)
def test_followup_bypasses_blocked(command, monkeypatch):
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    assert guard.check_bash(command, ROOT)


@pytest.mark.parametrize(
    "command",
    [
        "echo 'select 1' | sqlite3 data/app.sqlite",
        "rg API_KEY",
        "rg -u KEY backend",
        "git restore a.py",
        "git checkout -- a.py",
        "cd frontend && git add app/layout.tsx",
    ],
)
def test_followup_lookalikes_pass(command, monkeypatch):
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    assert guard.check_bash(command, ROOT) is None


def test_env_write_message_says_write(monkeypatch):
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    msg = guard.check_bash("cp .env.example .env", ROOT)
    assert msg and "쓰" in msg and "읽지" not in msg


def test_find_with_filter_asks_not_blocks(monkeypatch):
    """find . -name '*.pyc' -delete — 저장소 통째가 아니라 걸러 지우는 정리라 확인으로"""
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    cmd = "find . -name '*.pyc' -delete"
    assert guard.check_bash(cmd, ROOT) is None
    assert guard.check_ask(cmd, ROOT)
    assert guard.check_bash(
        "find data -name '*.sqlite' -delete", ROOT
    )  # 보호 경로 안은 그대로 막기


def test_home_path_inside_repo_judged_after_expand(monkeypatch):
    monkeypatch.setattr(guard, "lint_failure", lambda root: None)
    home = Path.home()
    if not ROOT.is_relative_to(home):
        pytest.skip("저장소가 홈 아래가 아님")
    cmd = f"rm -rf ~/{ROOT.relative_to(home)}/.superpowers"
    assert guard.check_bash(cmd, ROOT) is None
    assert guard.check_ask(cmd, ROOT)
    assert guard.check_bash(f"rm -rf ~/{ROOT.relative_to(home)}/data", ROOT)
    assert guard.check_bash("rm -rf ~", ROOT)


def test_commit_lint_follows_cd(monkeypatch, tmp_path):
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    seen = []
    monkeypatch.setattr(guard, "lint_failure", lambda root: seen.append(root))
    guard.check_bash(f"cd {wt} && git commit -m x", ROOT)
    assert seen == [wt.resolve()]


def test_main_asks_on_broken_input(monkeypatch, capsys):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO("{깨진"))
    assert guard.main([]) == 0
    assert json.loads(capsys.readouterr().out)["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_readonly_allows_copy_to_tmpdir():
    """리뷰 에이전트가 DB 사본을 임시 폴더로 뜨는 것 — $TMPDIR를 펼쳐 저장소 밖으로 본다"""
    assert guard.check_readonly('cp data/app.sqlite "$TMPDIR/a.sqlite"', ROOT) is None


_WRAPPED_INNER = [
    "rm -rf docs",
    "cat .env",
    "echo x > ground_truth/a.json",
    "git push --force",
    "make nightly",
    "uv run python -m backend.report run",
]


@pytest.mark.parametrize("wrap", ['bash -c "{}"', "sh -lc '{}'", 'zsh -c "{}"', 'eval "{}"'])
@pytest.mark.parametrize("inner", _WRAPPED_INNER)
def test_shell_wrapper_checks_inner_command(wrap, inner):
    """bash -c·eval로 감싸도 안쪽 명령을 같은 규칙으로 본다"""
    command = wrap.format(inner)
    # 감싸지 않으면 걸리는 명령
    assert guard.check_bash(inner, ROOT) or guard.check_ask(inner, ROOT)
    assert guard.check_bash(command, ROOT) == guard.check_bash(inner, ROOT)
    assert guard.check_ask(command, ROOT) == guard.check_ask(inner, ROOT)


def test_shell_wrapper_readonly():
    assert guard.check_readonly('bash -c "git commit -m x"', ROOT)


@pytest.mark.parametrize(
    "command", ['bash -c "make nightly PLAN=1"', 'sh -c "ls docs | head"', "bash scripts/x.sh"]
)
def test_shell_wrapper_free(command):
    assert guard.check_bash(command, ROOT) is None
    assert guard.check_ask(command, ROOT) is None


@pytest.mark.parametrize(
    "command",
    [
        "uv run python -m backend.report run > >(sort -h)",  # 다른 도구의 -h는 무료 플래그가 아니다
        "uv run python -m backend.report run 2>-h",
        "uv run python -m backend.nightly run -- -h",
    ],
)
def test_help_flag_only_for_the_module(command):
    assert guard.check_ask(command, ROOT)


@pytest.mark.parametrize(
    ("command", "tag", "part"),
    [
        (
            "git status; uv run python -m backend.report run",
            "[과금]",
            "uv run python -m backend.report run",
        ),
        ("make nightly", "[과금]", "make nightly"),
        ("git push origin main", "[되돌리기 어려움]", "git push origin main"),
        ("gh pr merge 63 --merge", "[되돌리기 어려움]", "gh pr merge 63 --merge"),
        ("ls; rm -rf docs", "[지우기]", "rm -rf docs"),
        (
            "uv run python -m evaluation.claim_qa score --split test",
            "[test 평가]",
            "uv run python -m evaluation.claim_qa score --split test",
        ),
        ('bash -c "make report"', "[과금]", "make report"),  # 감싼 명령은 안쪽을 보인다
    ],
)
def test_ask_prompt_shows_tag_and_part(command, tag, part):
    """확인 창 이유: 머리표로 시작하고, 긴 명령 중 걸린 단순 명령만 보이고, 허용하면 무슨 일인지"""
    why = guard.check_ask(command, ROOT)
    assert why.startswith(tag)
    assert f"걸린 부분: {part}" in why
    assert "허용하면" in why
    assert "\n" not in why


def test_ask_prompt_hides_other_segments():
    why = guard.check_ask("git log --oneline -3; uv run python -m backend.report run", ROOT)
    assert "git log" not in why


def test_ask_prompt_names_paid_module():
    why = guard.check_ask("uv run python -m backend.report run", ROOT)
    assert "경영 리포트 해석" in why


def test_ask_prompt_cuts_long_part():
    why = guard.check_ask("uv run python -m backend.report run " + "--months 2026-08 " * 20, ROOT)
    part = why.split("걸린 부분: ")[1].split(" · ")[0]
    assert len(part) <= guard.PROMPT_PART_MAX and part.endswith("…")


def test_every_paid_module_has_label():
    assert set(guard.MODULE_LABELS) == set(guard.PAID_MODULES)


def test_ask_prompt_unparsable():
    assert guard.check_ask("rm -rf 'docs", ROOT).startswith("[해석 못 함]")


def test_edit_ask_prompt_tag():
    why = guard.check_edit_ask(str(ROOT / ".claude/hooks/guard.py"), "x = 1")
    assert why.startswith("[안전장치]") and "걸린 부분: .claude/hooks/guard.py" in why


@pytest.mark.parametrize(
    "command",
    [
        'rg -l x -g "!docs/**" .',  # ! 글롭은 빼고 찾기 — .env를 읽지 않는다
        "rg -n x -g '!**/node_modules/**' -g '!docs/superpowers/**' .",
        "rg -n x --glob='!*.lock' .",
        "rg -n x --iglob '!**/*.md' docs",
    ],
)
def test_rg_exclude_glob_is_not_secret_read(command):
    assert guard.check_bash(command, ROOT) is None


@pytest.mark.parametrize("command", ['rg -n x -g "*" .', "rg -n x --glob '.env*' ."])
def test_rg_include_glob_matching_env_still_blocked(command):
    assert guard.check_bash(command, ROOT)
