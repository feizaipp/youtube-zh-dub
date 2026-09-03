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

    def test_collapsed_rewritten_sentences_are_redistributed_on_word_boundaries(self):
        words = [
            youtube_dub.TimedWord(text, float(index), float(index) + 0.5)
            for index, text in enumerate("one two three four five six seven eight nine ten".split())
        ]
        segments = [
            youtube_dub.Segment(0, 0.0, 0.05, "A rewritten sentence."),
            youtube_dub.Segment(1, 0.05, 0.1, "Another rewritten sentence."),
            youtube_dub.Segment(2, 0.1, 4.5, "The following reliable sentence."),
            youtube_dub.Segment(3, 9.0, 9.5, "Anchor sentence."),
        ]
        result = youtube_dub.repair_collapsed_sentence_windows(
            segments,
            [youtube_dub.english_tokens(item.text) for item in segments],
            words,
        )
        self.assertTrue(all(item.duration >= 0.5 for item in result))
        real_starts = {item.start for item in words}
        real_ends = {item.end for item in words}
        self.assertTrue(all(item.start in real_starts for item in result))
        self.assertTrue(all(item.end in real_ends for item in result))
        self.assertTrue(all(a.end <= b.start for a, b in zip(result, result[1:])))

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
    def test_concat_audio_prepends_initial_timeline_silence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "spoken.wav"
            source.touch()

            def fake_silence(path, duration):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
                self.assertAlmostEqual(duration, 2.5)

            with mock.patch.object(youtube_dub, "make_silence", side_effect=fake_silence), mock.patch.object(
                youtube_dub, "run"
            ):
                youtube_dub.concat_audio(
                    [source], root / "output.wav", root, initial_silence=2.5
                )
            listing = (root / "segments" / "concat.txt").read_text()
            self.assertIn("spoken.wav", listing)
            self.assertTrue(listing.startswith("file '"))
            self.assertLess(listing.index("initial_silence.wav"), listing.index("spoken.wav"))

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
        self.assertEqual(args.transcriber_backend, "faster-whisper")
        self.assertEqual(args.transcriber_model, "medium.en")
        self.assertEqual(args.transcribe_workers, 1)
        self.assertEqual(args.tts_backend, "aliyun-cosyvoice")
        self.assertEqual(args.tts_workers, 4)
        self.assertEqual(args.fit_workers, 4)
        self.assertEqual(args.whisper_cpu_threads, min(6, os.cpu_count() or 1))
        self.assertEqual(args.video_preset, "fast")

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

    def test_transcription_records_non_speech_chunks_without_failing(self):
        args = argparse.Namespace(
            force=True,
            url="https://youtu.be/example",
            start_seconds=0.0,
            chunk_seconds=10.0,
            transcriber_model="openai/whisper-1",
            transcriber_backend="openrouter-whisper1",
            transcribe_workers=1,
        )

        def fake_extract(_source, destination, _start, _end):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(destination.stem.encode("ascii"))

        def fake_request(_endpoint, _api_key, payload, **_kwargs):
            name = base64.b64decode(payload["input_audio"]["data"]).decode("ascii")
            index = int(name.rsplit("_", 1)[1])
            if index == 1:
                return {"text": "", "words": []}
            return {
                "text": f"Sentence {index}.",
                "words": [{"word": "Sentence", "start": 0.1, "end": 0.5}],
            }

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

        self.assertEqual([segment.id for segment in result], [0, 2])
        self.assertEqual(document["non_speech_chunk_ids"], [1])
        self.assertTrue(document["complete"])

    def test_tts_preparation_runs_concurrently(self):
        args = argparse.Namespace(tts_workers=4, tts_backend="mai")
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
                args, Path("work"), segments, Path("raw"), Path("trimmed")
            )

        self.assertGreaterEqual(maximum_active, 2)
        self.assertEqual(sorted(prepared), [0, 1, 2, 3])

    def test_aliyun_cosyvoice_request_uses_fixed_voice_and_downloads_wav(self):
        args = argparse.Namespace(
            tts_backend="aliyun-cosyvoice",
            tts_model=youtube_dub.DEFAULT_TTS,
            voice="cosyvoice-v3.5-flash-demo-123456",
            tts_speed=1.0,
            dashscope_base_url="https://workspace.example/api/v1",
            aliyun_instruction="自然、清晰地表达。",
        )
        audio = b"RIFF" + b"audio" * 30
        response_document = {
            "request_id": "request-1",
            "output": {
                "audio": {"url": "https://result.example/audio.wav"}
            },
        }

        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return self.payload

        responses = [
            FakeResponse(json.dumps(response_document).encode("utf-8")),
            FakeResponse(audio),
        ]
        with mock.patch.dict(os.environ, {"DASHSCOPE_API_KEY": "secret"}), mock.patch.object(
            youtube_dub.urllib.request, "urlopen", side_effect=responses
        ) as urlopen:
            result = youtube_dub.aliyun_cosyvoice_request(args, "测试中文。")

        self.assertEqual(result, audio)
        request = urlopen.call_args_list[0].args[0]
        self.assertEqual(
            request.full_url,
            "https://workspace.example/api/v1/services/audio/tts/SpeechSynthesizer",
        )
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "cosyvoice-v3.5-flash")
        self.assertEqual(payload["input"]["voice"], args.voice)
        self.assertEqual(payload["input"]["sample_rate"], 24000)
        self.assertEqual(payload["input"]["instruction"], args.aliyun_instruction)

    def test_aliyun_tts_source_is_cached_as_wav(self):
        args = argparse.Namespace(
            tts_backend="aliyun-cosyvoice",
            tts_model=youtube_dub.DEFAULT_TTS,
            voice="cosyvoice-v3.5-flash-demo-123456",
            tts_speed=1.0,
            aliyun_instruction=None,
        )
        segment = youtube_dub.Segment(0, 0.0, 2.0, "测试中文。")
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            youtube_dub, "aliyun_cosyvoice_request", return_value=b"RIFF" + b"x" * 200
        ) as request, mock.patch.object(
            youtube_dub, "trim_tts_edge_silence"
        ) as trim, mock.patch.object(
            youtube_dub, "probe_duration", return_value=1.2
        ):
            root = Path(folder)
            raw_dir = root / "raw"
            trimmed_dir = root / "trimmed"
            raw_dir.mkdir()
            trimmed_dir.mkdir()

            def fake_trim(_source, destination):
                destination.write_bytes(b"trimmed")

            trim.side_effect = fake_trim
            prepared = youtube_dub.ensure_tts_source(
                args, segment, raw_dir, trimmed_dir
            )

        request.assert_called_once_with(args, segment.text)
        self.assertEqual(prepared[1], 1.2)
        self.assertEqual(prepared[0].suffix, ".wav")

    def test_cosyvoice_source_backend_batches_jobs_and_hashes_references(self):
        args = argparse.Namespace(
            tts_backend="cosyvoice3-source",
            tts_model="Fun-CosyVoice3-0.5B",
            voice="source",
            tts_speed=1.0,
            cosyvoice_threads=2,
        )
        segments = [youtube_dub.Segment(0, 1.0, 3.0, "测试中文。")]

        with tempfile.TemporaryDirectory() as folder:
            workdir = Path(folder)
            (workdir / "source_audio.wav").write_bytes(b"source audio")
            raw_dir = workdir / "segments" / "tts_raw"
            trimmed_dir = workdir / "segments" / "tts_trimmed"
            raw_dir.mkdir(parents=True)
            trimmed_dir.mkdir(parents=True)
            root = workdir / "cosyvoice"
            (root / "cosyvoice").mkdir(parents=True)
            (root / "third_party" / "Matcha-TTS").mkdir(parents=True)
            model = root / "model"
            model.mkdir()
            python = workdir / "python"
            python.write_text("", encoding="utf-8")
            worker = workdir / "worker.py"
            worker.write_text("", encoding="utf-8")

            def fake_extract(_source, destination, start, end):
                self.assertEqual((start, end), (1.0, 3.0))
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"reference voice")

            def fake_run(command, **_kwargs):
                jobs_path = Path(command[command.index("--jobs") + 1])
                jobs = youtube_dub.read_json(jobs_path)
                self.assertEqual(len(jobs), 1)
                self.assertEqual(jobs[0]["text"], "测试中文。")
                Path(jobs[0]["output"]).write_bytes(b"raw wav")

            def fake_trim(_source, destination):
                destination.write_bytes(b"trimmed wav")

            with mock.patch.object(
                youtube_dub, "cosyvoice_paths", return_value=(root, python, model, worker)
            ), mock.patch.object(
                youtube_dub, "extract_segment", side_effect=fake_extract
            ), mock.patch.object(
                youtube_dub, "run", side_effect=fake_run
            ), mock.patch.object(
                youtube_dub, "trim_tts_edge_silence", side_effect=fake_trim
            ), mock.patch.object(
                youtube_dub, "probe_duration", return_value=1.5
            ):
                prepared = youtube_dub.prepare_tts_sources(
                    args, workdir, segments, raw_dir, trimmed_dir
                )

            self.assertEqual(prepared[0][1], 1.5)
            self.assertTrue(prepared[0][0].exists())
            references = list(
                (workdir / "segments" / "source_voice_reference").glob("segment_0000_*.wav")
            )
            self.assertEqual(len(references), 1)

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


