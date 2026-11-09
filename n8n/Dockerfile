FROM n8nio/n8n:1
# FROM n8nio/n8n:latest
USER root

# === RESTORE apk-tools (REQUIRED since n8n 2.1.0+ hardened image) ===
RUN ARCH=$(uname -m) && \
    wget -qO- "http://dl-cdn.alpinelinux.org/alpine/latest-stable/main/${ARCH}/" | \
    grep -o 'href="apk-tools-static-[^"]*\.apk"' | head -1 | cut -d'"' -f2 | \
    xargs -I {} wget -q "http://dl-cdn.alpinelinux.org/alpine/latest-stable/main/${ARCH}/{}" && \
    tar -xzf apk-tools-static-*.apk && \
    ./sbin/apk.static -X http://dl-cdn.alpinelinux.org/alpine/latest-stable/main \
        -U --allow-untrusted add apk-tools && \
    rm -rf sbin apk-tools-static-*.apk

# Install ffmpeg and enhanced media processing tools + nodejs & npm for n8n community nodes
RUN apk add --no-cache \
    ffmpeg \
    imagemagick \
    exiftool \
    mediainfo \
    sox \
    ghostscript \
    python3 \
    py3-pip \
    nodejs \ 
    npm \
    curl \
    && rm -rf /var/cache/apk/*

# Install Python libraries for advanced media processing
RUN pip3 install --no-cache-dir --break-system-packages \
    pydub \
    Pillow \
    && rm -rf /root/.cache

# Create media directories with proper permissions for node user
# UID 1000 is the node user in the n8n container
RUN mkdir -p /data/media /data/temp && \
    chown -R 1000:1000 /data/media /data/temp && \
    chmod 775 /data/media /data/temp

# === AI4TRADE MiniMax adapter (matches the VPS image ai4trade-n8n:1.123.27-minimax-v2-20261108) ===
# 1. Install the reviewed "adaptive" LmChatAnthropic adapter (SHA256 7ab4b90e..., includes the 900 s timeout).
# 2. Apply the v2 patch: any MiniMax-M* model, 48000 default max tokens, output_config.effort. Result SHA256 e51c89ae...
# Details: patches/minimax-v2/README.md. n8n must stay 1.123.27 for the hashes to hold.
RUN test ! -e /tmp/ai4trade-minimax-v2
COPY patches/minimax-v2/LmChatAnthropic.node.js.orig patches/minimax-v2/minimax_v2_patch.py patches/minimax-v2/minimax_v2_patch.sha256 /tmp/ai4trade-minimax-v2/
RUN set -eu; \
    cd /tmp/ai4trade-minimax-v2; \
    sha256sum -c minimax_v2_patch.sha256; \
    echo "7ab4b90eb103ee4299f87ba33f46548f70d0226c2dcdfa39e30c4e3a1710f541  LmChatAnthropic.node.js.orig" | sha256sum -c -; \
    test "$(node -p 'require("/usr/local/lib/node_modules/n8n/package.json").version')" = "1.123.27"; \
    target="/usr/local/lib/node_modules/n8n/node_modules/@n8n/n8n-nodes-langchain/dist/nodes/llms/LMChatAnthropic/LmChatAnthropic.node.js"; \
    test -f "$target"; \
    cat LmChatAnthropic.node.js.orig > "$target"; \
    staged="${target}.minimax-v2.new"; \
    test ! -e "$staged"; \
    trap 'rm -f "$staged"; rm -rf /tmp/ai4trade-minimax-v2' EXIT; \
    python3 /tmp/ai4trade-minimax-v2/minimax_v2_patch.py patch "$target" "$staged"; \
    TARGET="$target" STAGED="$staged" python3 -c 'import os, stat; source=os.stat(os.environ["TARGET"]); os.chown(os.environ["STAGED"], source.st_uid, source.st_gid); os.chmod(os.environ["STAGED"], stat.S_IMODE(source.st_mode))'; \
    mv "$staged" "$target"; \
    node --check "$target"; \
    python3 /tmp/ai4trade-minimax-v2/minimax_v2_patch.py verify "$target"; \
    echo "e51c89ae" | grep -q "^$(sha256sum "$target" | cut -c1-8)$"

USER node
WORKDIR /data
