"""Execute deployment worker/rollback functions with a stateful Docker double."""

import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "deploy/update-backend.sh"


def shell_functions():
    source = SCRIPT.read_text()
    names = ["pause_prompt_worker", "resume_prompt_worker", "finish"]
    functions = []
    for name in names:
        match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", source, re.MULTILINE | re.DOTALL)
        if match:
            functions.append(match.group())
    return "\n".join(functions)


MOCK_DOCKER = r"""
set -Eeuo pipefail
backup=backup
mkdir "$backup"
old_id=old-image
old_reference=app:release
success=false
switched=false
admin_switched=false
timer_was_active=false
prompt_worker_was_running=false
prompt_worker_resume_attempted=false
printf '%s' old-image > tag-image
printf '%s' old-image > api-image
printf '%s' old-image > worker-image
if [[ "$TEST_STATE" == running ]]; then printf true > worker-running
else printf false > worker-running; fi
sudo() {
    [[ "$1" == -n && "$2" == docker ]] || return 90
    shift 2
    printf '%s\n' "$*" >> calls
    case "$*" in
        'compose --profile image-prompt config --services')
            printf 'api\n'
            [[ "$TEST_STATE" == absent ]] || printf 'prompt-analysis-worker\n'
            return 0 ;;
        'compose --profile image-prompt ps -a -q prompt-analysis-worker')
            if [[ "$TEST_STATE" != absent && "$TEST_STATE" != uncreated ]]; then printf 'worker-id\n'; fi ;;
        'inspect --format {{.State.Running}} worker-id') cat worker-running ;;
        'inspect --format {{.Image}} worker-id') cat worker-image ;;
        'compose --profile image-prompt stop --timeout 130 prompt-analysis-worker')
            [[ "$TEST_MODE" != stop-failure ]] || return 41
            printf false > worker-running ;;
        'compose --profile image-prompt up -d --no-deps --no-build prompt-analysis-worker')
            if [[ "$TEST_MODE" == up-failure && "$(cat tag-image)" == new-image ]]; then return 42; fi
            printf true > worker-running
            if [[ "$TEST_MODE" == wrong-image && "$(cat tag-image)" == new-image ]]; then
                printf wrong-image > worker-image
            else cp tag-image worker-image; fi ;;
        'tag old-image app:release') printf old-image > tag-image ;;
        'compose -f compose.yml -f backup/rollback.yml up -d --no-deps --no-build api')
            cp tag-image api-image ;;
        'compose exec -T gateway nginx -s reload') return 0 ;;
        *) printf 'Unexpected Docker call: %s\n' "$*" >&2; return 91 ;;
    esac
}
"""


def execute(tmp_path, state, mode="success"):
    exercise = r"""
trap finish EXIT
pause_prompt_worker
printf 'build\n' >> calls
printf new-image > tag-image
printf new-image > api-image
switched=true
if [[ "$TEST_MODE" == deploy-failure ]]; then exit 17; fi
if [[ "$prompt_worker_was_running" == true ]]; then resume_prompt_worker new-image; fi
success=true
"""
    result = subprocess.run(
        ["bash", "-c", MOCK_DOCKER + shell_functions() + exercise],
        cwd=tmp_path,
        env={**os.environ, "TEST_STATE": state, "TEST_MODE": mode},
        capture_output=True,
        text=True,
    )
    calls = (tmp_path / "calls").read_text().splitlines() if (tmp_path / "calls").exists() else []
    return result, calls


@pytest.mark.parametrize("state", ["absent", "uncreated", "stopped"])
@pytest.mark.parametrize("mode", ["success", "deploy-failure"])
def test_previously_inactive_worker_is_never_started(tmp_path, state, mode):
    result, calls = execute(tmp_path, state, mode)
    assert result.returncode == (0 if mode == "success" else 17), result.stderr
    assert "build" in calls
    assert not any(" stop " in call or " up " in call and "prompt-analysis-worker" in call for call in calls)
    assert (tmp_path / "worker-running").read_text() == "false"


def test_running_worker_stops_before_build_and_resumes_with_new_api_image(tmp_path):
    result, calls = execute(tmp_path, "running")
    assert result.returncode == 0, result.stderr
    stop = "compose --profile image-prompt stop --timeout 130 prompt-analysis-worker"
    start = "compose --profile image-prompt up -d --no-deps --no-build prompt-analysis-worker"
    assert calls.index(stop) < calls.index("build") < calls.index(start)
    assert (tmp_path / "worker-running").read_text() == "true"
    assert (tmp_path / "worker-image").read_text() == (tmp_path / "api-image").read_text() == "new-image"


@pytest.mark.parametrize("mode", ["deploy-failure", "up-failure", "wrong-image"])
def test_failed_deploy_restores_old_api_tag_and_running_worker(tmp_path, mode):
    result, calls = execute(tmp_path, "running", mode)
    assert result.returncode != 0
    assert "build" in calls
    assert (tmp_path / "tag-image").read_text() == "old-image"
    assert (tmp_path / "api-image").read_text() == "old-image"
    assert (tmp_path / "worker-image").read_text() == "old-image"
    assert (tmp_path / "worker-running").read_text() == "true"
    restore = "compose -f compose.yml -f backup/rollback.yml up -d --no-deps --no-build api"
    worker_start = "compose --profile image-prompt up -d --no-deps --no-build prompt-analysis-worker"
    assert calls.index(restore) < len(calls) - 1 - calls[::-1].index(worker_start)


def test_stop_failure_never_reaches_build_or_api_switch(tmp_path):
    result, calls = execute(tmp_path, "running", "stop-failure")
    assert result.returncode != 0
    assert "compose --profile image-prompt stop --timeout 130 prompt-analysis-worker" in calls
    assert "build" not in calls
    assert (tmp_path / "api-image").read_text() == "old-image"
    assert (tmp_path / "worker-running").read_text() == "true"


def test_real_deployment_calls_worker_hooks_before_build_and_after_health():
    source = SCRIPT.read_text()
    pause_call = source.index("\npause_prompt_worker\n")
    assert source.index("trap finish EXIT") < pause_call < source.index("sudo -n docker build")
    resume_call = source.index('resume_prompt_worker "$new_api_image"')
    assert source.index('[[ "$healthy" == true ]]') < resume_call < source.index("\nsuccess=true")
