import argparse
import base64
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import youtube_dub


class TimestampTests(unittest.TestCase):
    def test_seconds_to_srt_rounds_milliseconds(self):
        self.assertEqual(youtube_dub.seconds_to_srt(3661.2346), "01:01:01,235")

    def test_boundaries_cover_timeline_and_prefer_silence(self):
        result = youtube_dub.choose_boundaries(38.0, [14.2, 29.8], 15.0)
        self.assertEqual(result, [0.0, 14.2, 29.8, 38.0])

    def test_short_tail_is_merged(self):
        result = youtube_dub.choose_boundaries(16.0, [], 15.0)
        self.assertEqual(result, [0.0, 16.0])

    def test_write_srt(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.srt"
            youtube_dub.write_srt(path, [youtube_dub.Segment(0, 0, 1.5, "你好")])
            self.assertEqual(path.read_text(), "1\n00:00:00,000 --> 00:00:01,500\n你好\n")

    def test_corrected_sentences_use_real_word_boundaries_and_keep_silence(self):
        words = [
            youtube_dub.TimedWord("one", 1.0, 1.4),
            youtube_dub.TimedWord("two", 1.5, 1.9),
            youtube_dub.TimedWord("three", 4.0, 4.5),
            youtube_dub.TimedWord("four", 4.6, 5.0),
        ]
        result = youtube_dub.align_sentences_to_timed_words(
            words, ["One two.", "Three four."]
        )
        self.assertEqual(result[0], youtube_dub.Segment(0, 1.0, 1.9, "One two."))
        self.assertEqual(result[1], youtube_dub.Segment(1, 4.0, 5.0, "Three four."))
        self.assertEqual(result[1].start - result[0].end, 2.1)

    def test_word_alignment_survives_polish_substitutions(self):
        words = [
            youtube_dub.TimedWord("CISP", 10.0, 10.5),
            youtube_dub.TimedWord("starts", 10.6, 11.0),
            youtube_dub.TimedWord("now", 11.1, 11.4),
        ]
        result = youtube_dub.align_sentences_to_timed_words(
            words, ["CISSP starts now."]
        )
        self.assertEqual(result, [youtube_dub.Segment(0, 10.0, 11.4, "CISSP starts now.")])

    def test_word_alignment_rejects_unrelated_polish_output(self):
        words = [
            youtube_dub.TimedWord("original", 1.0, 1.4),
            youtube_dub.TimedWord("transcript", 1.5, 2.0),
        ]
        with self.assertRaisesRegex(youtube_dub.PipelineError, "差异过大"):
            youtube_dub.align_sentences_to_timed_words(
                words, ["Completely unrelated replacement sentence."]
            )

    def test_segment_fingerprint_changes_with_corrected_text(self):
        before = [youtube_dub.Segment(0, 0.0, 1.0, "The big")]
        after = [youtube_dub.Segment(0, 0.0, 1.0, "The biggest mistake.")]
        self.assertNotEqual(
            youtube_dub.segments_fingerprint(before),
            youtube_dub.segments_fingerprint(after),
        )


class TextBatchingTests(unittest.TestCase):
    def test_polish_batches_split_only_after_complete_chunk_sentence(self):
        segments = [
            youtube_dub.Segment(0, 0.0, 1.0, "A chunk without an ending"),
            youtube_dub.Segment(1, 1.0, 2.0, "continues and now ends."),
            youtube_dub.Segment(2, 2.0, 3.0, "A final sentence."),
        ]
        batches = youtube_dub.batch_english_for_polish(
            segments, maximum_characters=100
        )
        self.assertEqual([[item.id for item in batch] for batch in batches], [[0, 1], [2]])

    def test_translation_batches_default_to_six_thousand_characters(self):
        segments = [
            youtube_dub.Segment(0, 0.0, 1.0, "a" * 3_000),
            youtube_dub.Segment(1, 1.0, 2.0, "b" * 3_000),
        ]
        batches = youtube_dub.batch_segments(segments)
        self.assertEqual([[item.id for item in batch] for batch in batches], [[0], [1]])

    def test_text_batch_cache_requires_same_model_and_request(self):
        args = argparse.Namespace(force=False, text_model="model-a")
        result = {"sentences": ["Hello."]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "batch.json"
            youtube_dub.write_text_batch_cache(
                args,
                path,
                source_fingerprint="source-a",
                request_fingerprint="request-a",
                result=result,
            )
            self.assertEqual(
                youtube_dub.load_text_batch_cache(
                    args,
                    path,
                    source_fingerprint="source-a",
                    request_fingerprint="request-a",
                ),
                result,
            )
            changed_model = argparse.Namespace(force=False, text_model="model-b")
            self.assertIsNone(
                youtube_dub.load_text_batch_cache(
                    changed_model,
                    path,
                    source_fingerprint="source-a",
                    request_fingerprint="request-a",
                )
            )
            self.assertIsNone(
                youtube_dub.load_text_batch_cache(
                    args,
                    path,
                    source_fingerprint="source-a",
                    request_fingerprint="request-b",
                )
            )


class AudioFilterTests(unittest.TestCase):
    def test_atempo_chain_supports_large_factor(self):
        self.assertEqual(
            youtube_dub.atempo_chain(5.0),
            "atempo=2.00000000,atempo=2.00000000,atempo=1.25000000",
        )

    def test_translation_character_count_ignores_spacing_and_punctuation(self):
        self.assertEqual(youtube_dub.translation_character_count("很正常。"), 3)
        self.assertEqual(youtube_dub.translation_character_count("Security+ 备考，很枯燥。"), 14)

    def test_overlong_segments_respects_comfortable_tempo_cap(self):
        segments = [
            youtube_dub.Segment(0, 0.0, 2.0, "第一句。"),
            youtube_dub.Segment(1, 2.0, 4.0, "第二句。"),
        ]
        result = youtube_dub.overlong_segments(
            segments,
            {0: 2.2, 1: 2.4},
            1.15,
        )
        self.assertEqual([item.id for item in result], [1])

    def test_tts_trim_accepts_codec_rounded_hundred_milliseconds(self):
        with mock.patch.object(youtube_dub, "run"), mock.patch.object(
            youtube_dub, "probe_duration", return_value=0.095
        ):
            youtube_dub.trim_tts_edge_silence(Path("source.mp3"), Path("trimmed.wav"))

        with mock.patch.object(youtube_dub, "run"), mock.patch.object(
            youtube_dub, "probe_duration", return_value=0.089
        ), self.assertRaises(youtube_dub.PipelineError):
            youtube_dub.trim_tts_edge_silence(Path("source.mp3"), Path("trimmed.wav"))


class ConcurrencyTests(unittest.TestCase):
    def test_default_worker_counts_are_bounded(self):
        args = youtube_dub.build_parser().parse_args([])
        self.assertEqual(args.transcribe_workers, 3)
        self.assertEqual(args.tts_workers, 4)
        self.assertEqual(args.fit_workers, 4)

        args.transcribe_workers = 0
        with self.assertRaises(youtube_dub.PipelineError):
            youtube_dub.validate_args(args)

    def test_transcription_runs_concurrently_and_restores_id_order(self):
        args = argparse.Namespace(
            force=True,
            url="https://youtu.be/example",
            start_seconds=0.0,
            chunk_seconds=10.0,
            transcriber_model="openai/whisper-1",
            transcriber_backend="openrouter-whisper1",
            transcribe_workers=3,
        )
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def fake_extract(_source, destination, _start, _end):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(destination.stem.encode("ascii"))

        def fake_request(_endpoint, _api_key, payload, **_kwargs):
            nonlocal active, maximum_active
            name = base64.b64decode(payload["input_audio"]["data"]).decode("ascii")
            index = int(name.rsplit("_", 1)[1])
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.03 - index * 0.005)
                return {
                    "text": f"Sentence {index}.",
                    "words": [
                        {"word": "Sentence", "start": 0.1, "end": 0.5},
                        {"word": str(index), "start": 0.6, "end": 0.8},
                    ],
                }
            finally:
                with lock:
                    active -= 1

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            youtube_dub, "probe_duration", return_value=30.0
        ), mock.patch.object(
            youtube_dub, "detect_silence_midpoints", return_value=[]
        ), mock.patch.object(
            youtube_dub, "extract_segment", side_effect=fake_extract
        ), mock.patch.object(
            youtube_dub, "require_openrouter_api_key", return_value="test-key"
        ), mock.patch.object(
            youtube_dub, "openrouter_request", side_effect=fake_request
        ):
            workdir = Path(folder)
            result = youtube_dub.transcribe_audio(args, workdir, workdir / "audio.wav")
            document = youtube_dub.read_json(workdir / "transcript.en.json")

        self.assertGreaterEqual(maximum_active, 2)
        self.assertEqual([segment.id for segment in result], [0, 1, 2])
        self.assertEqual([segment.text for segment in result], [
            "Sentence 0.",
            "Sentence 1.",
            "Sentence 2.",
        ])
        self.assertTrue(document["complete"])
        self.assertEqual(document["workers"], 3)
        self.assertEqual(document["timestamp_pipeline_version"], 1)
        self.assertEqual(len(document["words"]), 6)
        self.assertEqual(document["words"][2]["start"], 10.1)

    def test_tts_preparation_runs_concurrently(self):
        args = argparse.Namespace(tts_workers=4)
        segments = [
            youtube_dub.Segment(index, float(index), float(index + 1), f"句子{index}。")
            for index in range(4)
        ]
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def fake_prepare(_args, segment, _raw_dir, _trimmed_dir, *, force=False):
            nonlocal active, maximum_active
            self.assertFalse(force)
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.02)
                return Path(f"{segment.id}.wav"), 1.0, f"key-{segment.id}"
            finally:
                with lock:
                    active -= 1

        with mock.patch.object(
            youtube_dub, "ensure_tts_source", side_effect=fake_prepare
        ):
            prepared = youtube_dub.prepare_tts_sources(
                args, segments, Path("raw"), Path("trimmed")
            )

        self.assertGreaterEqual(maximum_active, 2)
        self.assertEqual(sorted(prepared), [0, 1, 2, 3])

    def test_audio_fitting_runs_concurrently_and_restores_source_order(self):
        args = argparse.Namespace(fit_workers=4, max_tempo=1.15)
        segments = [
            youtube_dub.Segment(index, float(index), float(index + 1), f"句子{index}。")
            for index in range(4)
        ]
        prepared = {
            segment.id: (Path(f"source-{segment.id}.wav"), 1.0, f"key-{segment.id}")
            for segment in segments
        }
        active = 0
        maximum_active = 0
        lock = threading.Lock()
        checkpoints: list[list[int]] = []

        def fake_fit(
            _source,
            _destination,
            target_duration,
            _max_tempo,
            *,
            generated_duration=None,
            output_duration=None,
        ):
            nonlocal active, maximum_active
            index = int(_source.stem.split("-")[1])
            self.assertEqual(generated_duration, 1.0)
            self.assertEqual(output_duration, 1.0)
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                time.sleep(0.03 - index * 0.005)
                return {
                    "generated_duration": 1.0,
                    "target_duration": target_duration,
                    "tempo": 1.0,
                    "warning": None,
                }
            finally:
                with lock:
                    active -= 1

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            youtube_dub, "fit_audio_to_window", side_effect=fake_fit
        ):
            fitted, reports = youtube_dub.fit_audio_segments(
                args,
                segments,
                prepared,
                Path(folder) / "nested" / "fitted",
                4.0,
                on_progress=lambda values: checkpoints.append(
                    [int(item["id"]) for item in values]
                ),
            )

        self.assertGreaterEqual(maximum_active, 2)
        self.assertEqual(
            [path.name.split("_", 2)[1] for path in fitted],
            ["0000", "0001", "0002", "0003"],
        )
        self.assertEqual([item["id"] for item in reports], [0, 1, 2, 3])
        self.assertEqual(checkpoints[-1], [0, 1, 2, 3])


