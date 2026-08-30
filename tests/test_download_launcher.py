import argparse
import importlib.util
import unittest
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

    def test_download_only_does_not_require_codex(self):
        args = argparse.Namespace(download_only=True)
        self.assertFalse(launcher.needs_codex(args, []))

    def test_download_stop_stage_does_not_require_codex(self):
        args = argparse.Namespace(download_only=False)
        self.assertFalse(launcher.needs_codex(args, ["--stop-after", "download"]))

    def test_transcribe_stop_stage_does_not_require_codex(self):
        args = argparse.Namespace(download_only=False)
        self.assertFalse(launcher.needs_codex(args, ["--stop-after", "transcribe"]))

    def test_full_pipeline_requires_codex(self):
        args = argparse.Namespace(download_only=False)
        self.assertTrue(launcher.needs_codex(args, []))

    def test_download_only_requires_no_model_keys(self):
        args = argparse.Namespace(download_only=True)
        self.assertFalse(launcher.needs_openrouter(args, []))

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

    def test_full_pipeline_requires_openrouter(self):
        args = argparse.Namespace(download_only=False)
        self.assertTrue(launcher.needs_openrouter(args, []))


if __name__ == "__main__":
    unittest.main()
