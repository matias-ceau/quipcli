import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import quipcli
from quipcli import (
    _build_user_content,
    _is_image_url,
    _load_models,
    _maybe_update_models_bg,
    _models_url,
    _resolve_model_name,
    _strip_fences,
    _UsageStats,
    build_parser,
    get_content,
)

# ── Helpers ───────────────────────────────────────────────────────────────────


class MockHTTPResponse:
    """Minimal http.client.HTTPResponse stand-in."""

    def __init__(
        self,
        status: int,
        body: bytes,
        sse_lines: list[bytes] | None = None,
        headers: dict[str, str] | None = None,
    ):
        self.status = status
        self._body = body
        self._lines = sse_lines or []
        self._idx = 0
        self._headers = headers or {}

    def getheader(self, name: str, default: str | None = None) -> str | None:
        return self._headers.get(name, default)

    def read(self) -> bytes:
        return self._body

    def readline(self) -> bytes:
        if self._idx >= len(self._lines):
            return b""
        line = self._lines[self._idx]
        self._idx += 1
        return line


def _mock_conn(resp: MockHTTPResponse) -> MagicMock:
    conn = MagicMock()
    conn.getresponse.return_value = resp
    return conn


@pytest.fixture
def mock_http():
    """Patches HTTPSConnection + API key. Yields a factory: resp -> http_cls mock."""
    with (
        patch("http.client.HTTPSConnection") as http_cls,
        patch("quipcli.constants._API_KEY", "key"),
    ):

        def setup(resp: MockHTTPResponse) -> MagicMock:
            http_cls.return_value = _mock_conn(resp)
            return http_cls

        yield setup


# ── _migrate_legacy_data ─────────────────────────────────────────────────────


class TestMigrateLegacyData:
    def _old_dir(self, tmp_path, monkeypatch):
        old = tmp_path / "old-xdg" / "llm-cmd"
        old.mkdir(parents=True)
        for var in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            monkeypatch.setenv(var, str(tmp_path / "old-xdg"))
        return old

    def test_copies_legacy_files_to_new_location(self, tmp_path, monkeypatch):
        old = self._old_dir(tmp_path, monkeypatch)
        (old / "config.json").write_text('{"default_model": "legacy/model"}')
        (old / "models.json").write_text('{"data": []}')
        (old / "history.db").write_text("legacy db bytes")
        new_dir = tmp_path / "new-xdg"
        with (
            patch("quipcli.constants._CONFIG_FILE", new_dir / "config.json"),
            patch("quipcli.constants._MODELS_CACHE", new_dir / "models.json"),
            patch("quipcli.constants._HISTORY_DB", new_dir / "history.db"),
        ):
            quipcli.constants._migrate_legacy_data()
            assert (
                new_dir / "config.json"
            ).read_text() == '{"default_model": "legacy/model"}'
            assert (new_dir / "models.json").read_text() == '{"data": []}'
            assert (new_dir / "history.db").read_text() == "legacy db bytes"
        # old files untouched, not moved
        assert (old / "config.json").exists()
        assert (old / "history.db").exists()

    def test_does_not_overwrite_existing_new_file(self, tmp_path, monkeypatch):
        old = self._old_dir(tmp_path, monkeypatch)
        (old / "config.json").write_text('{"default_model": "legacy/model"}')
        new_dir = tmp_path / "new-xdg"
        new_dir.mkdir()
        (new_dir / "config.json").write_text('{"default_model": "current/model"}')
        with (
            patch("quipcli.constants._CONFIG_FILE", new_dir / "config.json"),
            patch("quipcli.constants._MODELS_CACHE", new_dir / "models.json"),
            patch("quipcli.constants._HISTORY_DB", new_dir / "history.db"),
        ):
            quipcli.constants._migrate_legacy_data()
        assert (
            new_dir / "config.json"
        ).read_text() == '{"default_model": "current/model"}'

    def test_copy_failure_is_swallowed(self, tmp_path, monkeypatch):
        old = self._old_dir(tmp_path, monkeypatch)
        (old / "config.json").write_text('{"default_model": "legacy/model"}')
        new_dir = tmp_path / "new-xdg"
        with (
            patch("quipcli.constants._CONFIG_FILE", new_dir / "config.json"),
            patch("quipcli.constants._MODELS_CACHE", new_dir / "models.json"),
            patch("quipcli.constants._HISTORY_DB", new_dir / "history.db"),
            patch("shutil.copy2", side_effect=OSError("permission denied")),
        ):
            quipcli.constants._migrate_legacy_data()  # must not raise

    def test_no_legacy_dirs_is_a_noop(self, tmp_path, monkeypatch):
        for var in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
            monkeypatch.setenv(var, str(tmp_path / "no-such-xdg"))
        new_dir = tmp_path / "new-xdg"
        with (
            patch("quipcli.constants._CONFIG_FILE", new_dir / "config.json"),
            patch("quipcli.constants._MODELS_CACHE", new_dir / "models.json"),
            patch("quipcli.constants._HISTORY_DB", new_dir / "history.db"),
        ):
            quipcli.constants._migrate_legacy_data()  # must not raise
        assert not new_dir.exists()


# ── _strip_fences ─────────────────────────────────────────────────────────────


class TestStripFences:
    def test_plain_command_unchanged(self):
        assert _strip_fences("ls -la") == "ls -la"

    def test_bash_fence(self):
        assert _strip_fences("```bash\nls -la\n```") == "ls -la"

    def test_sh_fence(self):
        assert _strip_fences("```sh\nls -la\n```") == "ls -la"

    def test_generic_fence(self):
        assert _strip_fences("```\nls -la\n```") == "ls -la"

    def test_no_trailing_fence(self):
        assert _strip_fences("```bash\nls -la") == "ls -la"

    def test_whitespace_stripped(self):
        assert _strip_fences("  ls -la  ") == "ls -la"

    def test_multiline_content_preserved(self):
        assert _strip_fences("```bash\ncmd1 && cmd2\n```") == "cmd1 && cmd2"


# ── build_parser / get_content ────────────────────────────────────────────────


class TestParser:
    def test_words_joined(self):
        args = build_parser().parse_args(["what", "is", "X"])
        content, mods = get_content(args)
        assert content == "what is X"
        assert mods == set()

    def test_default_model(self):
        args = build_parser().parse_args(["hi"])
        assert args.model == quipcli.DEFAULT_MODEL

    def test_model_flag(self):
        args = build_parser().parse_args(["-m", "anthropic/claude-3-5-haiku", "hi"])
        assert args.model == "anthropic/claude-3-5-haiku"

    def test_execute_default_false(self):
        assert build_parser().parse_args(["hi"]).execute is False

    def test_execute_flag(self):
        assert build_parser().parse_args(["-e", "do it"]).execute is True

    def test_code_default_false(self):
        assert build_parser().parse_args(["hi"]).code is False

    def test_code_flag(self):
        assert build_parser().parse_args(["-c", "write a sort"]).code is True

    def test_agent_default_false(self):
        assert build_parser().parse_args(["hi"]).agent is False

    def test_agent_flag(self):
        assert build_parser().parse_args(["-a", "do it"]).agent is True

    @pytest.mark.parametrize(
        "combo", [["-e", "-c"], ["-e", "-a"], ["-c", "-a"], ["-e", "-c", "-a"]]
    )
    def test_mode_flags_are_mutually_exclusive(self, combo, capsys):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args([*combo, "do it"])
        assert exc.value.code == 2
        assert "not allowed with" in capsys.readouterr().err

    def test_max_steps_default(self):
        assert build_parser().parse_args(["hi"]).max_steps == 12

    def test_max_steps_flag(self):
        args = build_parser().parse_args(["-a", "--max-steps", "5", "do it"])
        assert args.max_steps == 5

    @pytest.mark.parametrize("bad", ["0", "-1", "-5"])
    def test_max_steps_rejects_non_positive(self, bad, capsys):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["-a", "--max-steps", bad, "do it"])
        assert exc.value.code == 2
        assert "must be >= 1" in capsys.readouterr().err

    def test_no_web_default_false(self):
        assert build_parser().parse_args(["hi"]).no_web is False

    def test_no_web_flag(self):
        assert build_parser().parse_args(["-a", "--no-web", "do it"]).no_web is True

    def test_system_flag(self):
        args = build_parser().parse_args(["-S", "be terse", "hi"])
        assert args.system == "be terse"

    def test_update_models_flag(self):
        assert build_parser().parse_args(["--update-models"]).update_models is True

    def test_models_flag(self):
        assert build_parser().parse_args(["--models"]).models is True

    def test_model_get_flag(self):
        assert build_parser().parse_args(["--model-get"]).model_get is True

    def test_model_set_flag_with_value(self):
        assert build_parser().parse_args(["--model-set", "haiku"]).model_set == "haiku"

    def test_model_set_flag_no_value(self):
        assert build_parser().parse_args(["--model-set"]).model_set == ""

    def test_model_set_flag_absent(self):
        assert build_parser().parse_args(["hi"]).model_set is None

    def test_config_edit_flag(self):
        assert build_parser().parse_args(["--config-edit"]).config_edit is True

    def test_status_flag(self):
        assert build_parser().parse_args(["--status"]).status is True

    def test_cost_flag_default_period(self):
        assert build_parser().parse_args(["--cost"]).cost == "7d"

    def test_cost_flag_explicit_period(self):
        assert build_parser().parse_args(["--cost", "30d"]).cost == "30d"

    def test_cost_flag_absent(self):
        assert build_parser().parse_args(["hi"]).cost is None

    def test_tui_flag(self):
        assert build_parser().parse_args(["--tui"]).tui is True

    def test_session_flag(self):
        args = build_parser().parse_args(["-s", "myconv", "hi"])
        assert args.session == "myconv"

    def test_session_auto(self):
        args = build_parser().parse_args(["-s", "auto", "hi"])
        assert args.session == "auto"

    def test_follow_up_flag(self):
        assert build_parser().parse_args(["-f", "hi"]).follow_up is True

    def test_input_flag(self):
        args = build_parser().parse_args(["-i", "photo.jpg", "describe"])
        assert args.input == ["photo.jpg"]

    def test_input_flag_repeatable(self):
        args = build_parser().parse_args(["-i", "a.jpg", "-i", "b.png", "describe"])
        assert args.input == ["a.jpg", "b.png"]

    def test_quiet_flag(self):
        assert build_parser().parse_args(["-q", "hi"]).quiet is True

    def test_version_flag_exits(self, capsys):
        with pytest.raises(SystemExit) as exc:
            build_parser().parse_args(["--version"])
        assert exc.value.code == 0
        assert "qp" in capsys.readouterr().out


class TestGetContent:
    def test_stdin_fallback(self):
        args = build_parser().parse_args([])
        stdin = MagicMock()
        stdin.isatty.return_value = False
        stdin.read.return_value = "  piped prompt\n"
        with patch("sys.stdin", stdin):
            content, mods = get_content(args)
        assert content == "piped prompt"
        assert mods == set()

    def test_tty_no_words_exits(self):
        args = build_parser().parse_args([])
        stdin = MagicMock()
        stdin.isatty.return_value = True
        with patch("sys.stdin", stdin), pytest.raises(SystemExit) as exc:
            get_content(args)
        assert exc.value.code == 1

    def test_empty_stdin_exits(self):
        args = build_parser().parse_args([])
        stdin = MagicMock()
        stdin.isatty.return_value = False
        stdin.read.return_value = "   "
        with patch("sys.stdin", stdin), pytest.raises(SystemExit) as exc:
            get_content(args)
        assert exc.value.code == 1

    def test_stdin_combined_with_words(self):
        args = build_parser().parse_args(["summarize", "this"])
        stdin = MagicMock()
        stdin.isatty.return_value = False
        stdin.read.return_value = "diff --git a/foo b/foo\n+x\n"
        with patch("sys.stdin", stdin):
            content, mods = get_content(args)
        assert content == "summarize this\n\ndiff --git a/foo b/foo\n+x"
        assert mods == set()

    def test_blank_stdin_with_words_ignored(self):
        args = build_parser().parse_args(["hello"])
        stdin = MagicMock()
        stdin.isatty.return_value = False
        stdin.read.return_value = "  \n"
        with patch("sys.stdin", stdin):
            content, _ = get_content(args)
        assert content == "hello"

    def test_tty_words_skip_stdin_read(self):
        args = build_parser().parse_args(["hello"])
        stdin = MagicMock()
        stdin.isatty.return_value = True
        with patch("sys.stdin", stdin):
            content, _ = get_content(args)
        assert content == "hello"
        stdin.read.assert_not_called()

    def test_stdin_combined_with_media_file(self, tmp_path):
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG")
        args = build_parser().parse_args(["describe", str(img)])
        stdin = MagicMock()
        stdin.isatty.return_value = False
        stdin.read.return_value = "extra context"
        with patch("sys.stdin", stdin):
            content, mods = get_content(args)
        assert mods == {"image"}
        assert isinstance(content, list)
        assert content[0] == {"type": "text", "text": "describe\n\nextra context"}


class TestPrintStats:
    def test_none_prints_nothing(self, capsys):
        quipcli._print_stats(None)
        assert capsys.readouterr().err == ""

    def test_formats_model_tokens_and_cost(self, capsys):
        stats = quipcli._UsageStats(
            model="openai/gpt-4o", prompt_tokens=10, completion_tokens=5, cost_usd=0.002
        )
        quipcli._print_stats(stats)
        err = capsys.readouterr().err
        assert "openai/gpt-4o" in err
        assert "15 tok" in err
        assert "$0.0020" in err

    def test_omits_cost_segment_when_none(self, capsys):
        stats = quipcli._UsageStats(
            model="openai/gpt-4o", prompt_tokens=10, completion_tokens=5, cost_usd=None
        )
        quipcli._print_stats(stats)
        err = capsys.readouterr().err
        assert "$" not in err

    def test_omits_token_segment_when_zero(self, capsys):
        stats = quipcli._UsageStats(
            model="openai/gpt-4o", prompt_tokens=0, completion_tokens=0, cost_usd=None
        )
        quipcli._print_stats(stats)
        err = capsys.readouterr().err
        assert "tok" not in err
        assert "openai/gpt-4o" in err


# ── _mode_prompt ──────────────────────────────────────────────────────────────


