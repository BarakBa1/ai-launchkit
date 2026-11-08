# MiniMax v2 adapter patch (n8n 1.123.27)

What: offline, hash-guarded patch of `LmChatAnthropic.node.js`.
Input: adaptive-timeout postimage, SHA256 `7ab4b90e...`. Output: v2, SHA256 `e51c89ae...` (12348 bytes).

Changes:
- `MiniMax-M3` checks widened to any `MiniMax-M*` model: adaptive thinking, default max_tokens, 900 s timeout.
- `MINIMAX_DEFAULT_MAX_TOKENS = 48000` names the default used when `maxTokensToSample` is -1 (was inline 32768).
- Thinking requests add `output_config.effort` for non-M3 `MiniMax-M*` models: `high` (budget <= 12000), `xhigh` (<= 24000), else `max`.

Why: other MiniMax-M* models get the adaptive path; 48000 leaves room for effort `max` to think past 32k.

Files:
- `minimax_v2_patch.py`: stdlib patcher. `patch <in> <out>` (exclusive create), `verify <file>`.
- `minimax_v2_patch.sha256`: checksum of the patcher (checked at image build).
- `LmChatAnthropic.node.js.orig`: input, copied from `ai4trade-n8n:1.123.27-minimax-adaptive-20261030-main-7ab4b90e`.
- `Dockerfile`: build recipe, mirrors `Dockerfile.minimax-timeout-derivative`. VPS build context: `/root/ai-launchkit/temp/minimax-v2-20261108/`.

Built (not running, compose untouched):
- `ai4trade-n8n:1.123.27-minimax-v2-20261108-main`
- `ai4trade-n8n:1.123.27-minimax-v2-20261108-worker`

Caveat: every MiniMax-M* model with thinking enabled now uses adaptive thinking and the 48000 default. Confirm which MiniMax models the workflows use before switching compose to these tags.

Rollback: point the service image back to the previous tags (`ai4trade-n8n:1.123.27-minimax-adaptive-20261030-{main,worker}-7ab4b90e`). Those images are untouched; keep the v2 images until rollback is no longer needed.
