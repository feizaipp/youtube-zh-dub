import argparse
import importlib.util
import tempfile
import unittest
from unittest import mock
from pathlib import Path

LAUNCHER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_youtube_zh_dub.py"
SPEC = importlib.util.spec_from_file_location("youtube_zh_dub_launcher", LAUNCHER_PATH)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)


class SkillLauncherTests(unittest.TestCase):
    def test_download_only_adds_download_stop_stage(self):
        args = argparse.Namespace(debug_seconds=None, download_only=True, quality="1080p")
        command = launcher.build_command(
            args,
            ["--video-format", "bestvideo", "--audio-format", "bestaudio"],
            Path("/tmp/youtube_dub.py"),
            Path("/tmp/output/Video title"),
            "https://www.youtube.com/watch?v=example",
        )
        self.assertIn("--full", command)
        self.assertEqual(command[command.index("--stop-after") + 1], "download")
        self.assertEqual(command[command.index("--video-format") + 1], "bestvideo")
        self.assertEqual(command[command.index("--audio-format") + 1], "bestaudio")
        self.assertEqual(command[command.index("--quality") + 1], "1080p")

    def test_stop_after_download_is_download_only_without_shortcut(self):
        args = argparse.Namespace(download_only=False)
        self.assertTrue(
            launcher.is_download_only(args, ["--stop-after", "download"])
        )

    def test_download_only_requires_no_model_keys(self):
        args = argparse.Namespace(download_only=True)
        self.assertFalse(launcher.needs_openrouter(args, []))

    def test_download_cover_is_a_standalone_local_operation(self):
        args = argparse.Namespace(download_cover=True)
        self.assertTrue(launcher.is_cover_only(args))
        self.assertFalse(launcher.needs_openrouter(args, []))

    def test_prepare_environment_loads_parent_dashscope_secrets(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            project = parent / "youtube-zh-dub"
            project.mkdir()
            secrets = parent / ".secrets"
            secrets.mkdir()
            (secrets / "dashscope.env").write_text(
                "DASHSCOPE_API_KEY='test-key'\n"
                "ALIYUN_COSYVOICE_VOICE=cosyvoice-v3.5-flash-test\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                launcher.os.environ,
                {
                    "DASHSCOPE_API_KEY": "",
                    "ALIYUN_COSYVOICE_VOICE": "",
                },
                clear=False,
            ):
                environment = launcher.prepare_environment(project)

            self.assertEqual(environment["DASHSCOPE_API_KEY"], "test-key")
            self.assertEqual(
                environment["ALIYUN_COSYVOICE_VOICE"],
                "cosyvoice-v3.5-flash-test",
            )

    def test_title_directory_replaces_whitespace_with_hyphens(self):
        self.assertEqual(
            launcher.safe_directory_name("My  Video\tTitle", "XjSJ6ybS9I8"),
            "My-Video-Title",
        )

    def test_title_directory_removes_special_characters(self):
        self.assertEqual(
            launcher.safe_directory_name(
                "Create Slides, Docs, and Templates", "syML5KT-HzI"
            ),
            "Create-Slides-Docs-and-Templates",
        )
        self.assertEqual(
            launcher.safe_directory_name("AI：从入门，到实践！", "example"),
            "AI从入门到实践",
        )

    def test_cover_candidates_prefer_maximum_resolution(self):
        self.assertEqual(
            launcher.cover_candidates("XjSJ6ybS9I8"),
            [
                "https://i.ytimg.com/vi/XjSJ6ybS9I8/maxresdefault.jpg",
                "https://i.ytimg.com/vi/XjSJ6ybS9I8/sddefault.jpg",
                "https://i.ytimg.com/vi/XjSJ6ybS9I8/hqdefault.jpg",
                "https://i.ytimg.com/vi/XjSJ6ybS9I8/mqdefault.jpg",
                "https://i.ytimg.com/vi/XjSJ6ybS9I8/default.jpg",
            ],
        )

    def test_normal_workflow_automatically_downloads_cover(self):
        self.assertTrue(
            launcher.should_download_cover(
                argparse.Namespace(download_cover=False, dry_run=False)
            )
        )
        self.assertFalse(
            launcher.should_download_cover(
                argparse.Namespace(download_cover=True, dry_run=False)
            )
        )
        self.assertFalse(
            launcher.should_download_cover(
                argparse.Namespace(download_cover=False, dry_run=True)
            )
        )

    def test_normal_workflow_continues_to_pipeline_after_cover_download(self):
        with tempfile.TemporaryDirectory() as temporary:
            workdir = Path(temporary) / "output"
            with (
                mock.patch.object(
                    launcher, "find_pipeline", return_value=Path(temporary) / "pipeline.py"
                ),
                mock.patch.object(launcher, "prepare_environment", return_value={}),
                mock.patch.object(launcher.shutil, "which", return_value="/bin/true"),
                mock.patch.object(
                    launcher, "download_cover", return_value=workdir / "cover.jpg"
                ) as download_cover,
                mock.patch.object(
                    launcher.subprocess, "run", return_value=mock.Mock(returncode=0)
                ) as run_pipeline,
                mock.patch.object(
                    launcher.sys,
                    "argv",
                    [
                        "run_youtube_zh_dub.py",
                        "https://youtu.be/XjSJ6ybS9I8",
                        "--workdir",
                        str(workdir),
                        "--tts-backend",
                        "cosyvoice3-source",
                    ],
                ),
            ):
                self.assertEqual(launcher.main(), 0)
            download_cover.assert_called_once_with(
                workdir, "XjSJ6ybS9I8", force=False
            )
            run_pipeline.assert_called_once()

    def test_local_transcribe_only_does_not_require_openrouter(self):
        args = argparse.Namespace(download_only=False)
        passthrough = ["--stop-after", "transcribe"]
        self.assertFalse(launcher.needs_openrouter(args, passthrough))

    def test_local_translate_only_does_not_require_openrouter(self):
        args = argparse.Namespace(download_only=False)
        self.assertFalse(
            launcher.needs_openrouter(args, ["--stop-after", "translate"])
        )

    def test_remote_transcribe_only_requires_openrouter(self):
        args = argparse.Namespace(download_only=False)
        passthrough = [
            "--stop-after", "transcribe",
            "--transcriber-backend", "openrouter-whisper1",
        ]
        self.assertTrue(launcher.needs_openrouter(args, passthrough))

    def test_default_aliyun_pipeline_does_not_require_openrouter(self):
        args = argparse.Namespace(download_only=False)
        self.assertFalse(launcher.needs_openrouter(args, []))

    def test_default_pipeline_requires_dashscope(self):
        args = argparse.Namespace(download_only=False, download_cover=False)
        self.assertTrue(launcher.needs_dashscope(args, []))

    def test_mai_pipeline_requires_openrouter(self):
        args = argparse.Namespace(download_only=False)
        self.assertTrue(launcher.needs_openrouter(args, ["--tts-backend", "mai"]))

    def test_aliyun_pipeline_requires_dashscope_only_when_synthesizing(self):
        args = argparse.Namespace(download_only=False, download_cover=False)
        self.assertTrue(
            launcher.needs_dashscope(
                args, ["--tts-backend", "aliyun-cosyvoice"]
            )
        )
        self.assertFalse(
            launcher.needs_dashscope(
                args,
                [
                    "--tts-backend",
                    "aliyun-cosyvoice",
                    "--stop-after",
                    "translate",
                ],
            )
        )


if __name__ == "__main__":
    unittest.main()