class TestModePrompt:
    def test_falls_back_to_default_for_chat(self):
        assert quipcli._mode_prompt({}, "chat") == quipcli.DEFAULT_CHAT_SYSTEM_PROMPT

    def test_falls_back_to_default_for_code(self):
        assert quipcli._mode_prompt({}, "code") == quipcli.CODE_SYSTEM_PROMPT

    def test_falls_back_to_default_for_agent(self):
        assert quipcli._mode_prompt({}, "agent") == quipcli.DEFAULT_AGENT_SYSTEM_PROMPT

    def test_mode_prompt_defaults_includes_all_four_modes(self):
        from quipcli.entry import _MODE_PROMPT_DEFAULTS

        assert set(_MODE_PROMPT_DEFAULTS) == {"chat", "execute", "code", "agent"}

    def test_uses_config_override(self):
        cfg = {"code_system_prompt": "custom code prompt"}
        assert quipcli._mode_prompt(cfg, "code") == "custom code prompt"

    def test_blank_config_value_falls_back_to_default(self):
        cfg = {"chat_system_prompt": ""}
        assert quipcli._mode_prompt(cfg, "chat") == quipcli.DEFAULT_CHAT_SYSTEM_PROMPT

    def test_default_execute_prompt_substitutes_current_shell(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/fish")
        result = quipcli._mode_prompt({}, "execute")
        assert "fish" in result
        assert "{shell}" not in result

    def test_custom_execute_prompt_shell_placeholder_also_substituted(
        self, monkeypatch
    ):
        monkeypatch.setenv("SHELL", "/bin/zsh")
        cfg = {"execute_system_prompt": "Shell is {shell}, be terse."}
        assert quipcli._mode_prompt(cfg, "execute") == "Shell is zsh, be terse."


# ── _models_url ───────────────────────────────────────────────────────────────


class TestModelsUrl:
    @pytest.mark.parametrize(
        "api_url,expected",
        [
            (
                "https://openrouter.ai/api/v1/chat/completions",
                "https://openrouter.ai/api/v1/models",
            ),
            (
                "https://api.openai.com/v1/chat/completions",
                "https://api.openai.com/v1/models",
            ),
            (
                "https://api.groq.com/openai/v1/chat/completions",
                "https://api.groq.com/openai/v1/models",
            ),
        ],
    )
    def test_derives_models_url(self, api_url, expected):
        with patch("quipcli.constants._API_URL", api_url):
            assert _models_url() == expected


# ── _load_models ──────────────────────────────────────────────────────────────


class TestLoadModels:
    def test_missing_cache_returns_empty(self, tmp_path):
        with patch("quipcli.constants._MODELS_CACHE", tmp_path / "models.json"):
            assert _load_models() == []

    def test_valid_cache_sorted(self, tmp_path):
        cache = tmp_path / "models.json"
        cache.write_text(
            json.dumps(
                {
                    "data": [
                        {"id": "openai/gpt-4o"},
                        {"id": "anthropic/claude-3-5-haiku"},
                    ]
                }
            )
        )
        with patch("quipcli.constants._MODELS_CACHE", cache):
            assert _load_models() == ["anthropic/claude-3-5-haiku", "openai/gpt-4o"]

    def test_corrupted_cache_returns_empty(self, tmp_path):
        cache = tmp_path / "models.json"
        cache.write_text("not json{{")
        with patch("quipcli.constants._MODELS_CACHE", cache):
            assert _load_models() == []

    def test_stale_cache_still_returned(self, tmp_path):
        cache = tmp_path / "models.json"
        cache.write_text(json.dumps({"data": [{"id": "old/model"}]}))
        old = time.time() - 999_999
        os.utime(cache, (old, old))
        with patch("quipcli.constants._MODELS_CACHE", cache):
            assert _load_models() == ["old/model"]


# ── _resolve_model_name ───────────────────────────────────────────────────────


class TestResolveModelName:
    def _cache(self, tmp_path, ids):
        cache = tmp_path / "models.json"
        cache.write_text(json.dumps({"data": [{"id": i} for i in ids]}))
        return cache

    def test_empty_cache_passthrough(self, tmp_path):
        with patch("quipcli.constants._MODELS_CACHE", tmp_path / "models.json"):
            assert _resolve_model_name("haiku") == "haiku"

    def test_exact_match_passthrough(self, tmp_path):
        cache = self._cache(tmp_path, ["anthropic/claude-3-5-haiku", "openai/gpt-4o"])
        with patch("quipcli.constants._MODELS_CACHE", cache):
            assert _resolve_model_name("openai/gpt-4o") == "openai/gpt-4o"

    def test_unique_substring_resolves(self, tmp_path):
        cache = self._cache(tmp_path, ["anthropic/claude-3-5-haiku", "openai/gpt-4o"])
        with patch("quipcli.constants._MODELS_CACHE", cache):
            assert _resolve_model_name("haiku") == "anthropic/claude-3-5-haiku"

    def test_no_match_passthrough(self, tmp_path):
        cache = self._cache(tmp_path, ["anthropic/claude-3-5-haiku", "openai/gpt-4o"])
        with patch("quipcli.constants._MODELS_CACHE", cache):
            assert _resolve_model_name("mistral/mixtral") == "mistral/mixtral"

    def test_ambiguous_match_exits(self, tmp_path, capsys):
        cache = self._cache(
            tmp_path,
            [
                "anthropic/claude-3-5-haiku",
                "anthropic/claude-3-opus",
                "openai/gpt-4o",
            ],
        )
        with (
            patch("quipcli.constants._MODELS_CACHE", cache),
            pytest.raises(SystemExit) as exc,
        ):
            _resolve_model_name("claude")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "anthropic/claude-3-5-haiku" in err
        assert "anthropic/claude-3-opus" in err

    def test_empty_name_passthrough(self, tmp_path):
        cache = self._cache(tmp_path, ["anthropic/claude-3-5-haiku"])
        with patch("quipcli.constants._MODELS_CACHE", cache):
            assert _resolve_model_name("") == ""


# ── _maybe_update_models_bg ───────────────────────────────────────────────────


class TestMaybeUpdateModelsBg:
    def test_skips_when_bg_env_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("_LLM_CMD_BG_UPDATE", "1")
        with (
            patch("subprocess.Popen") as popen,
            patch("quipcli.constants._MODELS_CACHE", tmp_path / "missing.json"),
        ):
            _maybe_update_models_bg()
        popen.assert_not_called()

    def test_skips_when_cache_fresh(self, monkeypatch, tmp_path):
        monkeypatch.delenv("_LLM_CMD_BG_UPDATE", raising=False)
        cache = tmp_path / "models.json"
        cache.write_text("{}")
        os.utime(cache, (time.time(), time.time()))
        with (
            patch("subprocess.Popen") as popen,
            patch("quipcli.constants._MODELS_CACHE", cache),
        ):
            _maybe_update_models_bg()
        popen.assert_not_called()

    def test_spawns_when_cache_missing(self, monkeypatch, tmp_path):
        monkeypatch.delenv("_LLM_CMD_BG_UPDATE", raising=False)
        with (
            patch("subprocess.Popen") as popen,
            patch("quipcli.constants._MODELS_CACHE", tmp_path / "missing.json"),
        ):
            _maybe_update_models_bg()
        popen.assert_called_once()

    def test_spawns_when_cache_stale(self, monkeypatch, tmp_path):
        monkeypatch.delenv("_LLM_CMD_BG_UPDATE", raising=False)
        cache = tmp_path / "models.json"
        cache.write_text("{}")
        old = time.time() - (quipcli._CACHE_TTL + 1)
        os.utime(cache, (old, old))
        with (
            patch("subprocess.Popen") as popen,
            patch("quipcli.constants._MODELS_CACHE", cache),
        ):
            _maybe_update_models_bg()
        popen.assert_called_once()

    def test_spawned_process_is_detached(self, monkeypatch, tmp_path):
        monkeypatch.delenv("_LLM_CMD_BG_UPDATE", raising=False)
        with (
            patch("subprocess.Popen") as popen,
            patch("quipcli.constants._MODELS_CACHE", tmp_path / "missing.json"),
        ):
            _maybe_update_models_bg()
        _, kwargs = popen.call_args
        assert kwargs.get("start_new_session") is True
        assert kwargs.get("stdout") == subprocess.DEVNULL
        assert kwargs.get("stdin") == subprocess.DEVNULL
        assert "_LLM_CMD_BG_UPDATE" in kwargs.get("env", {})


# ── _fetch_models ────────────────────────────────────────────────────────────


class TestFetchModels:
    def test_success_caches_and_returns_sorted_ids(self, tmp_path, mock_http):
        body = json.dumps(
            {"data": [{"id": "openai/gpt-4o"}, {"id": "anthropic/claude-3-5-haiku"}]}
        ).encode()
        mock_http(MockHTTPResponse(200, body))
        cache = tmp_path / "models.json"
        with (
            patch("quipcli.constants._MODELS_CACHE", cache),
            patch("quipcli.constants._CACHE_DIR", tmp_path),
        ):
            ids = quipcli._fetch_models()
        assert ids == ["anthropic/claude-3-5-haiku", "openai/gpt-4o"]
        assert json.loads(cache.read_text())["data"][0]["id"] == "openai/gpt-4o"

    def test_connection_error_returns_empty(self, capsys):
        with patch("http.client.HTTPSConnection") as cls:
            cls.return_value.request.side_effect = OSError("connection refused")
            assert quipcli._fetch_models() == []
        assert "connection failed" in capsys.readouterr().err

    def test_non_200_returns_empty(self, mock_http, capsys):
        mock_http(MockHTTPResponse(500, b"server error"))
        assert quipcli._fetch_models() == []
        assert "HTTP 500" in capsys.readouterr().err

    def test_invalid_json_returns_empty(self, mock_http, capsys):
        mock_http(MockHTTPResponse(200, b"not json{{"))
        assert quipcli._fetch_models() == []
        assert "invalid JSON" in capsys.readouterr().err


# ── _fetch_rankings / _load_rankings / _ranking_for ─────────────────────────────


class TestFetchRankings:
    def test_success_caches_latest_day_sorted_desc(self, tmp_path, mock_http):
        body = json.dumps(
            {
                "data": [
                    {
                        "date": "2026-07-22",
                        "model_permaslug": "openai/gpt-4o",
                        "total_tokens": "999",
                    },
                    {
                        "date": "2026-07-23",
                        "model_permaslug": "other",
                        "total_tokens": "500",
                    },
                    {
                        "date": "2026-07-23",
                        "model_permaslug": "anthropic/claude-3-5-sonnet",
                        "total_tokens": "200",
                    },
                    {
                        "date": "2026-07-23",
                        "model_permaslug": "openai/gpt-4o",
                        "total_tokens": "800",
                    },
                ]
            }
        ).encode()
        mock_http(MockHTTPResponse(200, body))
        cache = tmp_path / "rankings.json"
        with (
            patch("quipcli.constants._RANKINGS_CACHE", cache),
            patch("quipcli.constants._CACHE_DIR", tmp_path),
            patch("quipcli.constants._API_URL", quipcli.constants._DEFAULT_API_URL),
        ):
            ranked = quipcli._fetch_rankings()
        assert ranked == [
            {"rank": 1, "model_permaslug": "openai/gpt-4o", "total_tokens": 800},
            {
                "rank": 2,
                "model_permaslug": "anthropic/claude-3-5-sonnet",
                "total_tokens": 200,
            },
        ]
        assert json.loads(cache.read_text())["date"] == "2026-07-23"

    def test_skips_non_openrouter_provider(self):
        with (
            patch(
                "quipcli.constants._API_URL",
                "https://api.groq.com/openai/v1/chat/completions",
            ),
            patch("quipcli.constants._API_KEY", "key"),
        ):
            assert quipcli._fetch_rankings() == []

    def test_skips_without_api_key(self):
        with (
            patch("quipcli.constants._API_URL", quipcli.constants._DEFAULT_API_URL),
            patch("quipcli.constants._API_KEY", ""),
        ):
            assert quipcli._fetch_rankings() == []

    def test_non_200_returns_empty(self, mock_http):
        mock_http(MockHTTPResponse(401, b"unauthorized"))
        with patch("quipcli.constants._API_URL", quipcli.constants._DEFAULT_API_URL):
            assert quipcli._fetch_rankings() == []

    def test_invalid_json_returns_empty(self, mock_http):
        mock_http(MockHTTPResponse(200, b"not json{{"))
        with patch("quipcli.constants._API_URL", quipcli.constants._DEFAULT_API_URL):
            assert quipcli._fetch_rankings() == []


class TestRankingFor:
    def test_returns_none_when_no_cache(self, tmp_path):
        with patch("quipcli.constants._RANKINGS_CACHE", tmp_path / "missing.json"):
            assert quipcli._ranking_for("openai/gpt-4o") is None

    def test_empty_permaslug_returns_none(self, tmp_path):
        with patch("quipcli.constants._RANKINGS_CACHE", tmp_path / "missing.json"):
            assert quipcli._ranking_for("") is None

    def test_finds_matching_permaslug(self, tmp_path):
        cache = tmp_path / "rankings.json"
        cache.write_text(
            json.dumps(
                {
                    "date": "2026-07-23",
                    "data": [
                        {
                            "rank": 1,
                            "model_permaslug": "openai/gpt-4o",
                            "total_tokens": 100,
                        },
                    ],
                }
            )
        )
        with patch("quipcli.constants._RANKINGS_CACHE", cache):
            assert quipcli._ranking_for("openai/gpt-4o") == {
                "rank": 1,
                "model_permaslug": "openai/gpt-4o",
                "total_tokens": 100,
            }


# ── HTTP layer ────────────────────────────────────────────────────────────────


class TestMakeRequest:
    def _msgs(self, prompt="p", system=None):
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})
        return msgs

    def test_no_api_key_no_ollama_exits(self):
        with (
            patch("quipcli.constants._API_KEY", ""),
            patch("quipcli.http_client._ollama_models", return_value=None),
            pytest.raises(SystemExit) as exc,
        ):
            quipcli._make_request(self._msgs(), "m", False)
        assert exc.value.code == 1

    def test_connection_error_no_ollama_exits(self):
        with (
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.http_client.time.sleep") as sleep,
            patch("quipcli.http_client._ollama_models", return_value=None),
        ):
            cls.return_value.request.side_effect = OSError("connection refused")
            with (
                patch("quipcli.constants._API_KEY", "key"),
                pytest.raises(SystemExit) as exc,
            ):
                quipcli._make_request(self._msgs(), "m", False)
        assert exc.value.code == 1
        assert sleep.call_count == 3  # all backoff waits exhausted

    def test_http_error_exits(self, mock_http):
        mock_http(MockHTTPResponse(401, b'{"error":"unauthorized"}'))
        with pytest.raises(SystemExit) as exc:
            quipcli._make_request(self._msgs(), "m", False)
        assert exc.value.code == 1

    def test_retries_on_429_then_succeeds(self):
        limited = MockHTTPResponse(429, b"rate limited")
        ok = MockHTTPResponse(200, b"ok")
        with (
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch("quipcli.http_client.time.sleep") as sleep,
        ):
            cls.side_effect = [_mock_conn(limited), _mock_conn(ok)]
            resp, used = quipcli._make_request(self._msgs(), "m", False)
        assert resp.status == 200
        assert used == "m"
        assert sleep.call_count == 1

    @pytest.mark.parametrize("status", [500, 502, 503, 529])
    def test_retries_on_other_retriable_statuses_then_succeeds(self, status):
        failing = MockHTTPResponse(status, b"transient error")
        ok = MockHTTPResponse(200, b"ok")
        with (
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch("quipcli.http_client.time.sleep") as sleep,
        ):
            cls.side_effect = [_mock_conn(failing), _mock_conn(ok)]
            resp, used = quipcli._make_request(self._msgs(), "m", False)
        assert resp.status == 200
        assert used == "m"
        assert sleep.call_count == 1

    def test_retriable_status_exhausts_retries_and_exits(self):
        with (
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch("quipcli.http_client.time.sleep") as sleep,
            patch("quipcli.http_client._ollama_models", return_value=None),
        ):
            cls.return_value = _mock_conn(MockHTTPResponse(503, b"still down"))
            with pytest.raises(SystemExit) as exc:
                quipcli._make_request(self._msgs(), "m", False)
        assert exc.value.code == 1
        assert (
            sleep.call_count == 3
        )  # all backoff waits exhausted, no Ollama fallback for API status errors

    def test_no_retry_on_client_error(self, capsys):
        with (
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch("quipcli.http_client.time.sleep") as sleep,
        ):
            cls.return_value = _mock_conn(MockHTTPResponse(404, b"not found"))
            with pytest.raises(SystemExit) as exc:
                quipcli._make_request(self._msgs(), "m", False)
        assert exc.value.code == 1
        sleep.assert_not_called()

    def test_system_message_included(self, mock_http):
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        http_cls = mock_http(MockHTTPResponse(200, body))
        msgs = self._msgs("prompt", "be terse")
        quipcli._make_request(msgs, "model", False)
        _, _, body_arg, _ = http_cls.return_value.request.call_args[0]
        sent_msgs = json.loads(body_arg)["messages"]
        assert sent_msgs[0] == {"role": "system", "content": "be terse"}
        assert sent_msgs[1] == {"role": "user", "content": "prompt"}

    def test_no_system_message_when_none(self, mock_http):
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        http_cls = mock_http(MockHTTPResponse(200, body))
        msgs = self._msgs("prompt")
        quipcli._make_request(msgs, "model", False)
        _, _, body_arg, _ = http_cls.return_value.request.call_args[0]
        sent_msgs = json.loads(body_arg)["messages"]
        assert len(sent_msgs) == 1
        assert sent_msgs[0]["role"] == "user"

    def test_stream_options_added_when_streaming(self, mock_http):
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        http_cls = mock_http(MockHTTPResponse(200, body))
        quipcli._make_request(self._msgs(), "m", stream=True, include_usage=True)
        _, _, body_arg, _ = http_cls.return_value.request.call_args[0]
        assert json.loads(body_arg).get("stream_options") == {"include_usage": True}

    def test_no_stream_options_without_flag(self, mock_http):
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        http_cls = mock_http(MockHTTPResponse(200, body))
        quipcli._make_request(self._msgs(), "m", stream=True, include_usage=False)
        _, _, body_arg, _ = http_cls.return_value.request.call_args[0]
        assert "stream_options" not in json.loads(body_arg)


