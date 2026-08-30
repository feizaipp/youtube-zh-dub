#!/usr/bin/env python3
"""Run one resumable batch of source-voice CosyVoice3 synthesis."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cosyvoice-root", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("--threads must be at least 1")

    sys.path.insert(0, str(args.cosyvoice_root / "third_party" / "Matcha-TTS"))
    sys.path.insert(0, str(args.cosyvoice_root))
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/matplotlib-cosyvoice")
    os.environ.setdefault("MALLOC_ARENA_MAX", "2")
    os.environ.setdefault("OMP_NUM_THREADS", str(args.threads))
    os.environ.setdefault("MKL_NUM_THREADS", str(args.threads))
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

    import torch
    import torchaudio
    from cosyvoice.cli.cosyvoice import AutoModel

    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    jobs = json.loads(args.jobs.read_text(encoding="utf-8"))
    if not isinstance(jobs, list) or not jobs:
        raise RuntimeError("CosyVoice job document must be a non-empty list")

    print(f"[cosyvoice3] loading model {args.model}", flush=True)
    model = AutoModel(
        model_dir=str(args.model), load_trt=False, load_vllm=False, fp16=False
    )
    for position, job in enumerate(jobs, 1):
        output = Path(job["output"])
        if output.exists() and output.stat().st_size > 44:
            print(f"[cosyvoice3] cached {position}/{len(jobs)}: {output.name}", flush=True)
            continue
        synthesis_text = "You are a helpful assistant.<|endofprompt|>" + str(job["text"])
        chunks = []
        with torch.inference_mode():
            for result in model.inference_cross_lingual(
                synthesis_text, str(job["reference_audio"]), stream=False
            ):
                chunks.append(result["tts_speech"].cpu())
        if not chunks:
            raise RuntimeError(f"CosyVoice returned no audio for segment {job['id']}")
        speech = torch.cat(chunks, dim=1)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".tmp")
        torchaudio.save(str(temporary), speech, model.sample_rate, format="wav")
        temporary.replace(output)
        print(f"[cosyvoice3] wrote {position}/{len(jobs)}: {output.name}", flush=True)


if __name__ == "__main__":
    main()
