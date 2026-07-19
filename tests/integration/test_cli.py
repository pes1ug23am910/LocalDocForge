"""CLI contract tests: commands, exit codes, JSON output."""

from __future__ import annotations

import json

import pikepdf
from typer.testing import CliRunner

from localdocforge.cli.main import (
    EXIT_COLLISION,
    EXIT_USAGE,
    app,
)

runner = CliRunner()


def combined_output(result) -> str:
    try:
        return result.output + (result.stderr or "")
    except ValueError:
        return result.output


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
        engines = {e["name"]: e for e in payload["engines"]}
        assert engines["pikepdf"]["available"] is True
        assert engines["pillow"]["available"] is True
        capabilities = {c["id"]: c for c in payload["capabilities"]}
        assert capabilities["merge"]["available"] is True
        assert capabilities["ocr"]["available"] is False
        assert capabilities["office-to-pdf"]["available"] is False


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
            assert len(pdf.pages) == 2  # photo.jpg + exif-rotated.jpg

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