class TestOllamaFallback:
    def _msgs(self):
        return [{"role": "user", "content": "p"}]

    def _tags_resp(self, names):
        body = json.dumps({"models": [{"name": n} for n in names]}).encode()
        return MockHTTPResponse(200, body)

    def test_falls_back_when_provider_unreachable(self, capsys):
        ok = MockHTTPResponse(200, b"ok")
        with (
            patch("http.client.HTTPSConnection") as https_cls,
            patch("http.client.HTTPConnection") as http_cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch("quipcli.http_client.time.sleep"),
        ):
            https_cls.return_value.request.side_effect = OSError("no route")
            http_cls.side_effect = [
                _mock_conn(self._tags_resp(["llama3.2"])),
                _mock_conn(ok),
            ]
            resp, used = quipcli._make_request(self._msgs(), "openai/gpt-4o", False)
        assert resp.status == 200
        assert used == "llama3.2"
        assert "falling back to Ollama" in capsys.readouterr().err

    def test_no_api_key_uses_ollama(self, capsys):
        ok = MockHTTPResponse(200, b"ok")
        with (
            patch("http.client.HTTPConnection") as http_cls,
            patch("quipcli.constants._API_KEY", ""),
            patch("quipcli.constants._CONFIG_FILE", Path("/nonexistent/config.json")),
        ):
            http_cls.side_effect = [
                _mock_conn(self._tags_resp(["qwen3:8b"])),
                _mock_conn(ok),
            ]
            resp, used = quipcli._make_request(self._msgs(), "m", False)
        assert resp.status == 200
        assert used == "qwen3:8b"

    def test_config_ollama_model_preferred(self):
        from quipcli.http_client import _pick_ollama_model

        assert _pick_ollama_model(["a", "b"], {"ollama_model": "b"}) == "b"
        assert _pick_ollama_model(["a", "b"], {}) == "a"

    def test_no_fallback_on_api_status_error(self, capsys):
        with (
            patch("http.client.HTTPSConnection") as https_cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch("quipcli.http_client._ollama_models") as tags,
        ):
            https_cls.return_value = _mock_conn(MockHTTPResponse(401, b"unauthorized"))
            with pytest.raises(SystemExit):
                quipcli._make_request(self._msgs(), "m", False)
        tags.assert_not_called()

    def test_ollama_unreachable_returns_none(self):
        with patch("http.client.HTTPConnection") as http_cls:
            http_cls.return_value.request.side_effect = OSError("refused")
            assert quipcli.http_client._ollama_models() is None

    def test_ollama_empty_model_list_returns_none(self):
        with patch("http.client.HTTPConnection") as http_cls:
            http_cls.return_value = _mock_conn(self._tags_resp([]))
            assert quipcli.http_client._ollama_models() is None


