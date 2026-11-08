#!/usr/bin/env python3
"""Offline, hash-guarded v2 patch for n8n 1.123.27 LmChatAnthropic (MiniMax).

Input is the adaptive-timeout postimage (SHA256 7ab4b90e...), i.e. the file the
running ai4trade-n8n main and worker images already contain. Output widens the
MiniMax-M* family handling, names the default max_tokens constant, and adds an
effort hint to thinking requests. Every anchor must occur exactly once in the
text being patched. Stdlib only: no network, no packages, no in-place edits.

Usage:
  minimax_v2_patch.py patch <input.js> <output.js>   (output must not exist)
  minimax_v2_patch.py verify <file.js>
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

INPUT_SHA256 = "7ab4b90eb103ee4299f87ba33f46548f70d0226c2dcdfa39e30c4e3a1710f541"
INPUT_SIZE = 11902
OUTPUT_SHA256 = "e51c89ae1fd2bc9dea4c08912b79b2a7d695bd6eb3196f231f0e6f856aee51ed"
OUTPUT_SIZE = 12348


class UnsupportedAdapterSource(ValueError):
    """Raised when the input or an anchor does not match the reviewed source."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# (label, old, new), applied in order. Each old must occur exactly once.
REPLACEMENTS: tuple[tuple[str, bytes, bytes], ...] = (
    (
        "timeout",
        b"      ...(modelName === 'MiniMax-M3' ? { timeout: 900_000 } : {}),\n",
        b"      ...(String(modelName).startsWith('MiniMax-M') ? { timeout: 900_000 } : {}),\n",
    ),
    (
        "family-flag",
        b"    const isMiniMaxM3 = modelName === 'MiniMax-M3';\n",
        b"    const isMiniMaxM3 = String(modelName).startsWith('MiniMax-M');\n"
        b"    const miniMaxBudget = Number(options.thinkingBudget ?? -1);\n"
        b"    const miniMaxEffort = !isMiniMaxM3 || modelName === 'MiniMax-M3' ? undefined : (miniMaxBudget > 0 && miniMaxBudget <= 12000 ? 'high' : (miniMaxBudget > 0 && miniMaxBudget <= 24000 ? 'xhigh' : 'max'));\n",
    ),
    (
        "default-max-tokens",
        b"    const maxTokensToSample = isMiniMaxM3 && options.maxTokensToSample === -1 ? 32768 : options.maxTokensToSample;\n",
        b"    const MINIMAX_DEFAULT_MAX_TOKENS = 48000;\n"
        b"    const maxTokensToSample = isMiniMaxM3 && options.maxTokensToSample === -1 ? MINIMAX_DEFAULT_MAX_TOKENS : options.maxTokensToSample;\n",
    ),
    (
        "effort-in-invocation-kwargs",
        b"            budget_tokens: options.thinkingBudget ?? MIN_THINKING_BUDGET\n          },\n",
        b"            budget_tokens: options.thinkingBudget ?? MIN_THINKING_BUDGET\n          },\n"
        b"        ...(miniMaxEffort ? { output_config: { effort: miniMaxEffort } } : {}),\n",
    ),
)


def build_patched(source: bytes) -> bytes:
    """Return the v2 patched adapter bytes or raise on any unexpected input."""
    if not isinstance(source, bytes):
        raise TypeError("source must be bytes")
    if len(source) != INPUT_SIZE or _sha256(source) != INPUT_SHA256:
        raise UnsupportedAdapterSource("input does not match the reviewed adaptive postimage")
    patched = source
    for label, old, new in REPLACEMENTS:
        found = patched.count(old)
        if found != 1:
            raise UnsupportedAdapterSource(f"{label}: expected exactly one anchor, found {found}")
        patched = patched.replace(old, new, 1)
    return patched


def cmd_patch(input_path: Path, output_path: Path) -> None:
    source = input_path.read_bytes()
    patched = build_patched(source)
    with output_path.open("xb") as handle:
        handle.write(patched)
    print(f"input_sha256={_sha256(source)}")
    print(f"output_sha256={_sha256(patched)} output_size={len(patched)}")


def cmd_verify(path: Path) -> None:
    data = path.read_bytes()
    if len(data) != OUTPUT_SIZE or _sha256(data) != OUTPUT_SHA256:
        raise UnsupportedAdapterSource(f"{path} does not match the reviewed v2 postimage")
    print(f"verified output_sha256={OUTPUT_SHA256} output_size={OUTPUT_SIZE}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_patch = sub.add_parser("patch")
    p_patch.add_argument("input", type=Path)
    p_patch.add_argument("output", type=Path)
    p_verify = sub.add_parser("verify")
    p_verify.add_argument("path", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "patch":
            cmd_patch(args.input, args.output)
        else:
            cmd_verify(args.path)
    except (UnsupportedAdapterSource, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