class DownloadTests(unittest.TestCase):
    def test_quality_maps_to_height_capped_best_video(self):
        self.assertEqual(
            youtube_dub.video_format_for_quality("720p"),
            "bestvideo[height<=720]",
        )
        self.assertEqual(
            youtube_dub.video_format_for_quality("1080p"),
            "bestvideo[height<=1080]",
        )
        self.assertEqual(youtube_dub.video_format_for_quality("best"), "bestvideo")

    def test_unknown_quality_is_rejected(self):
        with self.assertRaises(youtube_dub.PipelineError):
            youtube_dub.video_format_for_quality("480p")

    def test_download_command_preserves_partial_file_unless_forced(self):
        args = mock.Mock(force=False, url="https://youtu.be/example")
        with mock.patch.object(youtube_dub, "yt_dlp_common", return_value=["yt-dlp"]):
            command = youtube_dub.build_download_command(
                args, "134+249", Path("source.%(ext)s")
            )
        self.assertNotIn("--force-overwrites", command)

        args.force = True
        with mock.patch.object(youtube_dub, "yt_dlp_common", return_value=["yt-dlp"]):
            forced = youtube_dub.build_download_command(
                args, "134+249", Path("source.%(ext)s")
            )
        self.assertIn("--force-overwrites", forced)

    def test_node_enables_remote_ejs_challenge_solver(self):
        args = mock.Mock(cookies_from_browser=None, proxy=None)
        with mock.patch.object(youtube_dub.shutil, "which", return_value="/usr/bin/node"), mock.patch.object(
            youtube_dub.urllib.request, "getproxies", return_value={}
        ):
            command = youtube_dub.yt_dlp_common(args)
        self.assertIn("--js-runtimes", command)
        self.assertIn("--remote-components", command)
        self.assertIn("ejs:github", command)
        self.assertIn("--http-chunk-size", command)
        self.assertIn("256K", command)
        self.assertIn("--throttled-rate", command)
        self.assertIn("50K", command)

    def test_retained_video_and_audio_get_stable_names(self):
        with tempfile.TemporaryDirectory() as folder:
            workdir = Path(folder)
            video = workdir / "source.f399.mp4"
            audio = workdir / "source.f251.webm"
            video.write_bytes(b"video")
            audio.write_bytes(b"audio")

            stream_types = {
                video.name: {"video"},
                audio.name: {"audio"},
            }
            with mock.patch.object(
                youtube_dub,
                "probe_stream_types",
                side_effect=lambda path: stream_types[path.name],
            ):
                stable_video, stable_audio = youtube_dub.preserve_downloaded_streams(
                    workdir, "source"
                )

            self.assertEqual(stable_video.name, "source_video_original.mp4")
            self.assertEqual(stable_audio.name, "source_audio_original.webm")
            self.assertTrue(stable_video.exists())
            self.assertTrue(stable_audio.exists())

    def test_av1_is_transcoded_for_quicktime(self):
        options = youtube_dub.quicktime_video_options("av1")
        self.assertIn("libx264", options)
        self.assertIn("yuv420p", options)
        self.assertIn("avc1", options)

    def test_h264_is_copied_for_quicktime(self):
        options = youtube_dub.quicktime_video_options("h264")
        self.assertEqual(options, ["-c:v", "copy", "-tag:v", "avc1"])

    def test_quicktime_subtitle_delays_only_first_zero_timestamp(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.srt"
            destination = Path(folder) / "quicktime.srt"
            source.write_text(
                "1\n00:00:00,000 --> 00:00:15,000\n第一条\n\n"
                "2\n00:00:15,000 --> 00:00:30,000\n第二条\n",
                encoding="utf-8",
            )
            youtube_dub.prepare_quicktime_subtitle(source, destination)
            result = destination.read_text(encoding="utf-8")
            self.assertIn("00:00:00,100 --> 00:00:15,000", result)
            self.assertIn("00:00:15,000 --> 00:00:30,000", result)

    def test_bilibili_output_burns_subtitles_and_copies_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            workdir = Path(folder)
            source = workdir / "dubbed.zh.mp4"
            subtitle = workdir / "transcript.zh.srt"
            source.write_bytes(b"video")
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n中文字幕\n",
                encoding="utf-8",
            )
            observed: list[str] = []

            def fake_run(command, **_kwargs):
                observed.extend(command)
                Path(command[-1]).write_bytes(b"bilibili")
                return mock.Mock()

            with mock.patch.object(youtube_dub, "run", side_effect=fake_run):
                output = youtube_dub.burn_bilibili_subtitles(
                    workdir, source, subtitle
                )

            self.assertEqual(output.name, "dubbed.zh.bilibili.mp4")
            self.assertEqual(output.read_bytes(), b"bilibili")
            self.assertIn("-vf", observed)
            self.assertIn("subtitles=filename=", observed[observed.index("-vf") + 1])
            self.assertEqual(observed[observed.index("-c:a") + 1], "copy")
            self.assertIn("-sn", observed)
            self.assertIn("libx264", observed)

    def test_bilibili_output_is_reused_when_inputs_are_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            workdir = Path(folder)
            source = workdir / "dubbed.zh.mp4"
            subtitle = workdir / "transcript.zh.srt"
            output = workdir / "dubbed.zh.bilibili.mp4"
            source.write_bytes(b"video")
            subtitle.write_text("subtitle", encoding="utf-8")
            output.write_bytes(b"existing")
            os.utime(output, (output.stat().st_atime, max(source.stat().st_mtime, subtitle.stat().st_mtime) + 1))
            youtube_dub.write_json(
                workdir / "segments" / "bilibili_subtitle_render.json",
                {
                    "render_version": youtube_dub.BILIBILI_SUBTITLE_RENDER_VERSION,
                    "source_mtime_ns": source.stat().st_mtime_ns,
                    "subtitle_mtime_ns": subtitle.stat().st_mtime_ns,
                    "font": "Noto Sans CJK SC",
                },
            )

            with mock.patch.object(youtube_dub, "run") as mocked_run:
                result = youtube_dub.burn_bilibili_subtitles(
                    workdir, source, subtitle
                )

            self.assertEqual(result, output)
            mocked_run.assert_not_called()