class TestCallLlmStreaming:
    def _msgs(self):
        return [{"role": "user", "content": "hi"}]

    def test_prints_content(self, mock_http, capsys):
        lines = [
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n',
            b'data: {"choices":[{"delta":{"content":" world"}}]}\n',
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        text, _ = quipcli.call_llm_streaming(self._msgs(), "m")
        assert "Hello world" in capsys.readouterr().out
        assert text == "Hello world"

    def test_skips_non_data_lines(self, mock_http, capsys):
        lines = [
            b": keep-alive\n",
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n',
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        _text, _stats = quipcli.call_llm_streaming(self._msgs(), "m")
        assert "ok" in capsys.readouterr().out

    def test_stops_at_done(self, mock_http, capsys):
        lines = [
            b"data: [DONE]\n",
            b'data: {"choices":[{"delta":{"content":"SHOULD NOT APPEAR"}}]}\n',
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        quipcli.call_llm_streaming(self._msgs(), "m")
        assert "SHOULD NOT APPEAR" not in capsys.readouterr().out

    def test_captures_usage_chunk(self, mock_http, capsys):
        lines = [
            b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":8,"cost":0.001}}\n',
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        _text, stats = quipcli.call_llm_streaming(self._msgs(), "m", collect_usage=True)
        assert stats is not None
        assert stats.prompt_tokens == 5
        assert stats.completion_tokens == 8
        assert stats.cost_usd == pytest.approx(0.001)
        assert "Hello" in capsys.readouterr().out

    def test_no_usage_when_not_requested(self, mock_http, capsys):
        lines = [
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":8}}\n',
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        _, stats = quipcli.call_llm_streaming(self._msgs(), "m", collect_usage=False)
        assert stats is None

    def test_markdown_rendering_adds_ansi_when_enabled(self, mock_http, capsys):
        lines = [
            b'data: {"choices":[{"delta":{"content":"# Heading\\nUse `code` and **bold**"}}]}\n',
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        with patch("quipcli.http_client._use_markdown_rendering", return_value=True):
            quipcli.call_llm_streaming(self._msgs(), "m", render_markdown=True)
        out = capsys.readouterr().out
        assert "\x1b[" in out
        assert "Heading" in out
        assert "code" in out
        assert "bold" in out

    def test_markdown_rendering_disabled_is_plain_text(self, mock_http, capsys):
        lines = [
            b'data: {"choices":[{"delta":{"content":"# Heading\\nUse `code`"}}]}\n',
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        with patch("quipcli.http_client._use_markdown_rendering", return_value=True):
            quipcli.call_llm_streaming(self._msgs(), "m", render_markdown=False)
        out = capsys.readouterr().out
        assert "\x1b[" not in out
        assert "# Heading" in out

    def test_markdown_rendering_handles_embedded_fences(self, mock_http, capsys):
        content = "````markdown\n```python\nprint('x')\n```\n````\nplain\n"
        lines = [
            f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n".encode(),
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        with patch("quipcli.http_client._use_markdown_rendering", return_value=True):
            quipcli.call_llm_streaming(self._msgs(), "m", render_markdown=True)
        out = capsys.readouterr().out
        plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
        assert plain == content + "\n"

    def test_markdown_fence_not_colored_as_code(self, mock_http, capsys):
        content = "```md\n# title\n```\nplain\n"
        lines = [
            f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n".encode(),
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        with patch("quipcli.http_client._use_markdown_rendering", return_value=True):
            quipcli.call_llm_streaming(self._msgs(), "m", render_markdown=True)
        out = capsys.readouterr().out
        assert "\x1b[38;5;150m" not in out

    def test_markdown_rendering_styles_list_items(self, mock_http, capsys):
        content = (
            "- bullet one\n* bullet two\n+ bullet three\n1. numbered\n2) numbered\n"
        )
        lines = [
            f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n".encode(),
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        with patch("quipcli.http_client._use_markdown_rendering", return_value=True):
            quipcli.call_llm_streaming(self._msgs(), "m", render_markdown=True)
        out = capsys.readouterr().out
        plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
        assert plain == content + "\n"
        assert "\x1b[38;5;215m-" in out
        assert "\x1b[38;5;215m*" in out
        assert "\x1b[38;5;215m+" in out
        assert "\x1b[38;5;215m1." in out
        assert "\x1b[38;5;215m2)" in out

    def test_markdown_rendering_styles_blockquote(self, mock_http, capsys):
        content = "> quoted line\nplain line\n"
        lines = [
            f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n".encode(),
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        with patch("quipcli.http_client._use_markdown_rendering", return_value=True):
            quipcli.call_llm_streaming(self._msgs(), "m", render_markdown=True)
        out = capsys.readouterr().out
        plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
        assert plain == content + "\n"
        assert "\x1b[2;3m>" in out

    def test_markdown_rendering_bold_not_confused_with_list(self, mock_http, capsys):
        content = "**bold** text\n"
        lines = [
            f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n".encode(),
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        with patch("quipcli.http_client._use_markdown_rendering", return_value=True):
            quipcli.call_llm_streaming(self._msgs(), "m", render_markdown=True)
        out = capsys.readouterr().out
        plain = re.sub(r"\x1b\[[0-9;]*m", "", out)
        assert plain == content + "\n"
        assert "\x1b[38;5;215m" not in out

    def test_markdown_rendering_across_small_unterminated_chunks_matches_single_chunk(
        self, mock_http, capsys
    ):
        # Real streaming sends many small token deltas with no trailing
        # newline — exercises the carry-buffer branch in render(), unlike
        # every other markdown test above which feeds one full,
        # newline-terminated chunk.
        parts = ["Use `co", "de` and **bo", "ld** here\n"]
        lines = [
            f"data: {json.dumps({'choices': [{'delta': {'content': p}}]})}\n".encode()
            for p in parts
        ]
        lines.append(b"data: [DONE]\n")
        mock_http(MockHTTPResponse(200, b"", lines))
        with patch("quipcli.http_client._use_markdown_rendering", return_value=True):
            quipcli.call_llm_streaming(self._msgs(), "m", render_markdown=True)
        split_out = capsys.readouterr().out

        whole_lines = [
            f"data: {json.dumps({'choices': [{'delta': {'content': ''.join(parts)}}]})}\n".encode(),
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", whole_lines))
        with patch("quipcli.http_client._use_markdown_rendering", return_value=True):
            quipcli.call_llm_streaming(self._msgs(), "m", render_markdown=True)
        whole_out = capsys.readouterr().out

        assert split_out == whole_out

    def test_stream_ending_mid_bold_still_resets_terminal(self, mock_http, capsys):
        lines = [
            b'data: {"choices":[{"delta":{"content":"**unclosed bold"}}]}\n',
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        with patch("quipcli.http_client._use_markdown_rendering", return_value=True):
            quipcli.call_llm_streaming(self._msgs(), "m", render_markdown=True)
        out = capsys.readouterr().out
        assert out.endswith("\x1b[0m\n")


class TestMarkdownAnsiRenderer:
    """Direct unit tests for the carry-buffer branches in render()/finish()
    that the streaming tests above only exercise indirectly."""

    def _renderer(self):
        return quipcli.http_client._MarkdownAnsiRenderer()

    def test_render_buffers_when_no_newline_yet(self):
        r = self._renderer()
        assert r.render("no newline here") == ""
        assert r._carry == "no newline here"

    def test_render_flushes_up_to_last_newline_and_keeps_remainder(self):
        r = self._renderer()
        out = r.render("first line\nsecond ")
        assert out == "first line\n"
        assert r._carry == "second "

    def test_finish_with_clean_trailing_state_has_no_reset(self):
        r = self._renderer()
        r.render("plain text\n")
        assert r.finish() == ""

    def test_finish_flushes_unterminated_bold_and_resets(self):
        r = self._renderer()
        r.render("**bold text without a closing marker")
        tail = r.finish()
        assert tail.endswith(r._RESET)
        assert r._in_bold is False

    def test_finish_flushes_unterminated_fenced_code_and_resets(self):
        r = self._renderer()
        r.render("```python\ncode with no closing fence")
        tail = r.finish()
        assert tail.endswith(r._RESET)
        assert r._in_fenced_code is False


# ── _make_request extra param ────────────────────────────────────────────────


class TestMakeRequestExtra:
    def _msgs(self):
        return [{"role": "user", "content": "p"}]

    def test_extra_merged_into_body(self, mock_http):
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        http_cls = mock_http(MockHTTPResponse(200, body))
        tools = [{"type": "function", "function": {"name": "run_shell"}}]
        quipcli._make_request(self._msgs(), "m", False, extra={"tools": tools})
        _, _, body_arg, _ = http_cls.return_value.request.call_args[0]
        sent = json.loads(body_arg)
        assert sent["tools"] == tools

    def test_no_extra_fields_when_omitted(self, mock_http):
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        http_cls = mock_http(MockHTTPResponse(200, body))
        quipcli._make_request(self._msgs(), "m", False)
        _, _, body_arg, _ = http_cls.return_value.request.call_args[0]
        assert "tools" not in json.loads(body_arg)


# ── tools.py ──────────────────────────────────────────────────────────────────


class TestToolsModule:
    def test_default_tools_includes_server_tools_by_default(self):
        tools = quipcli.default_tools()
        types = [t["type"] for t in tools]
        assert "openrouter:web_search" in types
        assert "openrouter:web_fetch" in types

    def test_default_tools_excludes_server_tools_when_disabled(self):
        tools = quipcli.default_tools(include_server_tools=False)
        types = [t["type"] for t in tools]
        assert "openrouter:web_search" not in types
        assert "openrouter:web_fetch" not in types
        assert all(t["type"] == "function" for t in tools)

    def test_default_tools_includes_local_function_names(self):
        names = [
            t["function"]["name"]
            for t in quipcli.default_tools()
            if t["type"] == "function"
        ]
        assert set(names) == {"run_shell", "read_file", "write_file"}

    def test_execute_tool_call_unknown_tool(self):
        assert "unknown tool" in quipcli.execute_tool_call("nope", "{}")

    def test_execute_tool_call_invalid_json(self):
        assert "invalid arguments" in quipcli.execute_tool_call(
            "read_file", "{not json"
        )

    def test_execute_tool_call_dispatches_read_file(self, tmp_path):
        f = tmp_path / "note.txt"
        f.write_text("hello there")
        result = quipcli.execute_tool_call("read_file", json.dumps({"path": str(f)}))
        assert result == "hello there"

    def test_read_file_missing_returns_error(self, tmp_path):
        result = quipcli.read_file({"path": str(tmp_path / "missing.txt")})
        assert "Error reading file" in result

    def test_read_file_truncates_long_content(self, tmp_path):
        f = tmp_path / "big.txt"
        f.write_text("x" * 20000)
        result = quipcli.read_file({"path": str(f)})
        assert result.endswith("...[truncated]")
        assert len(result) < 20000

    def test_write_file_confirmed_writes_content(self, tmp_path):
        target = tmp_path / "sub" / "out.txt"
        with patch("builtins.input", return_value="y"):
            result = quipcli.write_file({"path": str(target), "content": "hi there"})
        assert target.read_text() == "hi there"
        assert "Wrote" in result

    def test_write_file_declined_does_not_write(self, tmp_path):
        target = tmp_path / "out.txt"
        with patch("builtins.input", return_value="n"):
            result = quipcli.write_file({"path": str(target), "content": "hi"})
        assert not target.exists()
        assert "declined" in result

    def test_write_file_ctrl_c_declines(self, tmp_path):
        target = tmp_path / "out.txt"
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            result = quipcli.write_file({"path": str(target), "content": "hi"})
        assert not target.exists()
        assert "declined" in result

    def test_run_shell_confirmed_runs_command(self):
        with patch("quipcli.tools.subprocess.run") as run:
            run.return_value.stdout = "output\n"
            run.return_value.stderr = ""
            run.return_value.returncode = 0
            with patch("builtins.input", return_value="y"):
                result = quipcli.run_shell({"command": "echo hi"})
        run.assert_called_once_with(
            "echo hi",
            shell=True,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result == "output"

    def test_run_shell_declined_does_not_run(self):
        with (
            patch("quipcli.tools.subprocess.run") as run,
            patch("builtins.input", return_value="n"),
        ):
            result = quipcli.run_shell({"command": "rm -rf /"})
        run.assert_not_called()
        assert "declined" in result

    def test_run_shell_no_output_reports_exit_code(self):
        with patch("quipcli.tools.subprocess.run") as run:
            run.return_value.stdout = ""
            run.return_value.stderr = ""
            run.return_value.returncode = 0
            with patch("builtins.input", return_value="y"):
                result = quipcli.run_shell({"command": "true"})
        assert "exit code 0" in result

    def test_run_shell_timeout(self):
        with (
            patch(
                "quipcli.tools.subprocess.run",
                side_effect=subprocess.TimeoutExpired("cmd", 120),
            ),
            patch("builtins.input", return_value="y"),
        ):
            result = quipcli.run_shell({"command": "sleep 999"})
        assert "timed out" in result


# ── agent.py ──────────────────────────────────────────────────────────────────


class TestAgentLoop:
    def _msgs(self):
        return [{"role": "user", "content": "do the thing"}]

    def _response(self, message, usage=None):
        body = {"choices": [{"message": message}]}
        if usage is not None:
            body["usage"] = usage
        return MockHTTPResponse(200, json.dumps(body).encode())

    def test_final_answer_without_tool_calls(self, capsys):
        resp = self._response(
            {"role": "assistant", "content": "here is the answer"},
            usage={"prompt_tokens": 5, "completion_tokens": 3},
        )
        with (
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
        ):
            cls.return_value = _mock_conn(resp)
            text, stats = quipcli.run_agent_loop(
                self._msgs(), "m", render_markdown=False
            )
        assert text == "here is the answer"
        assert "here is the answer" in capsys.readouterr().out
        assert stats is not None
        assert stats.prompt_tokens == 5
        assert stats.completion_tokens == 3

    def test_runs_local_tool_then_returns_final_answer(self, capsys):
        tool_call_resp = self._response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "/tmp/x"}),
                        },
                    }
                ],
            },
            usage={"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
        )
        final_resp = self._response(
            {"role": "assistant", "content": "the file says hello"},
            usage={"prompt_tokens": 20, "completion_tokens": 8, "cost": 0.002},
        )
        with (
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch("quipcli.agent.execute_tool_call", return_value="hello") as exec_tool,
        ):
            cls.side_effect = [_mock_conn(tool_call_resp), _mock_conn(final_resp)]
            messages = self._msgs()
            text, stats = quipcli.run_agent_loop(messages, "m", render_markdown=False)
        exec_tool.assert_called_once_with("read_file", json.dumps({"path": "/tmp/x"}))
        assert text == "the file says hello"
        # tool result appended to the conversation before the final call
        tool_msgs = [m for m in messages if m.get("role") == "tool"]
        assert tool_msgs == [
            {"role": "tool", "tool_call_id": "call_1", "content": "hello"}
        ]
        # usage accumulated across both steps
        assert stats is not None
        assert stats.prompt_tokens == 30
        assert stats.completion_tokens == 13
        assert stats.cost_usd == pytest.approx(0.003)

    def test_server_tool_calls_are_not_executed_locally(self, capsys):
        resp = self._response(
            {
                "role": "assistant",
                "content": "grounded answer",
                "tool_calls": [
                    {"id": "call_1", "type": "openrouter:web_search", "function": {}}
                ],
            }
        )
        with (
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch("quipcli.agent.execute_tool_call") as exec_tool,
        ):
            cls.return_value = _mock_conn(resp)
            text, _ = quipcli.run_agent_loop(self._msgs(), "m", render_markdown=False)
        exec_tool.assert_not_called()
        assert text == "grounded answer"

    def test_max_steps_reached_asks_for_final_answer(self, capsys):
        looping_resp = self._response(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "run_shell", "arguments": "{}"},
                    }
                ],
            }
        )
        final_resp = self._response({"role": "assistant", "content": "summary"})
        with (
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch("quipcli.agent.execute_tool_call", return_value="ok"),
        ):
            conns = [_mock_conn(looping_resp) for _ in range(3)]
            conns.append(_mock_conn(final_resp))
            cls.side_effect = conns
            text, stats = quipcli.run_agent_loop(
                self._msgs(), "m", max_steps=3, render_markdown=False
            )
        assert cls.call_count == 4
        _, _, body_arg, _ = conns[-1].request.call_args[0]
        assert json.loads(body_arg)["tool_choice"] == "none"
        # The wrap-up reply is the answer — never the last raw tool output.
        assert text == "summary"
        assert "max steps" in capsys.readouterr().err
        assert stats is not None

    def test_server_tools_dropped_for_non_openrouter_provider(self, capsys):
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        with (
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch(
                "quipcli.constants._API_URL",
                "https://api.example.com/v1/chat/completions",
            ),
        ):
            cls.return_value = _mock_conn(MockHTTPResponse(200, body))
            quipcli.run_agent_loop(self._msgs(), "m", render_markdown=False)
        _, _, body_arg, _ = cls.return_value.request.call_args[0]
        sent_types = {t["type"] for t in json.loads(body_arg)["tools"]}
        assert sent_types == {"function"}

    def test_no_web_excludes_server_tools_from_request(self, mock_http):
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        http_cls = mock_http(MockHTTPResponse(200, body))
        quipcli.run_agent_loop(
            self._msgs(), "m", server_tools=False, render_markdown=False
        )
        _, _, body_arg, _ = http_cls.return_value.request.call_args[0]
        sent_types = [t["type"] for t in json.loads(body_arg)["tools"]]
        assert "openrouter:web_search" not in sent_types
        assert "openrouter:web_fetch" not in sent_types

    def test_error_payload_exits(self, mock_http, capsys):
        mock_http(
            MockHTTPResponse(200, json.dumps({"error": {"message": "boom"}}).encode())
        )
        with pytest.raises(SystemExit) as exc:
            quipcli.run_agent_loop(self._msgs(), "m", render_markdown=False)
        assert exc.value.code == 1
        assert "boom" in capsys.readouterr().err

    def test_malformed_json_response_exits(self, mock_http, capsys):
        mock_http(MockHTTPResponse(200, b"not json{{"))
        with pytest.raises(SystemExit) as exc:
            quipcli.run_agent_loop(self._msgs(), "m", render_markdown=False)
        assert exc.value.code == 1
        assert "unexpected API response" in capsys.readouterr().err

    def test_final_answer_rendered_as_markdown_when_enabled(self, mock_http, capsys):
        resp = self._response({"role": "assistant", "content": "**bold** answer"})
        mock_http(resp)
        with patch("quipcli.agent._use_markdown_rendering", return_value=True):
            quipcli.run_agent_loop(self._msgs(), "m", render_markdown=True)
        out = capsys.readouterr().out
        assert "\x1b[" in out
        assert "bold" in out


class TestReasoning:
    def _msgs(self):
        return [{"role": "user", "content": "hi"}]

    def test_reasoning_extra_cli_wins_over_config(self):
        from quipcli.entry import _reasoning_extra

        assert _reasoning_extra("low", {"reasoning_effort": "high"}) == {
            "reasoning": {"effort": "low"}
        }

    def test_reasoning_extra_from_config(self):
        from quipcli.entry import _reasoning_extra

        assert _reasoning_extra(None, {"reasoning_effort": "high"}) == {
            "reasoning": {"effort": "high"}
        }

    def test_reasoning_extra_absent_sends_nothing(self):
        from quipcli.entry import _reasoning_extra

        assert _reasoning_extra(None, {}) is None
        assert _reasoning_extra(None, {"reasoning_effort": ""}) is None

    def test_reasoning_flag_parses(self):
        args = quipcli.build_parser().parse_args(["-r", "low", "hello"])
        assert args.reasoning == "low"

    def test_reasoning_flag_rejects_unknown_effort(self):
        with pytest.raises(SystemExit):
            quipcli.build_parser().parse_args(["-r", "turbo", "hello"])

    def test_streaming_sends_reasoning_extra(self, mock_http, capsys):
        http_cls = mock_http(MockHTTPResponse(200, b"", [b"data: [DONE]\n"]))
        quipcli.call_llm_streaming(
            self._msgs(), "m", extra={"reasoning": {"effort": "low"}}
        )
        _, _, body_arg, _ = http_cls.return_value.request.call_args[0]
        assert json.loads(body_arg)["reasoning"] == {"effort": "low"}

    def _reasoning_lines(self):
        return [
            b'data: {"choices":[{"delta":{"content":"","reasoning":"thinking hard"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"Answer"}}]}\n',
            b"data: [DONE]\n",
        ]

    def test_reasoning_shown_on_stderr_not_stdout(self, mock_http, capsys):
        mock_http(MockHTTPResponse(200, b"", self._reasoning_lines()))
        with patch("sys.stderr.isatty", return_value=True):
            text, _ = quipcli.call_llm_streaming(self._msgs(), "m", show_reasoning=True)
        captured = capsys.readouterr()
        assert "thinking hard" in captured.err
        assert "thinking hard" not in captured.out
        assert text == "Answer"

    def test_reasoning_hidden_when_disabled(self, mock_http, capsys):
        mock_http(MockHTTPResponse(200, b"", self._reasoning_lines()))
        with patch("sys.stderr.isatty", return_value=True):
            quipcli.call_llm_streaming(self._msgs(), "m", show_reasoning=False)
        assert "thinking hard" not in capsys.readouterr().err

    def test_reasoning_hidden_when_stderr_not_tty(self, mock_http, capsys):
        mock_http(MockHTTPResponse(200, b"", self._reasoning_lines()))
        with patch("sys.stderr.isatty", return_value=False):
            quipcli.call_llm_streaming(self._msgs(), "m", show_reasoning=True)
        assert "thinking hard" not in capsys.readouterr().err


class TestCheckReasoningSupport:
    def _cache(self, tmp_path, reasoning):
        f = tmp_path / "models.json"
        entry = (
            {"id": "m"} if reasoning is None else {"id": "m", "reasoning": reasoning}
        )
        f.write_text(json.dumps({"data": [entry]}))
        return f

    def _run(self, tmp_path, reasoning, effort, model="m"):
        from quipcli.models import _check_reasoning_support

        with patch("quipcli.constants._MODELS_CACHE", self._cache(tmp_path, reasoning)):
            _check_reasoning_support(model, effort)

    def test_supported_effort_is_silent(self, tmp_path, capsys):
        self._run(tmp_path, {"supported_efforts": ["max", "high", "low"]}, "low")
        assert capsys.readouterr().err == ""

    def test_unsupported_effort_warns(self, tmp_path, capsys):
        self._run(tmp_path, {"supported_efforts": ["max", "high", "low"]}, "medium")
        assert "max, high, low" in capsys.readouterr().err

    def test_none_on_mandatory_warns(self, tmp_path, capsys):
        self._run(tmp_path, {"supported_efforts": None, "mandatory": True}, "none")
        assert "requires reasoning" in capsys.readouterr().err

    def test_null_supported_efforts_accepts_all(self, tmp_path, capsys):
        self._run(tmp_path, {"supported_efforts": None}, "minimal")
        assert capsys.readouterr().err == ""

    def test_non_reasoning_model_warns(self, tmp_path, capsys):
        self._run(tmp_path, None, "low")
        assert "no reasoning effort control" in capsys.readouterr().err

    def test_cache_miss_is_silent(self, tmp_path, capsys):
        self._run(tmp_path, {"supported_efforts": ["low"]}, "high", model="other")
        assert capsys.readouterr().err == ""


class TestReviewFixes:
    """Regression tests for the dev/review-polish fixes."""

    def _msgs(self):
        return [{"role": "user", "content": "hi"}]

    @pytest.mark.parametrize(
        "raw",
        [
            "```zsh\nls -la\n```",
            "```shell\nls -la\n```",
            "```console\nls -la\n```",
            "$ ls -la",
        ],
    )
    def test_strip_fences_any_info_string(self, raw):
        assert _strip_fences(raw) == "ls -la"

    def test_empty_command_is_not_run(self, capsys):
        with patch("subprocess.run") as run, pytest.raises(SystemExit) as exc:
            quipcli.confirm_and_run("```\n```", "p")
        assert exc.value.code == 1
        run.assert_not_called()
        assert "no command" in capsys.readouterr().err

    def test_confirm_prompt_goes_to_stderr(self, capsys):
        with patch("builtins.input", return_value="n"), pytest.raises(SystemExit):
            quipcli.confirm_and_run("ls", "p")
        captured = capsys.readouterr()
        assert "Run this command?" in captured.err
        assert "Run this command?" not in captured.out

    def test_stream_error_chunk_is_reported(self, mock_http, capsys):
        lines = [
            b'data: {"error":{"message":"provider overloaded"},"choices":[{"delta":{},"finish_reason":"error"}]}\n',
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        quipcli.call_llm_streaming(self._msgs(), "m")
        assert "provider overloaded" in capsys.readouterr().err

    def test_empty_stream_warns(self, mock_http, capsys):
        lines = [
            b'data: {"choices":[{"delta":{"content":""},"finish_reason":"length"}]}\n',
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        quipcli.call_llm_streaming(self._msgs(), "m")
        err = capsys.readouterr().err
        assert "no content" in err
        assert "length" in err

    def test_reasoning_content_field_shown(self, mock_http, capsys):
        lines = [
            b'data: {"choices":[{"delta":{"reasoning_content":"pondering"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n',
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        with patch("sys.stderr.isatty", return_value=True):
            quipcli.call_llm_streaming(self._msgs(), "m", show_reasoning=True)
        assert "pondering" in capsys.readouterr().err

    def test_echo_false_collects_without_printing(self, mock_http, capsys):
        lines = [
            b'data: {"choices":[{"delta":{"content":"ls -la"}}]}\n',
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        text, _ = quipcli.call_llm_streaming(self._msgs(), "m", echo=False)
        assert text == "ls -la"
        assert capsys.readouterr().out == ""

    def test_query_string_kept_in_request_target(self, mock_http):
        body = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
        http_cls = mock_http(MockHTTPResponse(200, body))
        with patch(
            "quipcli.constants._API_URL",
            "https://x.example/openai/chat/completions?api-version=2024-10-21",
        ):
            quipcli._make_request(self._msgs(), "m", False)
        _, target, _, _ = http_cls.return_value.request.call_args[0]
        assert target == "/openai/chat/completions?api-version=2024-10-21"

    def test_retry_after_header_honoured_and_capped(self):
        with (
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch("quipcli.http_client.time.sleep") as sleep,
        ):
            cls.side_effect = [
                _mock_conn(
                    MockHTTPResponse(429, b"slow", headers={"Retry-After": "7"})
                ),
                _mock_conn(
                    MockHTTPResponse(429, b"slow", headers={"Retry-After": "999"})
                ),
                _mock_conn(MockHTTPResponse(200, b"ok")),
            ]
            quipcli._make_request(self._msgs(), "m", False)
        assert [c.args[0] for c in sleep.call_args_list] == [7, 30]

    def test_ollama_fallback_strips_openrouter_server_tools(self, capsys):
        ok = MockHTTPResponse(200, b"ok")
        tags = MockHTTPResponse(
            200, json.dumps({"models": [{"name": "llama3.2"}]}).encode()
        )
        with (
            patch("http.client.HTTPConnection") as http_cls,
            patch("quipcli.constants._API_KEY", ""),
        ):
            post_conn = _mock_conn(ok)
            http_cls.side_effect = [_mock_conn(tags), post_conn]
            quipcli._make_request(
                self._msgs(),
                "m",
                False,
                extra={
                    "tools": [{"type": "function"}, {"type": "openrouter:web_search"}]
                },
            )
        _, _, body_arg, _ = post_conn.request.call_args[0]
        assert json.loads(body_arg)["tools"] == [{"type": "function"}]

    def test_fetch_models_over_plain_http_without_key(self, tmp_path):
        body = json.dumps({"data": [{"id": "llama3"}]}).encode()
        with (
            patch("http.client.HTTPConnection") as http_cls,
            patch("http.client.HTTPSConnection") as https_cls,
            patch(
                "quipcli.constants._API_URL",
                "http://localhost:8080/v1/chat/completions?x=1",
            ),
            patch("quipcli.constants._API_KEY", ""),
            patch("quipcli.constants._MODELS_CACHE", tmp_path / "models.json"),
            patch("quipcli.constants._CACHE_DIR", tmp_path),
        ):
            http_cls.return_value = _mock_conn(MockHTTPResponse(200, body))
            assert quipcli._fetch_models() == ["llama3"]
        https_cls.assert_not_called()
        method, target = http_cls.return_value.request.call_args[0]
        assert (method, target) == ("GET", "/v1/models")
        assert http_cls.return_value.request.call_args[1]["headers"] == {}

    def test_bg_refresh_backs_off_after_recent_attempt(self, monkeypatch, tmp_path):
        monkeypatch.delenv("_LLM_CMD_BG_UPDATE", raising=False)
        with (
            patch("subprocess.Popen") as popen,
            patch("quipcli.constants._MODELS_CACHE", tmp_path / "models.json"),
        ):
            _maybe_update_models_bg()  # no cache → spawns + stamps
            _maybe_update_models_bg()  # fetch evidently failed → backs off
        popen.assert_called_once()
        assert (tmp_path / "models.attempt").exists()

    def test_bg_refresh_retries_after_backoff_expires(self, monkeypatch, tmp_path):
        monkeypatch.delenv("_LLM_CMD_BG_UPDATE", raising=False)
        stamp = tmp_path / "models.attempt"
        stamp.touch()
        old = time.time() - 2 * 3600
        os.utime(stamp, (old, old))
        with (
            patch("subprocess.Popen") as popen,
            patch("quipcli.constants._MODELS_CACHE", tmp_path / "models.json"),
        ):
            _maybe_update_models_bg()
        popen.assert_called_once()

    def test_db_connection_is_closed(self, tmp_path):
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "history.db"),
            patch("quipcli.constants._DATA_DIR", tmp_path),
        ):
            with quipcli.db._db_conn() as conn:
                conn.execute("SELECT 1")
            with pytest.raises(sqlite3.ProgrammingError):
                conn.execute("SELECT 1")

    def test_db_schema_recreated_if_file_removed(self, tmp_path):
        db = tmp_path / "history.db"
        with (
            patch("quipcli.constants._HISTORY_DB", db),
            patch("quipcli.constants._DATA_DIR", tmp_path),
        ):
            quipcli._record_message("s", "user", "a", None, None, "chat")
            db.unlink()
            quipcli._record_message("s", "user", "b", None, None, "chat")
            msgs = quipcli._get_session_messages("s")
        assert msgs == [{"role": "user", "content": "b"}]

    def test_session_messages_keep_insert_order_on_equal_ts(self, tmp_path):
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "history.db"),
            patch("quipcli.constants._DATA_DIR", tmp_path),
            patch("quipcli.db.time.time", return_value=1000.0),
        ):
            for i in range(5):
                quipcli._record_message("s", "user", str(i), None, None, "chat")
            msgs = quipcli._get_session_messages("s")
        assert [m["content"] for m in msgs] == ["0", "1", "2", "3", "4"]

    def test_model_info_shows_reasoning_efforts(self, tmp_path, capsys):
        cache = tmp_path / "models.json"
        cache.write_text(
            json.dumps(
                {
                    "data": [
                        {
                            "id": "z/glm",
                            "reasoning": {
                                "supported_efforts": ["max", "high", "low"],
                                "default_effort": "max",
                                "mandatory": True,
                            },
                        }
                    ]
                }
            )
        )
        with patch("quipcli.constants._MODELS_CACHE", cache):
            quipcli.tui._print_model_info("  z/glm")
        out = capsys.readouterr().out
        assert "reasoning_efforts: max, high, low (default max; mandatory)" in out

    def test_tui_previews_call_current_interpreter(self):
        assert quipcli.tui._SELF.startswith(sys.executable)
        assert quipcli.tui._SELF.endswith(" -m quipcli")


class TestMainInterrupt:
    def test_ctrl_c_exits_130_without_traceback(self, capsys):
        from quipcli import entry

        with (
            patch.object(entry, "_main", side_effect=KeyboardInterrupt),
            pytest.raises(SystemExit) as exc,
        ):
            entry.main()
        assert exc.value.code == 130
        assert "Interrupted." in capsys.readouterr().err


class TestMainStatus:
    def test_api_key_masked(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(quipcli.constants, "_CONFIG_DIR", tmp_path)
        monkeypatch.setattr(quipcli.constants, "_CONFIG_FILE", tmp_path / "config.json")
        monkeypatch.setattr(
            quipcli.constants, "_MODELS_CACHE", tmp_path / "models.json"
        )
        monkeypatch.setattr(quipcli.constants, "_HISTORY_DB", tmp_path / "history.db")
        monkeypatch.setattr(
            quipcli.constants, "_API_KEY", "sk-or-v1-abcdef1234567890abcd"
        )
        from quipcli.entry import _do_status

        _do_status()
        out = capsys.readouterr().out
        assert "sk-or-v1-abcdef1234567890abcd" not in out
        assert "sk-or-v1…abcd" in out


# ── confirm_and_run ───────────────────────────────────────────────────────────


class TestConfirmAndRun:
    def test_y_runs_command(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            with (
                patch("builtins.input", return_value="y"),
                pytest.raises(SystemExit) as exc,
            ):
                quipcli.confirm_and_run("ls -la", "list files")
        assert exc.value.code == 0
        run.assert_called_once_with("ls -la", shell=True, check=False)

    def test_empty_enter_runs_command(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            with (
                patch("builtins.input", return_value=""),
                pytest.raises(SystemExit) as exc,
            ):
                quipcli.confirm_and_run("ls", "list")
        assert exc.value.code == 0
        run.assert_called_once()

    def test_n_aborts(self):
        with (
            patch("builtins.input", return_value="n"),
            pytest.raises(SystemExit) as exc,
        ):
            quipcli.confirm_and_run("ls", "list")
        assert exc.value.code == 0

    def test_ctrl_c_aborts(self):
        with (
            patch("builtins.input", side_effect=KeyboardInterrupt),
            pytest.raises(SystemExit) as exc,
        ):
            quipcli.confirm_and_run("ls", "list")
        assert exc.value.code == 0

    def test_fences_stripped_before_run(self):
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            with patch("builtins.input", return_value="y"), pytest.raises(SystemExit):
                quipcli.confirm_and_run("```bash\nls -la\n```", "list")
        run.assert_called_once_with("ls -la", shell=True, check=False)

    def test_e_opens_editor_then_reruns(self):
        edited_cmd = "ls -lah"
        responses = iter(["e", "y"])
        with patch("subprocess.run") as run:
            run.return_value.returncode = 0
            with (
                patch("builtins.input", side_effect=responses),
                patch("quipcli.execute._edit_in_editor", return_value=edited_cmd),
                pytest.raises(SystemExit),
            ):
                quipcli.confirm_and_run("ls -la", "list files")
        run.assert_called_once_with(edited_cmd, shell=True, check=False)


# ── _edit_in_editor ───────────────────────────────────────────────────────────


class TestEditInEditor:
    def test_strips_comment_lines(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EDITOR", "true")
        with patch("tempfile.NamedTemporaryFile") as mock_ntf:
            tmpfile = tmp_path / "cmd.sh"
            tmpfile.write_text("# Prompt: test\n# ────\n\nls -la\n")
            mock_ntf.return_value.__enter__ = lambda s: s
            mock_ntf.return_value.__exit__ = lambda *a: False
            mock_ntf.return_value.name = str(tmpfile)
            with patch("quipcli.execute.subprocess.run") as run, patch("os.unlink"):
                result = quipcli._edit_in_editor("ls -la", "test")
        run.assert_called_once_with(["true", str(tmpfile)], check=False)
        assert "#" not in result
        assert "ls -la" in result

    def test_editor_with_flags_split_correctly(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EDITOR", "code --wait")
        with patch("tempfile.NamedTemporaryFile") as mock_ntf:
            tmpfile = tmp_path / "cmd.sh"
            tmpfile.write_text("ls\n")
            mock_ntf.return_value.__enter__ = lambda s: s
            mock_ntf.return_value.__exit__ = lambda *a: False
            mock_ntf.return_value.name = str(tmpfile)
            with patch("quipcli.execute.subprocess.run") as run, patch("os.unlink"):
                quipcli._edit_in_editor("ls", "test")
        run.assert_called_once_with(["code", "--wait", str(tmpfile)], check=False)


class TestEditTextValue:
    def test_returns_edited_value_stripped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EDITOR", "true")
        with patch("tempfile.NamedTemporaryFile") as mock_ntf:
            tmpfile = tmp_path / "value.txt"
            tmpfile.write_text("  edited system prompt  \n")
            mock_ntf.return_value.__enter__ = lambda s: s
            mock_ntf.return_value.__exit__ = lambda *a: False
            mock_ntf.return_value.name = str(tmpfile)
            with patch("quipcli.execute.subprocess.run") as run, patch("os.unlink"):
                result = quipcli._edit_text_value("original value")
        run.assert_called_once_with(["true", str(tmpfile)], check=False)
        assert result == "edited system prompt"

    def test_no_comment_stripping_unlike_edit_in_editor(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EDITOR", "true")
        with patch("tempfile.NamedTemporaryFile") as mock_ntf:
            tmpfile = tmp_path / "value.txt"
            tmpfile.write_text("# not a comment header, just text\n")
            mock_ntf.return_value.__enter__ = lambda s: s
            mock_ntf.return_value.__exit__ = lambda *a: False
            mock_ntf.return_value.name = str(tmpfile)
            with patch("quipcli.execute.subprocess.run"), patch("os.unlink"):
                result = quipcli._edit_text_value("original value")
        assert result == "# not a comment header, just text"


# ── Config ────────────────────────────────────────────────────────────────────


class TestConfig:
    def test_load_missing_returns_empty(self, tmp_path):
        with patch("quipcli.constants._CONFIG_FILE", tmp_path / "config.json"):
            assert quipcli._load_config() == {}

    def test_round_trip(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
        ):
            quipcli._save_config({"default_model": "openai/gpt-4o"})
            result = quipcli._load_config()
        assert result == {"default_model": "openai/gpt-4o"}

    def test_load_corrupted_returns_empty(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("not json{{")
        with patch("quipcli.constants._CONFIG_FILE", cfg_file):
            assert quipcli._load_config() == {}

    def test_load_corrupted_warns_once(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr("quipcli.config._warned_bad_config", False)
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("not json{{")
        with patch("quipcli.constants._CONFIG_FILE", cfg_file):
            quipcli._load_config()
            quipcli._load_config()
            quipcli._load_config()
        err = capsys.readouterr().err
        assert str(cfg_file) in err
        assert "invalid JSON" in err
        assert err.count("invalid JSON") == 1

    def test_resolve_env_takes_priority(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_CMD_MODEL", "env/model")
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"default_model": "config/model"}))
        with patch("quipcli.constants._CONFIG_FILE", cfg_file):
            assert quipcli._resolve_default_model() == "env/model"

    def test_resolve_config_over_hardcoded(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LLM_CMD_MODEL", raising=False)
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"default_model": "config/model"}))
        with patch("quipcli.constants._CONFIG_FILE", cfg_file):
            assert quipcli._resolve_default_model() == "config/model"

    def test_resolve_hardcoded_fallback(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LLM_CMD_MODEL", raising=False)
        with patch("quipcli.constants._CONFIG_FILE", tmp_path / "missing.json"):
            assert quipcli._resolve_default_model() == "openai/gpt-4o-mini"

    def test_ensure_config_creates_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("LLM_CMD_MODEL", raising=False)
        cfg_file = tmp_path / "config.json"
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
        ):
            cfg = quipcli._ensure_config()
        assert cfg_file.exists()
        assert "default_model" not in cfg
        assert json.loads(cfg_file.read_text()) == {}

    def test_ensure_config_does_not_persist_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_CMD_MODEL", "temp/one-off-model")
        cfg_file = tmp_path / "config.json"
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
        ):
            quipcli._ensure_config()
        assert "default_model" not in json.loads(cfg_file.read_text())

    def test_ensure_config_does_not_mask_env_override(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LLM_CMD_MODEL", "temp/one-off-model")
        cfg_file = tmp_path / "config.json"
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
        ):
            quipcli._ensure_config()
            assert quipcli._resolve_default_model() == "temp/one-off-model"

    def test_ensure_config_leaves_existing_file_untouched(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"default_model": "custom/model"}))
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
        ):
            cfg = quipcli._ensure_config()
        assert cfg == {"default_model": "custom/model"}


# ── _seed_defaults ────────────────────────────────────────────────────────────


class TestSeedDefaults:
    def test_writes_missing_keys(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"default_model": "x"}))
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
        ):
            cfg = quipcli._seed_defaults({"chat_system_prompt": "be nice"})
        assert cfg["chat_system_prompt"] == "be nice"
        assert json.loads(cfg_file.read_text())["chat_system_prompt"] == "be nice"

    def test_does_not_overwrite_existing_key(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"chat_system_prompt": "custom"}))
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
        ):
            quipcli._seed_defaults({"chat_system_prompt": "default"})
        assert json.loads(cfg_file.read_text())["chat_system_prompt"] == "custom"

    def test_noop_write_when_nothing_missing(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"a": "1"}))
        mtime_before = cfg_file.stat().st_mtime_ns
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
        ):
            quipcli._seed_defaults({"a": "ignored"})
        assert cfg_file.stat().st_mtime_ns == mtime_before