class BackgroundAudioTests(unittest.TestCase):
    def background_args(self, **overrides):
        values = {
            "force": False,
            "background_mode": "demucs",
            "demucs_model": "htdemucs",
            "demucs_device": "cpu",
            "demucs_jobs": 2,
            "background_volume": 0.7,
            "background_duck_ratio": 4.0,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_parser_enables_demucs_background_by_default(self):
        args = youtube_dub.build_parser().parse_args([])
        self.assertEqual(args.background_mode, "demucs")
        self.assertEqual(args.demucs_model, "htdemucs")
        self.assertEqual(args.demucs_jobs, 2)
        self.assertEqual(args.background_volume, 0.7)
        self.assertEqual(args.background_duck_ratio, 4.0)

    def test_background_source_prefers_matching_retained_audio(self):
        with tempfile.TemporaryDirectory() as folder:
            workdir = Path(folder)
            video = workdir / "source.mp4"
            retained = workdir / "source_audio_original.webm"
            video.write_bytes(b"video")
            retained.write_bytes(b"audio")
            with mock.patch.object(youtube_dub, "probe_duration", return_value=10.0):
                result = youtube_dub.background_source_audio(workdir, video)
        self.assertEqual(result, retained)

    def test_background_source_uses_trimmed_video_when_retained_audio_is_full(self):
        with tempfile.TemporaryDirectory() as folder:
            workdir = Path(folder)
            video = workdir / "source.mp4"
            retained = workdir / "raw_audio_original.webm"
            video.write_bytes(b"video")
            retained.write_bytes(b"audio")

            def duration(path):
                return 45.0 if path == video else 300.0

            with mock.patch.object(youtube_dub, "probe_duration", side_effect=duration):
                result = youtube_dub.background_source_audio(workdir, video)
        self.assertEqual(result, video)

    def test_demucs_background_is_cached_by_source_and_model(self):
        args = self.background_args()
        observed: list[list[str]] = []
        with tempfile.TemporaryDirectory() as folder:
            workdir = Path(folder)
            video = workdir / "source.mp4"
            retained = workdir / "source_audio_original.webm"
            video.write_bytes(b"video")
            retained.write_bytes(b"audio")

            def fake_run(command, **_kwargs):
                observed.append(list(command))
                output_root = Path(command[command.index("--out") + 1])
                stem = output_root / "htdemucs" / retained.stem / "no_vocals.wav"
                stem.parent.mkdir(parents=True)
                stem.write_bytes(b"background")
                return mock.Mock()

            with mock.patch.object(
                youtube_dub, "probe_duration", return_value=10.0
            ), mock.patch.object(
                youtube_dub.shutil, "which", return_value="/venv/bin/demucs"
            ), mock.patch.object(youtube_dub, "run", side_effect=fake_run):
                first = youtube_dub.separate_background_audio(args, workdir, video)
                second = youtube_dub.separate_background_audio(args, workdir, video)

            self.assertEqual(first, workdir / "background_audio.wav")
            self.assertEqual(second, first)
            self.assertEqual(len(observed), 1)
            self.assertIn("--two-stems", observed[0])
            self.assertIn("vocals", observed[0])
            self.assertIn("--device", observed[0])
            self.assertEqual(observed[0][observed[0].index("--jobs") + 1], "2")
            self.assertEqual(observed[0][-1], str(retained))
            metadata = youtube_dub.read_json(
                workdir / "segments" / "demucs_background.json"
            )
            self.assertTrue(metadata["complete"])
            self.assertEqual(metadata["request"]["model"], "htdemucs")

    def test_mix_uses_sidechain_ducking_and_is_cached(self):
        args = self.background_args()
        observed: list[list[str]] = []
        with tempfile.TemporaryDirectory() as folder:
            workdir = Path(folder)
            video = workdir / "source.mp4"
            background = workdir / "background_audio.wav"
            dub = workdir / "chinese_voice.wav"
            video.write_bytes(b"video")
            background.write_bytes(b"background")
            dub.write_bytes(b"dub")

            def fake_run(command, **_kwargs):
                observed.append(list(command))
                Path(command[-1]).write_bytes(b"mixed")
                return mock.Mock()

            with mock.patch.object(
                youtube_dub, "probe_duration", return_value=10.0
            ), mock.patch.object(youtube_dub, "run", side_effect=fake_run):
                first = youtube_dub.mix_background_with_dub(
                    args, workdir, video, background, dub
                )
                second = youtube_dub.mix_background_with_dub(
                    args, workdir, video, background, dub
                )

            self.assertEqual(first, workdir / "chinese_mix.wav")
            self.assertEqual(second, first)
            self.assertEqual(len(observed), 1)
            filter_graph = observed[0][observed[0].index("-filter_complex") + 1]
            self.assertIn("sidechaincompress=", filter_graph)
            self.assertIn("amix=inputs=2", filter_graph)
            self.assertIn("volume=0.700000", filter_graph)
            self.assertIn("ratio=4.000000", filter_graph)

    def test_none_mode_returns_unmixed_dub(self):
        args = self.background_args(background_mode="none")
        dub = Path("chinese_voice.wav")
        self.assertEqual(
            youtube_dub.prepare_final_audio(args, Path("work"), Path("video"), dub),
            dub,
        )

    def test_mux_does_not_truncate_video_at_last_subtitle(self):
        args = self.background_args(
            background_mode="none", force=True, remux_only=True
        )
        observed: list[str] = []
        with tempfile.TemporaryDirectory() as folder:
            workdir = Path(folder)
            video = workdir / "source.mp4"
            dub = workdir / "chinese_voice.wav"
            subtitle = workdir / "transcript.zh.srt"
            video.write_bytes(b"video")
            dub.write_bytes(b"dub")
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\n字幕\n", encoding="utf-8"
            )

            def fake_run(command, **_kwargs):
                observed.extend(command)
                Path(command[-1]).write_bytes(b"muxed")
                return mock.Mock()

            with mock.patch.object(
                youtube_dub, "prepare_final_audio", return_value=dub
            ), mock.patch.object(
                youtube_dub, "probe_video_codec", return_value="h264"
            ), mock.patch.object(
                youtube_dub, "run", side_effect=fake_run
            ), mock.patch.object(youtube_dub, "burn_hardsub_subtitles"):
                youtube_dub.mux_video(args, workdir, video, dub)

        self.assertNotIn("-shortest", observed)
        self.assertIn("title=中文配音", observed)


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

    def test_web_embedded_player_client_is_forwarded_to_ytdlp(self):
        args = argparse.Namespace(
            cookies_from_browser=None,
            proxy=None,
            youtube_player_client="web_embedded",
        )
        with mock.patch.object(youtube_dub.shutil, "which", return_value=None):
            command = youtube_dub.yt_dlp_common(args)
        self.assertEqual(
            command[command.index("--extractor-args") + 1],
            "youtube:player_client=web_embedded",
        )

    def test_cookie_file_is_forwarded_to_ytdlp(self):
        args = argparse.Namespace(
            cookies_from_browser=None,
            cookies=Path("/root/youtube-cookies.txt"),
            proxy=None,
            youtube_player_client="web_embedded",
        )
        with mock.patch.object(youtube_dub.shutil, "which", return_value=None):
            command = youtube_dub.yt_dlp_common(args)
        self.assertEqual(
            command[command.index("--cookies") + 1],
            "/root/youtube-cookies.txt",
        )

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
        options = youtube_dub.quicktime_video_options("av1", "fast")
        self.assertIn("libx264", options)
        self.assertIn("yuv420p", options)
        self.assertIn("avc1", options)
        self.assertEqual(options[options.index("-preset") + 1], "fast")

    def test_video_preset_can_be_overridden(self):
        options = youtube_dub.quicktime_video_options("av1", "medium")
        self.assertEqual(options[options.index("-preset") + 1], "medium")

    def test_video_preset_cache_requires_matching_transcode_preset(self):
        self.assertTrue(youtube_dub.video_preset_cache_matches("h264", {}, "fast"))
        self.assertTrue(youtube_dub.video_preset_cache_matches("av1", {}, "medium"))
        self.assertFalse(youtube_dub.video_preset_cache_matches("av1", {}, "fast"))
        self.assertTrue(
            youtube_dub.video_preset_cache_matches(
                "av1", {"video_preset": "fast"}, "fast"
            )
        )

    def test_subtitles_only_forwards_video_preset(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            youtube_dub, "require_tools"
        ), mock.patch.object(
            youtube_dub,
            "embed_subtitles_only",
            return_value=Path(folder) / "dubbed.zh.mp4",
        ), mock.patch.object(
            youtube_dub,
            "burn_hardsub_subtitles",
            return_value=Path(folder) / "dubbed.zh.hardsub.mp4",
        ) as burn:
            result = youtube_dub.main(
                [
                    "--subtitles-only",
                    "--workdir",
                    folder,
                    "--video-preset",
                    "medium",
                ]
            )

        self.assertEqual(result, 0)
        self.assertEqual(burn.call_args.args[3], "medium")

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

    def test_hardsub_output_burns_subtitles_and_copies_audio(self):
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
                Path(command[-1]).write_bytes(b"hardsub")
                return mock.Mock()

            with mock.patch.object(youtube_dub, "run", side_effect=fake_run):
                output = youtube_dub.burn_hardsub_subtitles(
                    workdir, source, subtitle
                )

            self.assertEqual(output.name, "dubbed.zh.hardsub.mp4")
            self.assertEqual(output.read_bytes(), b"hardsub")
            self.assertIn("-vf", observed)
            self.assertIn("subtitles=filename=", observed[observed.index("-vf") + 1])
            self.assertEqual(observed[observed.index("-c:a") + 1], "copy")
            self.assertIn("-sn", observed)
            self.assertIn("libx264", observed)
            self.assertEqual(observed[observed.index("-preset") + 1], "fast")

    def test_hardsub_output_is_reused_when_inputs_are_unchanged(self):
        with tempfile.TemporaryDirectory() as folder:
            workdir = Path(folder)
            source = workdir / "dubbed.zh.mp4"
            subtitle = workdir / "transcript.zh.srt"
            output = workdir / "dubbed.zh.hardsub.mp4"
            source.write_bytes(b"video")
            subtitle.write_text("subtitle", encoding="utf-8")
            output.write_bytes(b"existing")
            os.utime(output, (output.stat().st_atime, max(source.stat().st_mtime, subtitle.stat().st_mtime) + 1))
            youtube_dub.write_json(
                workdir / "segments" / "hardsub_render.json",
                {
                    "render_version": youtube_dub.HARDSUB_RENDER_VERSION,
                    "source_mtime_ns": source.stat().st_mtime_ns,
                    "subtitle_mtime_ns": subtitle.stat().st_mtime_ns,
                    "font": "Noto Sans CJK SC",
                    "video_preset": "fast",
                },
            )

            with mock.patch.object(youtube_dub, "run") as mocked_run:
                result = youtube_dub.burn_hardsub_subtitles(
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


class CallingAgentTextBackendTests(unittest.TestCase):
    def test_pipeline_does_not_require_a_vendor_model_cli(self):
        requested: list[str] = []

        def find_tool(name: str) -> str:
            requested.append(name)
            return f"/usr/bin/{name}"

        with mock.patch.object(youtube_dub.shutil, "which", side_effect=find_tool):
            youtube_dub.require_tools(argparse.Namespace())

        self.assertEqual(requested, ["yt-dlp", "ffmpeg", "ffprobe"])

    def test_agent_request_is_resumable_and_checks_its_fingerprint(self):
        args = argparse.Namespace(text_model=None)
        schema = youtube_dub.segment_translation_schema()
        with tempfile.TemporaryDirectory() as folder:
            workdir = Path(folder)
            with self.assertRaisesRegex(youtube_dub.PipelineError, "当前后端模型"):
                youtube_dub.agent_json_completion(args, workdir, "Translate.", "Hello.", schema)
            request_path = next((workdir / "segments" / "agent_text_requests").glob("*.request.json"))
            request = youtube_dub.read_json(request_path)
            response_path = Path(request["response_path"])
            youtube_dub.write_json(
                response_path,
                {
                    "request_fingerprint": request["request_fingerprint"],
                    "result": {"segments": [{"id": 0, "zh": "你好。"}]},
                },
            )
            result = youtube_dub.agent_json_completion(args, workdir, "Translate.", "Hello.", schema)

        self.assertEqual(result["segments"][0]["zh"], "你好。")

    def test_polish_uses_calling_agent_backend_without_openrouter(self):
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
            youtube_dub, "agent_json_completion", return_value=result_value
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
        self.assertEqual(document["text_backend"], "calling-agent")

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
            youtube_dub, "agent_json_completion", return_value=result_value
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
        self.assertEqual(batch_document["text_backend"], "calling-agent")
        self.assertIn("request_fingerprint", batch_document)

    def test_translation_uses_calling_agent_backend_without_openrouter(self):
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
            youtube_dub, "agent_json_completion", return_value=result_value
        ), mock.patch.object(
            youtube_dub,
            "openrouter_request",
            side_effect=AssertionError("text work must not call OpenRouter"),
        ):
            translated = youtube_dub.translate_segments(args, Path(folder), english)
            document = youtube_dub.read_json(Path(folder) / "transcript.zh.json")

        self.assertEqual(translated[0].text, "大家好。")
        self.assertEqual(document["text_backend"], "calling-agent")

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
            youtube_dub, "agent_json_completion", side_effect=results
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

    def test_timing_shortening_uses_calling_agent_backend_without_openrouter(self):
        args = argparse.Namespace(text_model=None, max_tempo=1.15)
        segments = [youtube_dub.Segment(0, 0.0, 2.0, "这是一句很长的中文。")]
        result_value = {"segments": [{"id": 0, "zh": "这句话很长。"}]}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(
            youtube_dub, "agent_json_completion", return_value=result_value
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
            youtube_dub, "agent_json_completion", return_value=result_value
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
