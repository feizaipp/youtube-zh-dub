import argparse
import tempfile
import time
import unittest
import urllib.parse
import wave
from pathlib import Path
from unittest import mock

from scripts import nginx_voice_sample


class NginxVoiceSampleTests(unittest.TestCase):
    def make_wav(self, path: Path, seconds: int = 5) -> None:
        with wave.open(str(path), "wb") as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16_000)
            stream.writeframes(b"\0\0" * 16_000 * seconds)

    def test_publish_creates_valid_expiring_url_and_revoke_removes_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.wav"
            document_root = root / "published"
            secret_file = root / "secret"
            self.make_wav(source)
            secret_file.write_text("s" * 64, encoding="utf-8")
            args = argparse.Namespace(
                source=source,
                document_root=document_root,
                secret_file=secret_file,
                base_url="http://203.0.113.10",
                expires_in=900,
            )

            document = nginx_voice_sample.publish(args)
            parsed = urllib.parse.urlparse(str(document["url"]))
            query = urllib.parse.parse_qs(parsed.query)
            expected = nginx_voice_sample.signature(
                int(query["expires"][0]), parsed.path, "s" * 64
            )

            self.assertEqual(query["md5"], [expected])
            self.assertGreater(int(document["expires_at"]), int(time.time()))
            self.assertTrue(Path(str(document["published_path"])).exists())

            revoked = nginx_voice_sample.revoke(
                argparse.Namespace(
                    document_root=document_root,
                    token_or_url=str(document["url"]),
                )
            )
            self.assertEqual(len(revoked["removed"]), 2)
            self.assertFalse(Path(str(document["published_path"])).exists())

    def test_rejects_short_or_non_pcm_wav(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "short.wav"
            self.make_wav(source, seconds=1)
            with self.assertRaisesRegex(ValueError, "5 到 60"):
                nginx_voice_sample.inspect_wav(source)

    def test_revoke_cli_does_not_require_publish_expiration(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch(
            "sys.argv",
            [
                "nginx_voice_sample.py",
                "--document-root",
                folder,
                "revoke",
                "A" * 32,
            ],
        ):
            self.assertEqual(nginx_voice_sample.main(), 0)


if __name__ == "__main__":
    unittest.main()