# ── Machine context ───────────────────────────────────────────────────────────


class TestMachineContext:
    def test_includes_os_and_shell(self, monkeypatch):
        monkeypatch.setenv("SHELL", "/usr/bin/fish")
        ctx = quipcli._machine_context()
        assert "shell=fish" in ctx
        assert "OS=" in ctx
        assert "arch=" in ctx

    def test_reads_distro_from_os_release(self, tmp_path, monkeypatch):
        os_release = tmp_path / "os-release"
        os_release.write_text('NAME="Arch Linux"\nPRETTY_NAME="Arch Linux"\n')
        with patch("quipcli.context._OS_RELEASE", os_release):
            assert quipcli.context._linux_distro() == "Arch Linux"

    def test_no_distro_file_returns_none(self, tmp_path):
        with patch("quipcli.context._OS_RELEASE", tmp_path / "missing"):
            assert quipcli.context._linux_distro() is None


# ── History / SQLite ──────────────────────────────────────────────────────────


class TestHistory:
    def test_record_and_summary(self, tmp_path):
        stats = _UsageStats(
            model="openai/gpt-4o-mini",
            prompt_tokens=10,
            completion_tokens=20,
            cost_usd=0.0002,
        )
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "history.db"),
            patch("quipcli.constants._DATA_DIR", tmp_path),
        ):
            quipcli._record_usage(stats, "chat")
            s = quipcli._cost_summary(1)
        assert s["requests"] == 1
        assert s["prompt_tokens"] == 10
        assert s["completion_tokens"] == 20
        assert s["cost_usd"] == pytest.approx(0.0002)

    def test_summary_empty_db(self, tmp_path):
        with patch("quipcli.constants._HISTORY_DB", tmp_path / "missing.db"):
            assert quipcli._cost_summary(7) == {}

    def test_record_never_crashes(self, tmp_path):
        stats = _UsageStats(model="m", prompt_tokens=1, completion_tokens=1)
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "history.db"),
            patch("quipcli.constants._DATA_DIR", tmp_path),
            patch("sqlite3.connect", side_effect=sqlite3.OperationalError("db error")),
        ):
            quipcli._record_usage(stats, "chat")  # must not raise

    def test_summary_degrades_on_db_error(self, tmp_path):
        db = tmp_path / "history.db"
        db.write_text("")  # must exist so _cost_summary doesn't fast-return first
        with (
            patch("quipcli.constants._HISTORY_DB", db),
            patch("sqlite3.connect", side_effect=sqlite3.OperationalError("db error")),
        ):
            assert quipcli._cost_summary(7) == {}


