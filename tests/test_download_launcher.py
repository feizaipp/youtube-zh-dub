import argparse
import importlib.util
import unittest
from pathlib import Path
from unittest import mock

import download_youtube


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

    def test_project_wrapper_preserves_selected_quality(self):
        completed = mock.Mock(returncode=0)
        with mock.patch.object(download_youtube.subprocess, "run", return_value=completed) as run:
            result = download_youtube.main(
                ["https://youtu.be/example", "--quality", "720p"]
            )
        self.assertEqual(result, 0)
        command = run.call_args.args[0]
        self.assertIn("--download-only", command)
        self.assertEqual(command[command.index("--quality") + 1], "720p")
        self.assertNotIn("--video-format", command)


if __name__ == "__main__":
    unittest.main()
