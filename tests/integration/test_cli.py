"""CLI contract tests: commands, exit codes, JSON output."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pikepdf
import pytest
from typer.testing import CliRunner

import localdocforge.cli.main as cli_main
from localdocforge.cli.main import (
    EXIT_COLLISION,
    EXIT_FAILED,
    EXIT_USAGE,
    app,
)

runner = CliRunner()


def combined_output(result) -> str:
    try:
        return result.output + (result.stderr or "")
    except ValueError:
        return result.output


def assert_secret_absent(result, secret: str) -> None:
    encoded = secret.encode("utf-8")
    assert encoded not in result.stdout_bytes
    assert encoded not in result.stderr_bytes
    assert encoded not in result.output_bytes


class TestDoctor:
    def test_doctor_runs(self):
        result = runner.invoke(app, ["doctor"])
        assert result.exit_code == 0
        assert "pikepdf" in result.output
        assert "Privacy" in result.output

    def test_doctor_json_is_machine_readable_and_honest(self):
        result = runner.invoke(app, ["--json", "doctor"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["strict_offline"] is False
        assert payload["outbound_network_client"] is False
        engines = {e["name"]: e for e in payload["engines"]}
        assert engines["pikepdf"]["available"] is True
        assert engines["pillow"]["available"] is True
        capabilities = {c["id"]: c for c in payload["capabilities"]}
        assert capabilities["merge"]["available"] is True
        assert capabilities["ocr"]["available"] is False
        assert capabilities["office-to-pdf"]["available"] is False

    def test_doctor_reports_strict_offline_state(self):
        result = runner.invoke(app, ["--strict-offline", "doctor"])
        assert result.exit_code == 0
        assert "Strict-offline mode is enabled" in result.output


class TestVersionAndHelp:
    def test_version(self):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "LocalDocForge" in result.output

    def test_help_lists_commands(self):
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for command in ("merge", "split", "rotate", "doctor", "inspect"):
            assert command in result.output
        assert "--password-stdin" in result.output
        assert "LDF_PASSWORD" in result.output

    def test_password_stdin_does_not_consume_subcommand_help(self, monkeypatch):
        def unexpected_read():
            raise AssertionError("subcommand help must not consume password stdin")

        monkeypatch.setattr(cli_main, "_read_password_stdin", unexpected_read)
        result = runner.invoke(
            app,
            ["--password-stdin", "merge", "--help"],
            input=b"",
            env={"LDF_PASSWORD": None},
        )
        assert result.exit_code == 0, combined_output(result)
        assert "Usage:" in result.output
        assert "merge" in result.output
        assert cli_main._state["password_stdin_requested"] is False

    def test_strict_offline_refuses_explicit_nonlocal_web_bind(self):
        result = runner.invoke(
            app,
            ["--strict-offline", "web", "--host", "0.0.0.0", "--allow-nonlocal"],
        )
        assert result.exit_code == EXIT_USAGE
        assert "strict-offline" in combined_output(result)


class TestMergeCommand:
    def test_merge_files(self, fixtures_dir, out_dir):
        out = out_dir / "cli-merged.pdf"
        result = runner.invoke(
            app,
            [
                "merge",
                str(fixtures_dir / "simple-3page.pdf"),
                str(fixtures_dir / "second-2page.pdf"),
                "-o",
                str(out),
            ],
        )
        assert result.exit_code == 0, result.output
        with pikepdf.open(out) as pdf:
            assert len(pdf.pages) == 5
        assert "success" in result.output

    def test_merge_with_inline_ranges(self, fixtures_dir, out_dir):
        out = out_dir / "cli-ranged.pdf"
        result = runner.invoke(
            app,
            [
                "merge",
                f"{fixtures_dir / 'simple-3page.pdf'}::1,3",
                f"{fixtures_dir / 'second-2page.pdf'}::2",
                "-o",
                str(out),
            ],
        )
        assert result.exit_code == 0, result.output
        with pikepdf.open(out) as pdf:
            assert len(pdf.pages) == 3

    def test_merge_pages_count_mismatch_is_usage_error(self, fixtures_dir, out_dir):
        result = runner.invoke(
            app,
            [
                "merge",
                str(fixtures_dir / "simple-3page.pdf"),
                str(fixtures_dir / "second-2page.pdf"),
                "--pages",
                "1-2",
                "-o",
                str(out_dir / "never.pdf"),
            ],
        )
        assert result.exit_code == EXIT_USAGE

    def test_merge_collision_exit_code(self, fixtures_dir, out_dir):
        out = out_dir / "cli-collide.pdf"
        args = [
            "merge",
            str(fixtures_dir / "simple-3page.pdf"),
            str(fixtures_dir / "second-2page.pdf"),
            "-o",
            str(out),
        ]
        assert runner.invoke(app, args).exit_code == 0
        assert runner.invoke(app, args).exit_code == EXIT_COLLISION

    def test_merge_bad_range_is_usage_error(self, fixtures_dir, out_dir):
        result = runner.invoke(
            app,
            [
                "merge",
                f"{fixtures_dir / 'simple-3page.pdf'}::banana",
                str(fixtures_dir / "second-2page.pdf"),
                "-o",
                str(out_dir / "never.pdf"),
            ],
        )
        assert result.exit_code == EXIT_USAGE


class TestPasswordSources:
    def test_password_stdin_reads_utf8_crlf_without_trimming(
        self, fixtures_dir, out_dir, unicode_fixture_password
    ):
        out = out_dir / "stdin-unicode.pdf"
        result = runner.invoke(
            app,
            [
                "--password-stdin",
                "merge",
                str(fixtures_dir / "encrypted-unicode.pdf"),
                str(fixtures_dir / "second-2page.pdf"),
                "-o",
                str(out),
            ],
            input=f"{unicode_fixture_password}\r\n",
            env={"LDF_PASSWORD": None},
        )
        assert result.exit_code == 0, combined_output(result)
        with pikepdf.open(out) as pdf:
            assert len(pdf.pages) == 5

    def test_environment_password_succeeds_and_does_not_leak_to_next_invocation(
        self, fixtures_dir, out_dir, fixture_password
    ):
        first = out_dir / "env-success.pdf"
        first_result = runner.invoke(
            app,
            [
                "merge",
                str(fixtures_dir / "encrypted.pdf"),
                str(fixtures_dir / "second-2page.pdf"),
                "-o",
                str(first),
            ],
            env={"LDF_PASSWORD": fixture_password},
        )
        assert first_result.exit_code == 0, combined_output(first_result)
        assert cli_main._state["password"] is None
        assert cli_main._state["password_supplied"] is False
        assert cli_main._state["password_stdin_requested"] is False

        second = out_dir / "must-not-exist.pdf"
        second_result = runner.invoke(
            app,
            [
                "merge",
                str(fixtures_dir / "encrypted.pdf"),
                str(fixtures_dir / "second-2page.pdf"),
                "-o",
                str(second),
            ],
            env={"LDF_PASSWORD": None},
        )
        assert second_result.exit_code == EXIT_USAGE
        assert "--password-stdin" in second_result.stderr
        assert "LDF_PASSWORD" in second_result.stderr
        assert second_result.stdout == ""
        assert not second.exists()

    def test_environment_password_is_removed_before_operation(
        self, fixtures_dir, fixture_password, monkeypatch
    ):
        original = cli_main.organize_ops.inspect_pdf
        observed: list[tuple[str | None, str | None]] = []

        def observed_inspect(input_file, *, password=None):
            observed.append((os.environ.get("LDF_PASSWORD"), password))
            return original(input_file, password=password)

        monkeypatch.setattr(cli_main.organize_ops, "inspect_pdf", observed_inspect)
        result = runner.invoke(
            app,
            ["--json", "inspect", str(fixtures_dir / "encrypted.pdf")],
            env={"LDF_PASSWORD": fixture_password},
        )
        assert result.exit_code == 0, combined_output(result)
        assert observed == [(None, fixture_password)]
        assert_secret_absent(result, fixture_password)

    def test_password_stdin_takes_precedence_over_environment(
        self, fixtures_dir, out_dir, fixture_password
    ):
        out = out_dir / "stdin-precedence.pdf"
        result = runner.invoke(
            app,
            [
                "--password-stdin",
                "merge",
                str(fixtures_dir / "encrypted.pdf"),
                str(fixtures_dir / "second-2page.pdf"),
                "-o",
                str(out),
            ],
            input=f"{fixture_password}\n",
            env={"LDF_PASSWORD": "wrong-environment-password"},
        )
        assert result.exit_code == 0, combined_output(result)
        assert out.is_file()

    def test_wrong_stdin_password_does_not_fall_back_to_environment(
        self, fixtures_dir, out_dir, fixture_password
    ):
        wrong_password = "wrong-stdin-password"
        out = out_dir / "no-fallback.pdf"
        result = runner.invoke(
            app,
            [
                "--password-stdin",
                "merge",
                str(fixtures_dir / "encrypted.pdf"),
                str(fixtures_dir / "second-2page.pdf"),
                "-o",
                str(out),
            ],
            input=f"{wrong_password}\n",
            env={"LDF_PASSWORD": fixture_password},
        )
        assert result.exit_code == EXIT_FAILED
        assert "Wrong password" in result.stderr
        assert "encrypted.pdf" in result.stderr
        assert_secret_absent(result, wrong_password)
        assert not out.exists()

    @pytest.mark.parametrize(
        ("password_input", "expected_message"),
        [
            (b"", "requires one UTF-8 line"),
            (b"\xff\n", "must be valid UTF-8"),
        ],
    )
    def test_password_stdin_input_errors_are_sanitized_usage_failures(
        self, fixtures_dir, fixture_password, password_input, expected_message
    ):
        result = runner.invoke(
            app,
            [
                "--password-stdin",
                "inspect",
                str(fixtures_dir / "simple-3page.pdf"),
            ],
            input=password_input,
            env={"LDF_PASSWORD": fixture_password},
        )
        assert result.exit_code == EXIT_USAGE
        assert expected_message in result.stderr
        assert_secret_absent(result, fixture_password)

    def test_password_stdin_remains_explicit_source_when_stdin_is_tty(
        self, fixtures_dir, fixture_password, monkeypatch
    ):
        monkeypatch.setattr(cli_main, "_stdin_is_tty", lambda: True)

        def unexpected_prompt(*_args, **_kwargs):
            raise AssertionError("--password-stdin must outrank the hidden prompt")

        monkeypatch.setattr(cli_main.typer, "prompt", unexpected_prompt)
        result = runner.invoke(
            app,
            [
                "--password-stdin",
                "--json",
                "inspect",
                str(fixtures_dir / "encrypted.pdf"),
            ],
            input=f"{fixture_password}\n",
            env={"LDF_PASSWORD": None},
        )
        assert result.exit_code == 0, combined_output(result)
        assert json.loads(result.stdout)["encrypted"] is True
        assert_secret_absent(result, fixture_password)

    def test_wrong_password_behavior_remains_exit_one(self, fixtures_dir, out_dir):
        wrong_password = "synthetic-wrong-password"
        out = out_dir / "wrong-password.pdf"
        result = runner.invoke(
            app,
            [
                "merge",
                str(fixtures_dir / "encrypted.pdf"),
                str(fixtures_dir / "second-2page.pdf"),
                "-o",
                str(out),
            ],
            env={"LDF_PASSWORD": wrong_password},
        )
        assert result.exit_code == EXIT_FAILED
        assert "Wrong password" in result.stderr
        assert_secret_absent(result, wrong_password)
        assert not out.exists()

    def test_empty_environment_password_counts_as_supplied(
        self, fixtures_dir, monkeypatch
    ):
        monkeypatch.setattr(cli_main, "_stdin_is_tty", lambda: True)

        def unexpected_prompt(*_args, **_kwargs):
            raise AssertionError("a supplied environment value must suppress prompting")

        monkeypatch.setattr(cli_main.typer, "prompt", unexpected_prompt)
        result = runner.invoke(
            app,
            ["inspect", str(fixtures_dir / "encrypted.pdf")],
            env={"LDF_PASSWORD": ""},
        )
        assert result.exit_code == EXIT_FAILED
        assert "is encrypted" in result.stderr
        assert "One password is used for all encrypted inputs" in result.stderr

    def test_inspect_honors_password_stdin(
        self, fixtures_dir, unicode_fixture_password
    ):
        result = runner.invoke(
            app,
            [
                "--password-stdin",
                "--json",
                "inspect",
                str(fixtures_dir / "encrypted-unicode.pdf"),
            ],
            input=f"{unicode_fixture_password}\n",
            env={"LDF_PASSWORD": None},
        )
        assert result.exit_code == 0, combined_output(result)
        payload = json.loads(result.stdout)
        assert payload["encrypted"] is True
        assert payload["page_count"] == 3
        assert_secret_absent(result, unicode_fixture_password)

    def test_inspect_without_password_is_noninteractive_usage_error(self, fixtures_dir):
        result = runner.invoke(
            app,
            ["inspect", str(fixtures_dir / "encrypted.pdf")],
            env={"LDF_PASSWORD": None},
        )
        assert result.exit_code == EXIT_USAGE
        assert "--password-stdin" in result.stderr
        assert "LDF_PASSWORD" in result.stderr
        assert result.stdout == ""

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason="Windows NUL is a character device that is not an interactive console",
    )
    def test_windows_devnull_stdin_is_noninteractive(
        self, fixtures_dir, fixture_password, tmp_path
    ):
        environment = os.environ.copy()
        environment.pop("LDF_PASSWORD", None)
        environment["LDF_JOBS_ROOT"] = str(tmp_path / "jobs")
        source_root = Path(__file__).resolve().parents[2] / "src"
        prior_pythonpath = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(source_root)
            if not prior_pythonpath
            else f"{source_root}{os.pathsep}{prior_pythonpath}"
        )
        output = tmp_path / "must-not-exist.pdf"
        reports = tmp_path / "must-not-exist-reports"
        completed = subprocess.run(  # noqa: S603 - repository-owned CLI under test
            [
                sys.executable,
                "-m",
                "localdocforge.cli.main",
                "--report-dir",
                str(reports),
                "merge",
                str(fixtures_dir / "encrypted.pdf"),
                str(fixtures_dir / "second-2page.pdf"),
                "-o",
                str(output),
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=15,
            check=False,
        )
        assert completed.returncode == EXIT_USAGE
        assert completed.stdout == b""
        assert b"--password-stdin" in completed.stderr
        assert b"LDF_PASSWORD" in completed.stderr
        assert b"PDF password" not in completed.stderr
        secret = fixture_password.encode("utf-8")
        assert secret not in completed.stdout
        assert secret not in completed.stderr
        assert not output.exists()
        assert not reports.exists()

    def test_pdf_to_images_honors_environment_and_missing_password_is_usage_error(
        self, fixtures_dir, out_dir, tmp_path, fixture_password
    ):
        success_dir = out_dir / "encrypted-render"
        success = runner.invoke(
            app,
            [
                "pdf-to-images",
                str(fixtures_dir / "encrypted.pdf"),
                "-d",
                str(success_dir),
                "--pages",
                "1",
                "--dpi",
                "72",
            ],
            env={"LDF_PASSWORD": fixture_password},
        )
        assert success.exit_code == 0, combined_output(success)
        assert len(list(success_dir.glob("*.png"))) == 1

        missing_dir = tmp_path / "missing-password-render"
        missing = runner.invoke(
            app,
            [
                "pdf-to-images",
                str(fixtures_dir / "encrypted.pdf"),
                "-d",
                str(missing_dir),
                "--pages",
                "1",
            ],
            env={"LDF_PASSWORD": None},
        )
        assert missing.exit_code == EXIT_USAGE
        assert "--password-stdin" in missing.stderr
        assert "LDF_PASSWORD" in missing.stderr
        assert not missing_dir.exists()

    def test_hidden_interactive_prompt_fallback_still_works(
        self, fixtures_dir, fixture_password, monkeypatch
    ):
        monkeypatch.setattr(cli_main, "_stdin_is_tty", lambda: True)
        result = runner.invoke(
            app,
            ["--json", "inspect", str(fixtures_dir / "encrypted.pdf")],
            input=f"{fixture_password}\n",
            env={"LDF_PASSWORD": None},
        )
        assert result.exit_code == 0, combined_output(result)
        assert json.loads(result.stdout)["encrypted"] is True
        assert "PDF password" in result.output
        assert_secret_absent(result, fixture_password)

    def test_different_passwords_fail_clearly_under_one_password_v1(
        self, fixtures_dir, out_dir, fixture_password
    ):
        out = out_dir / "different-passwords.pdf"
        result = runner.invoke(
            app,
            [
                "merge",
                str(fixtures_dir / "encrypted.pdf"),
                str(fixtures_dir / "encrypted-unicode.pdf"),
                "-o",
                str(out),
            ],
            env={"LDF_PASSWORD": fixture_password},
        )
        assert result.exit_code == EXIT_FAILED
        assert "Wrong password" in result.stderr
        assert "encrypted-unicode.pdf" in result.stderr
        assert "One password is used for all encrypted inputs" in result.stderr
        assert_secret_absent(result, fixture_password)
        assert not out.exists()

    def test_hidden_prompt_different_passwords_keep_one_password_guidance(
        self, fixtures_dir, out_dir, fixture_password, monkeypatch
    ):
        monkeypatch.setattr(cli_main, "_stdin_is_tty", lambda: True)
        out = out_dir / "prompted-different-passwords.pdf"
        result = runner.invoke(
            app,
            [
                "merge",
                str(fixtures_dir / "encrypted.pdf"),
                str(fixtures_dir / "encrypted-unicode.pdf"),
                "-o",
                str(out),
            ],
            input=f"{fixture_password}\n",
            env={"LDF_PASSWORD": None},
        )
        assert result.exit_code == EXIT_FAILED
        assert "Wrong password" in result.stderr
        assert "encrypted-unicode.pdf" in result.stderr
        assert "One password is used for all encrypted inputs" in result.stderr
        assert_secret_absent(result, fixture_password)
        assert not out.exists()

    def test_password_absent_from_json_human_and_report_files(
        self, fixtures_dir, out_dir, tmp_path, fixture_password
    ):
        reports = tmp_path / "reports"
        json_result = runner.invoke(
            app,
            [
                "--json",
                "--report-dir",
                str(reports),
                "merge",
                str(fixtures_dir / "encrypted.pdf"),
                str(fixtures_dir / "second-2page.pdf"),
                "-o",
                str(out_dir / "json-secret-check.pdf"),
            ],
            env={"LDF_PASSWORD": fixture_password},
        )
        assert json_result.exit_code == 0, combined_output(json_result)
        assert json.loads(json_result.stdout)["status"] == "success"
        assert_secret_absent(json_result, fixture_password)
        report_files = sorted(reports.glob("merge-*.report.*"))
        assert len(report_files) == 2
        secret_bytes = fixture_password.encode("utf-8")
        for report_file in report_files:
            assert secret_bytes not in report_file.read_bytes()

        human_result = runner.invoke(
            app,
            [
                "--password-stdin",
                "merge",
                str(fixtures_dir / "encrypted.pdf"),
                str(fixtures_dir / "second-2page.pdf"),
                "-o",
                str(out_dir / "human-secret-check.pdf"),
            ],
            input=f"{fixture_password}\n",
            env={"LDF_PASSWORD": None},
        )
        assert human_result.exit_code == 0, combined_output(human_result)
        assert "Status    : success" in human_result.stdout
        assert_secret_absent(human_result, fixture_password)


class TestSplitAndOrganizeCommands:
    def test_split_every(self, fixtures_dir, out_dir):
        result = runner.invoke(
            app,
            ["split", str(fixtures_dir / "outline-6page.pdf"), "-d", str(out_dir), "--every", "2"],
        )
        assert result.exit_code == 0, result.output
        assert len(list(out_dir.glob("*.pdf"))) == 3

    def test_missing_input_is_usage_error(self, out_dir):
        result = runner.invoke(
            app, ["split", str(out_dir / "ghost.pdf"), "-d", str(out_dir)]
        )
        assert result.exit_code == EXIT_USAGE

    def test_rotate_via_cli(self, fixtures_dir, out_dir):
        out = out_dir / "cli-rotated.pdf"
        result = runner.invoke(
            app,
            ["rotate", str(fixtures_dir / "simple-3page.pdf"), "-o", str(out),
             "--degrees", "180", "--pages", "1"],
        )
        assert result.exit_code == 0, result.output
        with pikepdf.open(out) as pdf:
            assert int(pdf.pages[0].obj.get("/Rotate", 0)) == 180

    def test_crop_warns_not_redaction(self, fixtures_dir, out_dir):
        out = out_dir / "cli-cropped.pdf"
        result = runner.invoke(
            app,
            ["crop", str(fixtures_dir / "simple-3page.pdf"), "-o", str(out),
             "--box", "50,50,400,500"],
        )
        assert result.exit_code == 0, result.output
        assert "not" in combined_output(result).lower()
        assert "redaction" in combined_output(result).lower()

    def test_crop_bad_box_is_usage_error(self, fixtures_dir, out_dir):
        result = runner.invoke(
            app,
            ["crop", str(fixtures_dir / "simple-3page.pdf"), "-o", str(out_dir / "n.pdf"),
             "--box", "1,2,3"],
        )
        assert result.exit_code == EXIT_USAGE


class TestImageCommands:
    def test_images_to_pdf_with_glob(self, fixtures_dir, out_dir):
        out = out_dir / "cli-album.pdf"
        pattern = str(fixtures_dir / "images" / "*.jpg")
        result = runner.invoke(app, ["images-to-pdf", pattern, "-o", str(out)])
        assert result.exit_code == 0, result.output
        with pikepdf.open(out) as pdf:
            # photo, exif-rotated, srgb-tagged, and bad-profile fixtures
            assert len(pdf.pages) == 4

    def test_images_to_pdf_accepts_heic(self, fixtures_dir, out_dir):
        out = out_dir / "cli-heic.pdf"
        result = runner.invoke(
            app,
            ["images-to-pdf", str(fixtures_dir / "images" / "photo.heic"),
             "-o", str(out)],
        )
        assert result.exit_code == 0, result.output
        with pikepdf.open(out) as pdf:
            assert len(pdf.pages) == 1

    def test_images_to_pdf_no_glob_match(self, out_dir):
        result = runner.invoke(
            app, ["images-to-pdf", str(out_dir / "*.jpg"), "-o", str(out_dir / "n.pdf")]
        )
        assert result.exit_code == EXIT_USAGE

    def test_pdf_to_images_cli(self, fixtures_dir, out_dir):
        result = runner.invoke(
            app,
            ["pdf-to-images", str(fixtures_dir / "simple-3page.pdf"), "-d", str(out_dir),
             "--format", "jpeg", "--dpi", "72", "--pages", "1"],
        )
        assert result.exit_code == 0, result.output
        assert len(list(out_dir.glob("*.jpg"))) == 1


class TestConvertImagesCommand:
    def test_heic_glob_with_llm_preset(self, fixtures_dir, out_dir):
        pattern = str(fixtures_dir / "images" / "photo.heic")
        result = runner.invoke(
            app, ["convert-images", pattern, "-d", str(out_dir), "--preset", "llm"]
        )
        assert result.exit_code == 0, combined_output(result)
        assert (out_dir / "photo.jpg").is_file()
        assert "EXIF metadata" in result.output

    def test_format_and_max_dimension_flags(self, fixtures_dir, out_dir):
        result = runner.invoke(
            app,
            ["convert-images", str(fixtures_dir / "images" / "photo.jpg"),
             "-d", str(out_dir), "--format", "png", "--max-dimension", "200"],
        )
        assert result.exit_code == 0, combined_output(result)
        assert (out_dir / "photo.png").is_file()

    def test_unknown_preset_is_usage_error(self, fixtures_dir, out_dir):
        result = runner.invoke(
            app,
            ["convert-images", str(fixtures_dir / "images" / "photo.jpg"),
             "-d", str(out_dir), "--preset", "tiny"],
        )
        assert result.exit_code == EXIT_USAGE
        assert "llm" in combined_output(result)

    def test_unknown_format_is_usage_error(self, fixtures_dir, out_dir):
        result = runner.invoke(
            app,
            ["convert-images", str(fixtures_dir / "images" / "photo.jpg"),
             "-d", str(out_dir), "--format", "gif"],
        )
        assert result.exit_code == EXIT_USAGE

    def test_missing_input_is_usage_error(self, out_dir):
        result = runner.invoke(
            app,
            ["convert-images", str(out_dir / "absent.heic"), "-d", str(out_dir)],
        )
        assert result.exit_code == EXIT_USAGE

    def test_existing_output_respects_collision_policy(self, fixtures_dir, out_dir):
        (out_dir / "photo.jpg").write_bytes(b"occupied")
        result = runner.invoke(
            app,
            ["convert-images", str(fixtures_dir / "images" / "photo.jpg"),
             "-d", str(out_dir)],
        )
        assert result.exit_code == EXIT_COLLISION
        assert (out_dir / "photo.jpg").read_bytes() == b"occupied"


class TestCompressCommand:
    def test_compress_via_cli(self, fixtures_dir, out_dir):
        out = out_dir / "compressed.pdf"
        result = runner.invoke(
            app, ["compress", str(fixtures_dir / "simple-3page.pdf"), "-o", str(out)]
        )
        assert result.exit_code == 0, result.output
        assert "compress" in result.output
        with pikepdf.open(out) as pdf:
            assert len(pdf.pages) == 3

    def test_compress_unavailable_preset_is_usage_error(self, fixtures_dir, out_dir):
        result = runner.invoke(
            app,
            ["compress", str(fixtures_dir / "simple-3page.pdf"), "-o", str(out_dir / "n.pdf"),
             "--preset", "balanced"],
        )
        assert result.exit_code == EXIT_USAGE
        assert "not available in this build" in result.output

    def test_compress_collision_exit_code(self, fixtures_dir, out_dir):
        out = out_dir / "compressed-twice.pdf"
        args = ["compress", str(fixtures_dir / "simple-3page.pdf"), "-o", str(out)]
        assert runner.invoke(app, args).exit_code == 0
        assert runner.invoke(app, args).exit_code == EXIT_COLLISION


class TestInspectCommand:
    def test_inspect_json(self, fixtures_dir):
        result = runner.invoke(
            app, ["--json", "inspect", str(fixtures_dir / "simple-3page.pdf")]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["page_count"] == 3
        assert payload["encrypted"] is False


class TestReportDir:
    def test_report_files_written(self, fixtures_dir, out_dir, tmp_path):
        reports = tmp_path / "reports"
        result = runner.invoke(
            app,
            ["--report-dir", str(reports), "merge",
             str(fixtures_dir / "simple-3page.pdf"),
             str(fixtures_dir / "second-2page.pdf"),
             "-o", str(out_dir / "reported.pdf")],
        )
        assert result.exit_code == 0, result.output
        json_reports = list(reports.glob("merge-*.report.json"))
        text_reports = list(reports.glob("merge-*.report.txt"))
        assert len(json_reports) == 1 and len(text_reports) == 1
        payload = json.loads(json_reports[0].read_text(encoding="utf-8"))
        assert payload["status"] == "success"
        assert payload["engine"] == "pikepdf"