# ── Sessions ──────────────────────────────────────────────────────────────────


class TestSessions:
    def test_record_and_retrieve_messages(self, tmp_path):
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "history.db"),
            patch("quipcli.constants._DATA_DIR", tmp_path),
        ):
            quipcli._record_message("sess1", "user", "hello", None, None, "chat")
            quipcli._record_message("sess1", "assistant", "world", "gpt", None, "chat")
            msgs = quipcli._get_session_messages("sess1")
        assert len(msgs) == 2
        assert msgs[0] == {"role": "user", "content": "hello"}
        assert msgs[1] == {"role": "assistant", "content": "world"}

    def test_last_session_id(self, tmp_path):
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "history.db"),
            patch("quipcli.constants._DATA_DIR", tmp_path),
        ):
            quipcli._record_message("first", "user", "a", None, None, "chat")
            quipcli._record_message("second", "user", "b", None, None, "chat")
            last = quipcli._last_session_id()
        assert last == "second"

    def test_last_session_none_when_empty(self, tmp_path):
        with patch("quipcli.constants._HISTORY_DB", tmp_path / "missing.db"):
            assert quipcli._last_session_id() is None

    def test_record_message_never_crashes_on_db_error(self, tmp_path):
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "history.db"),
            patch("quipcli.constants._DATA_DIR", tmp_path),
            patch("sqlite3.connect", side_effect=sqlite3.OperationalError("db error")),
        ):
            quipcli._record_message(
                "sess1", "user", "hi", None, None, "chat"
            )  # must not raise

    def test_get_session_messages_degrades_on_db_error(self, tmp_path):
        db = tmp_path / "history.db"
        db.write_text("")
        with (
            patch("quipcli.constants._HISTORY_DB", db),
            patch("sqlite3.connect", side_effect=sqlite3.OperationalError("db error")),
        ):
            assert quipcli._get_session_messages("sess1") == []

    def test_last_session_id_degrades_on_db_error(self, tmp_path):
        db = tmp_path / "history.db"
        db.write_text("")
        with (
            patch("quipcli.constants._HISTORY_DB", db),
            patch("sqlite3.connect", side_effect=sqlite3.OperationalError("db error")),
        ):
            assert quipcli._last_session_id() is None

    def test_resolve_session_none(self):
        sid, msgs = quipcli._resolve_session(None, False)
        assert sid is None
        assert msgs == []

    def test_resolve_session_named_new(self, tmp_path):
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "history.db"),
            patch("quipcli.constants._DATA_DIR", tmp_path),
        ):
            sid, msgs = quipcli._resolve_session("myconv", False)
        assert sid == "myconv"
        assert msgs == []

    def test_resolve_session_named_existing(self, tmp_path):
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "history.db"),
            patch("quipcli.constants._DATA_DIR", tmp_path),
        ):
            quipcli._record_message("myconv", "user", "hi", None, None, "chat")
            quipcli._record_message("myconv", "assistant", "hey", "gpt", None, "chat")
            sid, msgs = quipcli._resolve_session("myconv", False)
        assert sid == "myconv"
        assert len(msgs) == 2

    def test_resolve_session_auto_generates_name(self, tmp_path, capsys):
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "history.db"),
            patch("quipcli.constants._DATA_DIR", tmp_path),
        ):
            sid, msgs = quipcli._resolve_session("auto", False)
        assert sid is not None
        assert sid.startswith("auto-")
        assert msgs == []
        assert "Session:" in capsys.readouterr().err

    def test_resolve_follow_up(self, tmp_path, capsys):
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "history.db"),
            patch("quipcli.constants._DATA_DIR", tmp_path),
        ):
            quipcli._record_message("prev", "user", "q", None, None, "chat")
            quipcli._record_message("prev", "assistant", "a", "gpt", None, "chat")
            sid, msgs = quipcli._resolve_session(None, follow_up=True)
        assert sid == "prev"
        assert len(msgs) == 2

    def test_resolve_follow_up_no_history_exits(self, tmp_path):
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "missing.db"),
            pytest.raises(SystemExit),
        ):
            quipcli._resolve_session(None, follow_up=True)

    def test_session_and_followup_mutually_exclusive(self):
        with pytest.raises(SystemExit):
            quipcli._resolve_session("myconv", follow_up=True)

    def test_named_existing_session_announced_with_count(self, tmp_path, capsys):
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "history.db"),
            patch("quipcli.constants._DATA_DIR", tmp_path),
        ):
            quipcli._record_message("myconv", "user", "hi", None, None, "chat")
            quipcli._record_message("myconv", "assistant", "hey", "gpt", None, "chat")
            quipcli._resolve_session("myconv", False)
        assert "Session: myconv (2 messages)" in capsys.readouterr().err

    def test_quiet_suppresses_session_announcement(self, tmp_path, capsys):
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "history.db"),
            patch("quipcli.constants._DATA_DIR", tmp_path),
        ):
            quipcli._resolve_session("auto", False, quiet=True)
        assert capsys.readouterr().err == ""

    def test_multimodal_content_round_trip(self, tmp_path):
        multimodal = [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
        ]
        with (
            patch("quipcli.constants._HISTORY_DB", tmp_path / "history.db"),
            patch("quipcli.constants._DATA_DIR", tmp_path),
        ):
            quipcli._record_message("mm", "user", multimodal, None, None, "chat")
            msgs = quipcli._get_session_messages("mm")
        assert msgs[0]["content"] == multimodal


# ── Multimodal ────────────────────────────────────────────────────────────────


class TestIsImageUrl:
    def test_http_jpg(self):
        assert _is_image_url("https://example.com/photo.jpg")

    def test_https_png(self):
        assert _is_image_url("https://example.com/img.png")

    def test_not_an_image_url(self):
        assert not _is_image_url("https://example.com/doc.pdf")

    def test_not_a_url(self):
        assert not _is_image_url("photo.jpg")

    def test_http_not_https(self):
        assert _is_image_url("http://example.com/a.webp")


class TestBuildUserContent:
    def test_plain_text_words(self):
        content, mods = _build_user_content(["what", "is", "this"])
        assert content == "what is this"
        assert mods == set()

    def test_image_url_in_words(self):
        content, mods = _build_user_content(
            ["describe", "https://example.com/photo.jpg"]
        )
        assert isinstance(content, list)
        assert mods == {"image"}
        text_part = next(p for p in content if p.get("type") == "text")
        assert text_part["text"] == "describe"
        img_part = next(p for p in content if p.get("type") == "image_url")
        assert img_part["image_url"]["url"] == "https://example.com/photo.jpg"

    def test_local_image_file(self, tmp_path):
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG\r\n")  # minimal PNG header
        content, mods = _build_user_content(["describe", str(img)])
        assert isinstance(content, list)
        assert "image" in mods
        img_part = next(p for p in content if p.get("type") == "image_url")
        assert img_part["image_url"]["url"].startswith("data:image/png;base64,")

    def test_explicit_file_via_extra(self, tmp_path):
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff")  # JPEG magic bytes
        content, mods = _build_user_content(["what is this"], [str(img)])
        assert isinstance(content, list)
        assert "image" in mods

    def test_nonexistent_explicit_file_warned(self, tmp_path, capsys):
        _build_user_content(["hi"], ["/nonexistent/file.jpg"])
        assert "not found" in capsys.readouterr().err

    def test_non_media_file_ignored(self, tmp_path):
        txt = tmp_path / "notes.txt"
        txt.write_text("hello")
        content, mods = _build_user_content(["read", str(txt)])
        # .txt is not in _MEDIA_EXTENSIONS, treated as text token
        assert isinstance(content, str)
        assert mods == set()

    def test_explicit_text_file_is_inlined(self, tmp_path):
        log = tmp_path / "app.log"
        log.write_text("ERROR boom\n")
        content, mods = _build_user_content(["why", "this", "error"], [str(log)])
        assert isinstance(content, str)
        assert content.startswith("why this error\n\napp.log:\n```\nERROR boom\n```")
        assert mods == set()

    def test_explicit_binary_non_media_file_skipped(self, tmp_path, capsys):
        blob = tmp_path / "x.bin"
        blob.write_bytes(b"\xff\xfe\x00\x81")
        content, _ = _build_user_content(["hi"], [str(blob)])
        assert content == "hi"
        assert "skipped" in capsys.readouterr().err

    def test_text_only_returns_str(self):
        content, _mods = _build_user_content(["hello", "world"])
        assert isinstance(content, str)
        assert content == "hello world"

    def test_only_image_no_text(self, tmp_path):
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG\r\n")
        content, _mods = _build_user_content([str(img)])
        assert isinstance(content, list)
        # No text part when only a file
        text_parts = [p for p in content if p.get("type") == "text"]
        assert text_parts == []


class TestEncodeFileContent:
    def test_image_format(self, tmp_path):
        f = tmp_path / "img.png"
        f.write_bytes(b"PNG")
        part = quipcli._encode_file_content(f)
        assert part["type"] == "image_url"
        assert part["image_url"]["url"].startswith("data:image/png;base64,")

    def test_pdf_format(self, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"%PDF")
        part = quipcli._encode_file_content(f)
        assert part["type"] == "file"
        assert part["file"]["filename"] == "doc.pdf"
        # OpenRouter expects file_data as a data URL, not a bare "data" field
        assert part["file"]["file_data"].startswith("data:application/pdf;base64,")
        assert "data" not in part["file"]

    @pytest.mark.parametrize(
        "name,fmt", [("a.flac", "flac"), ("a.m4a", "m4a"), ("a.aac", "aac")]
    )
    def test_extra_audio_formats(self, tmp_path, name, fmt):
        f = tmp_path / name
        f.write_bytes(b"\x00")
        part = quipcli._encode_file_content(f)
        assert part["input_audio"]["format"] == fmt

    def test_mov_video(self, tmp_path):
        f = tmp_path / "clip.mov"
        f.write_bytes(b"\x00")
        part = quipcli._encode_file_content(f)
        assert part["video_url"]["url"].startswith("data:video/mov;base64,")

    def test_audio_format(self, tmp_path):
        f = tmp_path / "sound.mp3"
        f.write_bytes(b"ID3")
        part = quipcli._encode_file_content(f)
        assert part["type"] == "input_audio"
        assert part["input_audio"]["format"] == "mp3"

    def test_video_format(self, tmp_path):
        f = tmp_path / "clip.mp4"
        f.write_bytes(b"\x00\x00\x00\x18")
        part = quipcli._encode_file_content(f)
        assert part["type"] == "video_url"
        assert part["video_url"]["url"].startswith("data:video/mp4;base64,")


class TestModalitySupport:
    def _make_cache(self, tmp_path, models):
        cache = tmp_path / "models.json"
        cache.write_text(json.dumps({"data": models}))
        return cache

    def test_supported_modality_no_error(self, tmp_path):
        cache = self._make_cache(
            tmp_path,
            [
                {
                    "id": "m",
                    "architecture": {
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                    },
                }
            ],
        )
        with patch("quipcli.constants._MODELS_CACHE", cache):
            quipcli._check_modality_support("m", {"image"})  # should not raise

    def test_unsupported_modality_exits(self, tmp_path, capsys):
        cache = self._make_cache(
            tmp_path,
            [
                {
                    "id": "m",
                    "architecture": {
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                    },
                }
            ],
        )
        with (
            patch("quipcli.constants._MODELS_CACHE", cache),
            pytest.raises(SystemExit) as exc,
        ):
            quipcli._check_modality_support("m", {"image"})
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "image" in err

    def test_empty_needed_no_error(self, tmp_path):
        quipcli._check_modality_support("any", set())  # should not raise

    def test_list_by_modality(self, tmp_path):
        cache = self._make_cache(
            tmp_path,
            [
                {
                    "id": "img-model",
                    "architecture": {
                        "input_modalities": ["text", "image"],
                        "output_modalities": ["text"],
                    },
                },
                {
                    "id": "text-only",
                    "architecture": {
                        "input_modalities": ["text"],
                        "output_modalities": ["text"],
                    },
                },
                {
                    "id": "audio-gen",
                    "architecture": {
                        "input_modalities": ["text"],
                        "output_modalities": ["text", "audio"],
                    },
                },
            ],
        )
        with patch("quipcli.constants._MODELS_CACHE", cache):
            img_models = quipcli._list_models_by_modality(in_mods=["image"])
            audio_out = quipcli._list_models_by_modality(out_mods=["audio"])
        assert "img-model" in img_models
        assert "text-only" not in img_models
        assert "audio-gen" in audio_out
        assert "text-only" not in audio_out


# ── model/config/status/cost flags ─────────────────────────────────────────────