class TranslationValidationTests(unittest.TestCase):
    def test_translation_ids_must_match(self):
        batch = [youtube_dub.Segment(2, 0, 1, "hello")]
        with self.assertRaises(youtube_dub.PipelineError):
            youtube_dub.validate_translations(batch, {"segments": [{"id": 3, "zh": "你好"}]})

    def test_code_fenced_json_is_accepted(self):
        value = youtube_dub.parse_json_content('```json\n{"segments": []}\n```')
        self.assertEqual(value, {"segments": []})

    def test_top_level_translation_array_is_accepted(self):
        value = youtube_dub.parse_json_content('[{"id": 1, "zh": "你好。"}]')
        self.assertEqual(value, {"segments": [{"id": 1, "zh": "你好。"}]})


class CodexTextBackendTests(unittest.TestCase):
    def test_codex_subprocess_does_not_receive_model_api_configuration(self):
        args = argparse.Namespace(text_model=None)
        schema = youtube_dub.segment_translation_schema()
        observed: dict[str, object] = {}

        def fake_run(command, **kwargs):
            observed["command"] = command
            observed["environment"] = kwargs["env"]
            output_index = command.index("--output-last-message") + 1
            Path(command[output_index]).write_text(
                '{"segments":[{"id":0,"zh":"你好。"}]}', encoding="utf-8"
            )
            return mock.Mock(stdout="", stderr="")

        with mock.patch.object(youtube_dub.shutil, "which", return_value="/usr/bin/codex"), mock.patch.object(
            youtube_dub.subprocess, "run", side_effect=fake_run
        ), mock.patch.dict(
            os.environ,
            {
                "OPENROUTER_API_KEY": "must-not-leak",
                "OPENAI_API_KEY": "also-must-not-leak",
                "OPENAI_BASE_URL": "https://openrouter.example/v1",
            },
        ):
            result = youtube_dub.codex_json_completion(
                args, "Translate.", "Hello.", schema
            )

        self.assertEqual(result["segments"][0]["zh"], "你好。")
        self.assertIn("--ignore-user-config", observed["command"])
        environment = observed["environment"]
        self.assertNotIn("OPENROUTER_API_KEY", environment)
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("OPENAI_BASE_URL", environment)

    def test_polish_uses_codex_backend_without_openrouter(self):
        args = argparse.Namespace(
            force=True,
            text_model=None,
            url="https://youtu.be/example",
        )
        raw = [
            youtube_dub.Segment(
                0, 0.0, 2.0, "Hello hello world this is a transcript test for today"
            )
        ]
        raw_words = [
            youtube_dub.TimedWord(word, index * 0.15, index * 0.15 + 0.1)
            for index, word in enumerate(
                "Hello hello world this is a transcript test for today".split()
            )
        ]
        result_value = {
            "sentences": ["Hello world this is a transcript test for today."],
            "corrections": [
                {"before": "hello hello", "after": "hello", "reason": "repetition"}
            ],
        }
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            youtube_dub, "codex_json_completion", return_value=result_value
        ), mock.patch.object(
            youtube_dub,
            "openrouter_request",
            side_effect=AssertionError("text work must not call OpenRouter"),
        ):
            polished = youtube_dub.polish_transcript(
                args, Path(folder), raw, raw_words
            )
            document = youtube_dub.read_json(Path(folder) / "transcript.en.polished.json")

        self.assertEqual(
            polished[0].text, "Hello world this is a transcript test for today."
        )
        self.assertEqual(document["text_backend"], "codex-cli")

    def test_polish_resumes_from_per_batch_cache(self):
        args = argparse.Namespace(
            force=False,
            text_model=None,
            url="https://youtu.be/example",
        )
        raw = [youtube_dub.Segment(0, 0.0, 2.0, "One two three four five.")]
        raw_words = [
            youtube_dub.TimedWord(word, index * 0.3, index * 0.3 + 0.2)
            for index, word in enumerate("One two three four five".split())
        ]
        result_value = {
            "sentences": ["One two three four five."],
            "corrections": [],
        }
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            youtube_dub, "codex_json_completion", return_value=result_value
        ) as completion, mock.patch.object(
            youtube_dub,
            "openrouter_request",
            side_effect=AssertionError("text work must not call OpenRouter"),
        ):
            workdir = Path(folder)
            youtube_dub.polish_transcript(args, workdir, raw, raw_words)
            (workdir / "transcript.en.polished.json").unlink()
            polished = youtube_dub.polish_transcript(args, workdir, raw, raw_words)
            batch_document = youtube_dub.read_json(
                workdir / "segments" / "english_polished_batches" / "batch_0000.json"
            )

        self.assertEqual(completion.call_count, 1)
        self.assertEqual(polished[0].text, "One two three four five.")
        self.assertEqual(batch_document["text_backend"], "codex-cli")
        self.assertIn("request_fingerprint", batch_document)

    def test_translation_uses_codex_backend_without_openrouter(self):
        args = argparse.Namespace(
            force=True,
            text_model=None,
            url="https://youtu.be/example",
            start_seconds=0.0,
        )
        english = [youtube_dub.Segment(0, 0.0, 2.0, "Hello world.")]
        result_value = {
            "topic": {
                "english": "Greeting",
                "chinese": "问候",
                "expertise": "General communication",
            },
            "segments": [{"id": 0, "zh": "大家好。"}],
        }
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            youtube_dub, "codex_json_completion", return_value=result_value
        ), mock.patch.object(
            youtube_dub,
            "openrouter_request",
            side_effect=AssertionError("text work must not call OpenRouter"),
        ):
            translated = youtube_dub.translate_segments(args, Path(folder), english)
            document = youtube_dub.read_json(Path(folder) / "transcript.zh.json")

        self.assertEqual(translated[0].text, "大家好。")
        self.assertEqual(document["text_backend"], "codex-cli")

    def test_translation_resumes_all_batches_and_accepts_ellipsis(self):
        args = argparse.Namespace(
            force=False,
            text_model=None,
            url="https://youtu.be/example",
            start_seconds=0.0,
        )
        english = [
            youtube_dub.Segment(0, 0.0, 2.0, "A" * 6_001 + "."),
            youtube_dub.Segment(1, 2.0, 4.0, "B" * 6_001 + "."),
        ]
        results = [
            {
                "topic": {
                    "english": "Test",
                    "chinese": "测试",
                    "expertise": "Testing",
                },
                "segments": [{"id": 0, "zh": "第一段……"}],
            },
            {"segments": [{"id": 1, "zh": "第二段…"}]},
        ]
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            youtube_dub, "codex_json_completion", side_effect=results
        ) as completion, mock.patch.object(
            youtube_dub,
            "openrouter_request",
            side_effect=AssertionError("text work must not call OpenRouter"),
        ):
            workdir = Path(folder)
            youtube_dub.translate_segments(args, workdir, english)
            (workdir / "transcript.zh.json").unlink()
            translated = youtube_dub.translate_segments(args, workdir, english)

        self.assertEqual(completion.call_count, 2)
        self.assertEqual([item.text for item in translated], ["第一段……", "第二段…"])

    def test_timing_shortening_uses_codex_backend_without_openrouter(self):
        args = argparse.Namespace(text_model=None, max_tempo=1.15)
        segments = [youtube_dub.Segment(0, 0.0, 2.0, "这是一句很长的中文。")]
        result_value = {"segments": [{"id": 0, "zh": "这句话很长。"}]}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            youtube_dub, "codex_json_completion", return_value=result_value
        ), mock.patch.object(
            youtube_dub,
            "openrouter_request",
            side_effect=AssertionError("text work must not call OpenRouter"),
        ):
            rewritten, records = youtube_dub.shorten_translations_for_timing(
                args,
                Path(folder),
                segments,
                {0: 3.0},
                1,
            )

        self.assertEqual(rewritten[0], "这句话很长。")
        self.assertEqual(len(records), 1)

    def test_timing_shortening_accepts_ellipsis(self):
        args = argparse.Namespace(text_model=None, max_tempo=1.15)
        segments = [youtube_dub.Segment(0, 0.0, 2.0, "这是一句很长的中文。")]
        result_value = {"segments": [{"id": 0, "zh": "长话短说……"}]}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            youtube_dub, "codex_json_completion", return_value=result_value
        ):
            rewritten, _ = youtube_dub.shorten_translations_for_timing(
                args,
                Path(folder),
                segments,
                {0: 3.0},
                1,
            )

        self.assertEqual(rewritten[0], "长话短说……")


if __name__ == "__main__":
    unittest.main()
