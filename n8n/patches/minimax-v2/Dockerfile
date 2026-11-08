ARG BASE_IMAGE
FROM ${BASE_IMAGE}

USER root

RUN test ! -e /tmp/ai4trade-minimax-v2
COPY minimax_v2_patch.py minimax_v2_patch.sha256 /tmp/ai4trade-minimax-v2/
RUN set -eu; \
    cd /tmp/ai4trade-minimax-v2; \
    sha256sum -c minimax_v2_patch.sha256; \
    test "$(node -p 'require("/usr/local/lib/node_modules/n8n/package.json").version')" = "1.123.27"; \
    target="/usr/local/lib/node_modules/n8n/node_modules/@n8n/n8n-nodes-langchain/dist/nodes/llms/LMChatAnthropic/LmChatAnthropic.node.js"; \
    staged="${target}.minimax-v2.new"; \
    test ! -e "$staged"; \
    trap 'rm -f "$staged"; rm -rf /tmp/ai4trade-minimax-v2' EXIT; \
    python3 /tmp/ai4trade-minimax-v2/minimax_v2_patch.py patch "$target" "$staged"; \
    TARGET="$target" STAGED="$staged" python3 -c 'import os, stat; source=os.stat(os.environ["TARGET"]); os.chown(os.environ["STAGED"], source.st_uid, source.st_gid); os.chmod(os.environ["STAGED"], stat.S_IMODE(source.st_mode))'; \
    mv "$staged" "$target"; \
    node --check "$target"; \
    python3 /tmp/ai4trade-minimax-v2/minimax_v2_patch.py verify "$target"

USER node
WORKDIR /data