class TestModelConfigFlags:
    def _cache(self, tmp_path, ids):
        cache = tmp_path / "models.json"
        cache.write_text(json.dumps({"data": [{"id": i} for i in ids]}))
        return cache

    def test_set_with_exact_model(self, tmp_path, capsys):
        cfg_file = tmp_path / "config.json"
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
            patch("quipcli.constants._MODELS_CACHE", tmp_path / "models.json"),
        ):
            quipcli._do_model_set("openai/gpt-4o")
        assert json.loads(cfg_file.read_text())["default_model"] == "openai/gpt-4o"
        assert "openai/gpt-4o" in capsys.readouterr().out

    def test_set_with_unique_substring(self, tmp_path):
        cache = self._cache(tmp_path, ["anthropic/claude-3-5-haiku", "openai/gpt-4o"])
        cfg_file = tmp_path / "config.json"
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
            patch("quipcli.constants._MODELS_CACHE", cache),
        ):
            quipcli._do_model_set("haiku")
        assert (
            json.loads(cfg_file.read_text())["default_model"]
            == "anthropic/claude-3-5-haiku"
        )

    def test_set_no_value_uses_tui_picker_when_fzf_available(self, tmp_path):
        cache = self._cache(tmp_path, ["anthropic/claude-3-5-haiku", "openai/gpt-4o"])
        cfg_file = tmp_path / "config.json"
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
            patch("quipcli.constants._MODELS_CACHE", cache),
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("quipcli.tui.pick_model_interactive", return_value="openai/gpt-4o"),
        ):
            quipcli._do_model_set("")
        assert json.loads(cfg_file.read_text())["default_model"] == "openai/gpt-4o"

    def test_set_no_value_aborts_when_tui_picker_cancelled(self, tmp_path, capsys):
        cache = self._cache(tmp_path, ["openai/gpt-4o"])
        cfg_file = tmp_path / "config.json"
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._MODELS_CACHE", cache),
            patch("shutil.which", return_value="/usr/bin/fzf"),
            patch("quipcli.tui.pick_model_interactive", return_value=None),
        ):
            quipcli._do_model_set("")
        assert "Aborted" in capsys.readouterr().err

    def test_set_interactive_by_index_falls_back_without_fzf(self, tmp_path):
        cache = self._cache(tmp_path, ["anthropic/claude-3-5-haiku", "openai/gpt-4o"])
        cfg_file = tmp_path / "config.json"
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
            patch("quipcli.constants._MODELS_CACHE", cache),
            patch("shutil.which", return_value=None),
            patch("builtins.input", return_value="2"),
        ):
            quipcli._do_model_set("")
        assert json.loads(cfg_file.read_text())["default_model"] == "openai/gpt-4o"

    def test_set_interactive_by_name_falls_back_without_fzf(self, tmp_path):
        cache = self._cache(tmp_path, ["anthropic/claude-3-5-haiku", "openai/gpt-4o"])
        cfg_file = tmp_path / "config.json"
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
            patch("quipcli.constants._MODELS_CACHE", cache),
            patch("shutil.which", return_value=None),
            patch("builtins.input", return_value="haiku"),
        ):
            quipcli._do_model_set("")
        assert (
            json.loads(cfg_file.read_text())["default_model"]
            == "anthropic/claude-3-5-haiku"
        )

    def test_set_interactive_no_cache_errors_without_fzf(self, tmp_path):
        with (
            patch("quipcli.constants._CONFIG_FILE", tmp_path / "config.json"),
            patch("quipcli.constants._MODELS_CACHE", tmp_path / "models.json"),
            patch("shutil.which", return_value=None),
            pytest.raises(SystemExit) as exc,
        ):
            quipcli._do_model_set("")
        assert exc.value.code == 1

    def test_set_interactive_aborted_on_eof_without_fzf(self, tmp_path):
        cache = self._cache(tmp_path, ["openai/gpt-4o"])
        with (
            patch("quipcli.constants._CONFIG_FILE", tmp_path / "config.json"),
            patch("quipcli.constants._MODELS_CACHE", cache),
            patch("shutil.which", return_value=None),
            patch("builtins.input", side_effect=EOFError),
            pytest.raises(SystemExit) as exc,
        ):
            quipcli._do_model_set("")
        assert exc.value.code == 0

    def test_models_marks_current_default(self, tmp_path, capsys):
        cache = self._cache(tmp_path, ["anthropic/claude-3-5-haiku", "openai/gpt-4o"])
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"default_model": "openai/gpt-4o"}))
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._MODELS_CACHE", cache),
        ):
            quipcli._do_models(None, None)
        out = capsys.readouterr().out
        assert "* " in out
        assert "openai/gpt-4o" in out

    def test_models_no_cache_errors(self, tmp_path):
        with (
            patch("quipcli.constants._MODELS_CACHE", tmp_path / "missing.json"),
            pytest.raises(SystemExit) as exc,
        ):
            quipcli._do_models(None, None)
        assert exc.value.code == 1

    def test_model_get_prints_source(self, tmp_path, capsys):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"default_model": "openai/gpt-4o"}))
        with patch("quipcli.constants._CONFIG_FILE", cfg_file):
            quipcli._do_model_get()
        out = capsys.readouterr().out
        assert "openai/gpt-4o" in out
        assert "config" in out

    def test_model_get_prefers_env_label_when_both_set(
        self, tmp_path, monkeypatch, capsys
    ):
        # Regression: source label must match the value actually printed —
        # _resolve_default_model() checks env before config, so when both are
        # set the env value wins and must be labeled "(env)", not "(config)".
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"default_model": "openai/gpt-4o"}))
        monkeypatch.setenv("LLM_CMD_MODEL", "anthropic/claude-3-5-haiku")
        with patch("quipcli.constants._CONFIG_FILE", cfg_file):
            quipcli._do_model_get()
        out = capsys.readouterr().out
        assert "anthropic/claude-3-5-haiku" in out
        assert "openai/gpt-4o" not in out
        assert "(env)" in out

    def test_status_prefers_env_label_when_both_set(
        self, tmp_path, monkeypatch, capsys
    ):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"default_model": "openai/gpt-4o"}))
        monkeypatch.setenv("LLM_CMD_MODEL", "anthropic/claude-3-5-haiku")
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._MODELS_CACHE", tmp_path / "missing.json"),
            patch("quipcli.constants._HISTORY_DB", tmp_path / "missing.db"),
        ):
            quipcli._do_status()
        out = capsys.readouterr().out
        assert "anthropic/claude-3-5-haiku  (env)" in out

    def test_config_edit_opens_editor(self, tmp_path, monkeypatch):
        cfg_file = tmp_path / "config.json"
        monkeypatch.setenv("EDITOR", "myeditor")
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("subprocess.run") as run,
        ):
            quipcli._do_config_edit()
        run.assert_called_once_with(["myeditor", str(cfg_file)], check=False)

    def test_cost_invalid_period_exits(self, capsys):
        with pytest.raises(SystemExit) as exc:
            quipcli._do_cost("bogus")
        assert exc.value.code == 1

    def test_cost_no_history(self, tmp_path, capsys):
        with patch("quipcli.constants._HISTORY_DB", tmp_path / "missing.db"):
            quipcli._do_cost("7d")
        assert "No history" in capsys.readouterr().out


# ── main() argv guards ───────────────────────────────────────────────────────


class TestMainArgvGuards:
    """--model-set/--cost use nargs='?', so argparse greedily consumes a bare
    following word as their value even when it was meant to start a chat
    prompt. main() must refuse to proceed rather than silently corrupting
    config or running --cost with a bogus period (see input-paths audit H1)."""

    def _isolate_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr(quipcli.constants, "_CONFIG_DIR", tmp_path)
        monkeypatch.setattr(quipcli.constants, "_CONFIG_FILE", tmp_path / "config.json")
        monkeypatch.setattr(
            quipcli.constants, "_MODELS_CACHE", tmp_path / "models.json"
        )
        monkeypatch.setattr(quipcli.constants, "_HISTORY_DB", tmp_path / "history.db")

    def test_model_set_with_leftover_words_errors_without_touching_config(
        self, tmp_path, monkeypatch, capsys
    ):
        self._isolate_config(tmp_path, monkeypatch)
        monkeypatch.setattr(
            sys, "argv", ["qp", "--model-set", "list", "all", "my", "files"]
        )
        with patch("subprocess.Popen"), pytest.raises(SystemExit) as exc:
            quipcli.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--model-set" in err
        assert "'list'" in err
        cfg = json.loads((tmp_path / "config.json").read_text())
        assert "default_model" not in cfg

    def test_cost_with_leftover_words_errors(self, tmp_path, monkeypatch, capsys):
        self._isolate_config(tmp_path, monkeypatch)
        monkeypatch.setattr(sys, "argv", ["qp", "--cost", "what", "is", "going", "on"])
        with patch("subprocess.Popen"), pytest.raises(SystemExit) as exc:
            quipcli.main()
        assert exc.value.code == 1
        assert "--cost" in capsys.readouterr().err

    def test_model_set_alone_is_unaffected(self, tmp_path, monkeypatch):
        self._isolate_config(tmp_path, monkeypatch)
        cache = tmp_path / "models.json"
        cache.write_text(json.dumps({"data": [{"id": "openai/gpt-4o"}]}))
        monkeypatch.setattr(quipcli.constants, "_MODELS_CACHE", cache)
        monkeypatch.setattr(sys, "argv", ["qp", "--model-set", "openai/gpt-4o"])
        with patch("subprocess.Popen"):
            quipcli.main()
        cfg = json.loads((tmp_path / "config.json").read_text())
        assert cfg["default_model"] == "openai/gpt-4o"

    def test_cost_alone_is_unaffected(self, tmp_path, monkeypatch, capsys):
        self._isolate_config(tmp_path, monkeypatch)
        monkeypatch.setattr(sys, "argv", ["qp", "--cost", "30d"])
        with patch("subprocess.Popen"):
            quipcli.main()
        assert "No history" in capsys.readouterr().out

    @pytest.mark.parametrize(
        "argv", [["qp", "--in", "image", "hi"], ["qp", "--out", "audio", "hi"]]
    )
    def test_in_out_filters_require_models_flag(
        self, argv, tmp_path, monkeypatch, capsys
    ):
        self._isolate_config(tmp_path, monkeypatch)
        monkeypatch.setattr(sys, "argv", argv)
        with patch("subprocess.Popen"), pytest.raises(SystemExit) as exc:
            quipcli.main()
        assert exc.value.code == 1
        assert "--models" in capsys.readouterr().err

    def test_models_with_in_filter_is_unaffected(self, tmp_path, monkeypatch, capsys):
        self._isolate_config(tmp_path, monkeypatch)
        cache = tmp_path / "models.json"
        cache.write_text(
            json.dumps(
                {
                    "data": [
                        {
                            "id": "m",
                            "architecture": {
                                "input_modalities": ["text", "image"],
                                "output_modalities": ["text"],
                            },
                        }
                    ]
                }
            )
        )
        monkeypatch.setattr(quipcli.constants, "_MODELS_CACHE", cache)
        monkeypatch.setattr(sys, "argv", ["qp", "--models", "--in", "image"])
        with patch("subprocess.Popen"):
            quipcli.main()
        assert "m" in capsys.readouterr().out


class TestMainDispatch:
    """main()'s per-mode dispatch (chat/-e/-a/-c) end-to-end — the argv ->
    mode -> call_llm_*/run_agent_loop -> stats/history wiring had zero direct
    test coverage; only its individual helpers were unit-tested in isolation."""

    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(quipcli.constants, "_CONFIG_DIR", tmp_path)
        monkeypatch.setattr(quipcli.constants, "_CONFIG_FILE", tmp_path / "config.json")
        monkeypatch.setattr(
            quipcli.constants, "_MODELS_CACHE", tmp_path / "models.json"
        )
        monkeypatch.setattr(quipcli.constants, "_HISTORY_DB", tmp_path / "history.db")
        monkeypatch.setattr(quipcli.constants, "_DATA_DIR", tmp_path)

    def _sse(self, text, usage=None):
        chunk = json.dumps({"choices": [{"delta": {"content": text}}]}).encode()
        lines = [b"data: " + chunk + b"\n"]
        if usage is not None:
            lines.append(
                b"data: " + json.dumps({"choices": [], "usage": usage}).encode() + b"\n"
            )
        lines.append(b"data: [DONE]\n")
        return MockHTTPResponse(200, b"", lines)

    def _json(self, message, usage=None):
        body = {"choices": [{"message": message}]}
        if usage is not None:
            body["usage"] = usage
        return MockHTTPResponse(200, json.dumps(body).encode())

    def test_chat_default_mode_streams_and_records(self, tmp_path, monkeypatch, capsys):
        self._isolate(tmp_path, monkeypatch)
        monkeypatch.setattr(sys, "argv", ["qp", "hello"])
        resp = self._sse("hi there", usage={"prompt_tokens": 3, "completion_tokens": 2})
        with (
            patch("subprocess.Popen"),
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch("quipcli.entry._record_usage") as record_usage,
            patch("quipcli.entry._record_message") as record_message,
        ):
            cls.return_value = _mock_conn(resp)
            quipcli.main()
        assert "hi there" in capsys.readouterr().out
        record_usage.assert_called_once()
        assert record_usage.call_args[0][1] == "chat"
        record_message.assert_not_called()  # no -s/-f, so no session_id

    def test_execute_mode_streams_silently_and_confirms(
        self, tmp_path, monkeypatch, capsys
    ):
        self._isolate(tmp_path, monkeypatch)
        monkeypatch.setattr(sys, "argv", ["qp", "-e", "list files"])
        resp = self._sse("ls -la\n", usage={"prompt_tokens": 4, "completion_tokens": 3})
        with (
            patch("subprocess.Popen"),
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch("quipcli.entry._record_usage") as record_usage,
            patch("builtins.input", return_value="n"),
        ):
            cls.return_value = _mock_conn(resp)
            with pytest.raises(SystemExit) as exc:
                quipcli.main()
        assert exc.value.code == 0  # user declined to run the generated command
        record_usage.assert_called_once()
        assert record_usage.call_args[0][1] == "execute"
        captured = capsys.readouterr()
        assert captured.out == ""  # command shown only in the confirm prompt
        assert "ls -la" in captured.err

    def test_agent_mode_calls_run_agent_loop_and_records(
        self, tmp_path, monkeypatch, capsys
    ):
        self._isolate(tmp_path, monkeypatch)
        monkeypatch.setattr(sys, "argv", ["qp", "-a", "do it"])
        resp = self._json(
            {"role": "assistant", "content": "done"},
            usage={"prompt_tokens": 5, "completion_tokens": 1},
        )
        with (
            patch("subprocess.Popen"),
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch("quipcli.entry._record_usage") as record_usage,
        ):
            cls.return_value = _mock_conn(resp)
            quipcli.main()
        assert "done" in capsys.readouterr().out
        record_usage.assert_called_once()
        assert record_usage.call_args[0][1] == "agent"

    def test_code_mode_streams_and_records(self, tmp_path, monkeypatch, capsys):
        self._isolate(tmp_path, monkeypatch)
        monkeypatch.setattr(sys, "argv", ["qp", "-c", "write a sort"])
        resp = self._sse(
            "def sort(): ...", usage={"prompt_tokens": 6, "completion_tokens": 4}
        )
        with (
            patch("subprocess.Popen"),
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
            patch("quipcli.entry._record_usage") as record_usage,
        ):
            cls.return_value = _mock_conn(resp)
            quipcli.main()
        assert "def sort()" in capsys.readouterr().out
        record_usage.assert_called_once()
        assert record_usage.call_args[0][1] == "code"

    def test_model_substring_resolved_notice_printed(
        self, tmp_path, monkeypatch, capsys
    ):
        self._isolate(tmp_path, monkeypatch)
        cache = tmp_path / "models.json"
        cache.write_text(json.dumps({"data": [{"id": "anthropic/claude-3-5-haiku"}]}))
        monkeypatch.setattr(quipcli.constants, "_MODELS_CACHE", cache)
        monkeypatch.setattr(sys, "argv", ["qp", "-m", "haiku", "hi"])
        with (
            patch("subprocess.Popen"),
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
        ):
            cls.return_value = _mock_conn(self._sse("ok"))
            quipcli.main()
        assert "anthropic/claude-3-5-haiku" in capsys.readouterr().err

    def test_quiet_suppresses_model_resolved_notice(
        self, tmp_path, monkeypatch, capsys
    ):
        self._isolate(tmp_path, monkeypatch)
        cache = tmp_path / "models.json"
        cache.write_text(json.dumps({"data": [{"id": "anthropic/claude-3-5-haiku"}]}))
        monkeypatch.setattr(quipcli.constants, "_MODELS_CACHE", cache)
        monkeypatch.setattr(sys, "argv", ["qp", "-m", "haiku", "-q", "hi"])
        with (
            patch("subprocess.Popen"),
            patch("http.client.HTTPSConnection") as cls,
            patch("quipcli.constants._API_KEY", "key"),
        ):
            cls.return_value = _mock_conn(self._sse("ok"))
            quipcli.main()
        assert "Model:" not in capsys.readouterr().err


# ── tui ───────────────────────────────────────────────────────────────────────


class TestTuiHelpers:
    def test_model_id_from_line_with_marker(self):
        assert quipcli._model_id_from_line("* openai/gpt-4o") == "openai/gpt-4o"

    def test_model_id_from_line_without_marker(self):
        assert (
            quipcli._model_id_from_line("  anthropic/claude-3-5-haiku")
            == "anthropic/claude-3-5-haiku"
        )

    def test_key_from_line(self):
        assert (
            quipcli._key_from_line("default_model = openai/gpt-4o") == "default_model"
        )

    def test_config_lines_shows_not_set(self):
        lines = quipcli._config_lines({})
        for key in ("default_model", "system_prompt", "ollama_model"):
            line = next(l for l in lines if quipcli._key_from_line(l) == key)
            assert line.endswith("= (not set)")

    def test_config_lines_truncates_long_values(self):
        lines = quipcli._config_lines({"system_prompt": "x" * 100})
        prompt_line = next(l for l in lines if l.startswith("system_prompt"))
        assert prompt_line.endswith("...")
        assert len(prompt_line) < 100

    def test_model_lines_marks_current_default(self, tmp_path):
        cache = tmp_path / "models.json"
        cache.write_text(json.dumps({"data": [{"id": "a"}, {"id": "b"}]}))
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text(json.dumps({"default_model": "b"}))
        with (
            patch("quipcli.constants._MODELS_CACHE", cache),
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
        ):
            lines = quipcli._model_lines()
        assert lines[0] == "  a"
        assert quipcli._model_id_from_line(lines[1]) == "b"
        assert "\033[32m" in lines[1]


class TestTuiModelInfo:
    def _cache(self, tmp_path):
        cache = tmp_path / "models.json"
        cache.write_text(
            json.dumps(
                {
                    "data": [
                        {
                            "id": "openai/gpt-4o-mini",
                            "name": "GPT-4o mini",
                            "context_length": 128000,
                            "pricing": {
                                "prompt": "0.00000015",
                                "completion": "0.0000006",
                            },
                            "architecture": {
                                "input_modalities": ["text", "image"],
                                "output_modalities": ["text"],
                            },
                        }
                    ]
                }
            )
        )
        return cache

    def test_prints_formatted_fields(self, tmp_path, capsys):
        with patch("quipcli.constants._MODELS_CACHE", self._cache(tmp_path)):
            quipcli._print_model_info("* openai/gpt-4o-mini")
        out = capsys.readouterr().out
        assert "id: openai/gpt-4o-mini" in out
        assert "context_length: 128000" in out
        assert "price_per_1M_prompt: $0.15" in out
        assert "price_per_1M_completion: $0.60" in out
        assert "input_modalities: text, image" in out

    def test_unknown_model_shows_placeholder(self, tmp_path, capsys):
        with patch("quipcli.constants._MODELS_CACHE", self._cache(tmp_path)):
            quipcli._print_model_info("  unknown/model")
        out = capsys.readouterr().out
        assert "id: unknown/model" in out
        assert "no cached details" in out

    def test_prints_wrapped_description(self, tmp_path, capsys):
        cache = tmp_path / "models.json"
        cache.write_text(
            json.dumps(
                {
                    "data": [
                        {
                            "id": "openai/gpt-4o-mini",
                            "name": "GPT-4o mini",
                            "canonical_slug": "openai/gpt-4o-mini-2024-07-18",
                            "description": "word " * 40,
                            "pricing": {},
                            "architecture": {},
                        }
                    ]
                }
            )
        )
        with (
            patch("quipcli.constants._MODELS_CACHE", cache),
            patch("quipcli.constants._RANKINGS_CACHE", tmp_path / "rankings.json"),
        ):
            quipcli._print_model_info("  openai/gpt-4o-mini")
        out = capsys.readouterr().out
        assert "word word" in out
        assert all(len(line) <= 70 for line in out.splitlines())

    def test_prints_usage_rank_when_cached(self, tmp_path, capsys):
        cache = tmp_path / "models.json"
        cache.write_text(
            json.dumps(
                {
                    "data": [
                        {
                            "id": "openai/gpt-4o-mini",
                            "canonical_slug": "openai/gpt-4o-mini-2024-07-18",
                            "pricing": {},
                            "architecture": {},
                        }
                    ]
                }
            )
        )
        rankings = tmp_path / "rankings.json"
        rankings.write_text(
            json.dumps(
                {
                    "date": "2026-07-23",
                    "data": [
                        {
                            "rank": 3,
                            "model_permaslug": "openai/gpt-4o-mini-2024-07-18",
                            "total_tokens": 42_000_000,
                        },
                    ],
                }
            )
        )
        with (
            patch("quipcli.constants._MODELS_CACHE", cache),
            patch("quipcli.constants._RANKINGS_CACHE", rankings),
        ):
            quipcli._print_model_info("  openai/gpt-4o-mini")
        out = capsys.readouterr().out
        assert "usage_rank: #3 of top 50" in out
        assert "42,000,000 tokens/day" in out

    def test_no_usage_rank_line_without_cache(self, tmp_path, capsys):
        with (
            patch("quipcli.constants._MODELS_CACHE", self._cache(tmp_path)),
            patch(
                "quipcli.constants._RANKINGS_CACHE", tmp_path / "missing-rankings.json"
            ),
        ):
            quipcli._print_model_info("* openai/gpt-4o-mini")
        out = capsys.readouterr().out
        assert "usage_rank" not in out


class TestModelsView:
    def test_picker_mode_returns_id_without_saving(self, tmp_path):
        cache = tmp_path / "models.json"
        cache.write_text(json.dumps({"data": [{"id": "a"}]}))
        cfg_file = tmp_path / "config.json"
        with (
            patch("quipcli.constants._MODELS_CACHE", cache),
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.tui._run_fzf", return_value="  a"),
        ):
            result = quipcli._models_view(picker_mode=True)
        assert result == "a"
        assert not cfg_file.exists()

    def test_standalone_mode_saves_as_default(self, tmp_path, capsys):
        cache = tmp_path / "models.json"
        cache.write_text(json.dumps({"data": [{"id": "a"}]}))
        cfg_file = tmp_path / "config.json"
        with (
            patch("quipcli.constants._MODELS_CACHE", cache),
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
            patch("quipcli.tui._run_fzf", return_value="  a"),
        ):
            result = quipcli._models_view(picker_mode=False)
        assert result == "a"
        assert json.loads(cfg_file.read_text())["default_model"] == "a"
        assert "a" in capsys.readouterr().err

    def test_escape_returns_none(self, tmp_path):
        cache = tmp_path / "models.json"
        cache.write_text(json.dumps({"data": [{"id": "a"}]}))
        with (
            patch("quipcli.constants._MODELS_CACHE", cache),
            patch("quipcli.tui._run_fzf", return_value=None),
        ):
            assert quipcli._models_view(picker_mode=True) is None

    def test_empty_cache_skips_fzf(self, tmp_path):
        with (
            patch("quipcli.constants._MODELS_CACHE", tmp_path / "missing.json"),
            patch("quipcli.tui._run_fzf") as run_fzf,
        ):
            result = quipcli._models_view()
        assert result is None
        run_fzf.assert_not_called()

    def test_pick_model_interactive_delegates_to_picker_mode(self, tmp_path):
        cache = tmp_path / "models.json"
        cache.write_text(json.dumps({"data": [{"id": "a"}]}))
        cfg_file = tmp_path / "config.json"
        with (
            patch("quipcli.constants._MODELS_CACHE", cache),
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.tui._run_fzf", return_value="  a"),
        ):
            assert quipcli.pick_model_interactive() == "a"
        assert not cfg_file.exists()


class TestConfigView:
    def test_system_prompt_edit(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
            patch(
                "quipcli.tui._run_fzf", side_effect=["system_prompt = (not set)", None]
            ),
            patch("quipcli.tui._edit_text_value", return_value="prefer pacman"),
        ):
            quipcli._config_view()
        assert json.loads(cfg_file.read_text())["system_prompt"] == "prefer pacman"

    @pytest.mark.parametrize(
        "key",
        [
            "chat_system_prompt",
            "execute_system_prompt",
            "code_system_prompt",
            "agent_system_prompt",
        ],
    )
    def test_mode_prompt_keys_are_editable(self, tmp_path, key):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
            patch("quipcli.tui._run_fzf", side_effect=[f"{key} = (not set)", None]),
            patch("quipcli.tui._edit_text_value", return_value="be terse"),
        ):
            quipcli._config_view()
        assert json.loads(cfg_file.read_text())[key] == "be terse"

    def test_config_keys_include_all_mode_prompts_and_are_listed(self):
        assert quipcli._config_lines({})[0].startswith("default_model")
        keys = [quipcli._key_from_line(l) for l in quipcli._config_lines({})]
        assert keys == [
            "default_model",
            "reasoning_effort",
            "chat_system_prompt",
            "execute_system_prompt",
            "code_system_prompt",
            "agent_system_prompt",
            "system_prompt",
            "ollama_model",
        ]

    def test_default_model_drills_into_models_view(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
            patch(
                "quipcli.tui._run_fzf", side_effect=["default_model = (not set)", None]
            ),
            patch(
                "quipcli.tui._models_view", return_value="openai/gpt-4o"
            ) as models_view,
        ):
            quipcli._config_view()
        models_view.assert_called_once_with(picker_mode=True)
        assert json.loads(cfg_file.read_text())["default_model"] == "openai/gpt-4o"

    def test_ollama_model_picks_from_local_list(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
            patch("quipcli.tui._ollama_models", return_value=["llama3.2", "mistral"]),
            patch(
                "quipcli.tui._run_fzf",
                side_effect=["ollama_model = (not set)", "  mistral", None],
            ),
        ):
            quipcli._config_view()
        assert json.loads(cfg_file.read_text())["ollama_model"] == "mistral"

    def test_ollama_model_falls_back_to_text_edit_when_unreachable(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.constants._CONFIG_DIR", tmp_path),
            patch("quipcli.tui._ollama_models", return_value=None),
            patch("quipcli.tui._edit_text_value", return_value="qwen3:8b"),
            patch(
                "quipcli.tui._run_fzf", side_effect=["ollama_model = (not set)", None]
            ),
        ):
            quipcli._config_view()
        assert json.loads(cfg_file.read_text())["ollama_model"] == "qwen3:8b"

    def test_escape_exits_immediately(self, tmp_path):
        cfg_file = tmp_path / "config.json"
        cfg_file.write_text("{}")
        with (
            patch("quipcli.constants._CONFIG_FILE", cfg_file),
            patch("quipcli.tui._run_fzf", return_value=None) as run_fzf,
        ):
            quipcli._config_view()
        run_fzf.assert_called_once()


class TestRunTui:
    def test_missing_fzf_exits_with_error(self, capsys):
        with (
            patch("quipcli.tui._fzf_available", return_value=False),
            pytest.raises(SystemExit) as exc,
        ):
            quipcli.run_tui()
        assert exc.value.code == 1
        assert "requires fzf" in capsys.readouterr().err

    def test_escape_at_top_menu_returns(self):
        with (
            patch("quipcli.tui._fzf_available", return_value=True),
            patch("quipcli.tui._run_fzf", return_value=None) as run_fzf,
        ):
            quipcli.run_tui()
        run_fzf.assert_called_once()

    def test_selecting_models_then_escaping_returns_to_menu(self):
        with (
            patch("quipcli.tui._fzf_available", return_value=True),
            patch("quipcli.tui._run_fzf", side_effect=["Models", None]),
            patch("quipcli.tui._models_view") as models_view,
        ):
            quipcli.run_tui()
        models_view.assert_called_once_with(picker_mode=False)

    def test_selecting_config_then_escaping_returns_to_menu(self):
        with (
            patch("quipcli.tui._fzf_available", return_value=True),
            patch("quipcli.tui._run_fzf", side_effect=["Config", None]),
            patch("quipcli.tui._config_view") as config_view,
        ):
            quipcli.run_tui()
        config_view.assert_called_once()


class TestRunFzf:
    def test_builds_expected_argv(self):
        with patch("quipcli.tui.subprocess.run") as run:
            run.return_value.stdout = "picked\n"
            result = quipcli._run_fzf(
                ["a", "b"],
                header="h",
                preview_cmd="preview {}",
                extra_binds=["ctrl-r:reload(x)"],
                prompt="p> ",
            )
        assert result == "picked"
        argv = run.call_args[0][0]
        assert argv[0] == "fzf"
        assert "--header" in argv and "h" in argv
        assert "--prompt" in argv and "p> " in argv
        assert "--preview" in argv and "preview {}" in argv
        assert "--bind" in argv and "ctrl-r:reload(x)" in argv
        assert run.call_args[1]["input"] == "a\nb"

    def test_empty_stdout_returns_none(self):
        with patch("quipcli.tui.subprocess.run") as run:
            run.return_value.stdout = "\n"
            assert quipcli._run_fzf(["a"]) is None

    def test_missing_fzf_binary_returns_none(self):
        with patch("quipcli.tui.subprocess.run", side_effect=OSError("not found")):
            assert quipcli._run_fzf(["a"]) is None


class TestAtomicWrite:
    def test_writes_content(self, tmp_path):
        target = tmp_path / "out.json"
        quipcli.constants._atomic_write_text(target, '{"a": 1}')
        assert target.read_text() == '{"a": 1}'

    def test_overwrites_existing(self, tmp_path):
        target = tmp_path / "out.json"
        target.write_text("old")
        quipcli.constants._atomic_write_text(target, "new")
        assert target.read_text() == "new"

    def test_no_leftover_temp_file(self, tmp_path):
        target = tmp_path / "out.json"
        quipcli.constants._atomic_write_text(target, "x")
        assert [p.name for p in tmp_path.iterdir()] == ["out.json"]


# ── Real (served) model reporting ─────────────────────────────────────────────


class TestServedModel:
    def _msgs(self):
        return [{"role": "user", "content": "p"}]

    def test_served_model_helper(self):
        assert quipcli.db._served_model("openrouter/auto", "anthropic/x") == (
            "anthropic/x",
            "openrouter/auto",
        )
        assert quipcli.db._served_model(
            "openai/gpt-4o", "openai/gpt-4o-2024-08-06"
        ) == (
            "openai/gpt-4o",
            None,
        )
        assert quipcli.db._served_model("m", None) == ("m", None)

    def test_streaming_reports_routed_model(self, mock_http, capsys):
        lines = [
            b'data: {"model":"anthropic/claude-x","choices":[{"delta":{"content":"hi"}}]}\n',
            (
                b'data: {"model":"anthropic/claude-x","choices":[],'
                b'"usage":{"prompt_tokens":1,"completion_tokens":1}}\n'
            ),
            b"data: [DONE]\n",
        ]
        mock_http(MockHTTPResponse(200, b"", lines))
        _, stats = quipcli.call_llm_streaming(
            self._msgs(), "openrouter/auto", collect_usage=True
        )
        assert stats is not None
        assert stats.model == "anthropic/claude-x"
        assert stats.routed_from == "openrouter/auto"
        quipcli._print_stats(stats)
        assert "openrouter/auto → anthropic/claude-x" in capsys.readouterr().err

    def test_agent_reports_routed_model(self, mock_http):
        body = json.dumps(
            {
                "model": "openai/gpt-5",
                "choices": [{"message": {"role": "assistant", "content": "done"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }
        ).encode()
        mock_http(MockHTTPResponse(200, body))
        _, stats = quipcli.run_agent_loop(self._msgs(), "openrouter/auto")
        assert stats is not None
        assert (stats.model, stats.routed_from) == ("openai/gpt-5", "openrouter/auto")
