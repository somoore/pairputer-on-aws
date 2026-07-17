# Security-sensitive third-party artifacts are version and digest pinned below.
FROM public.ecr.aws/lambda/microvms:al2023-minimal@sha256:05cb9b38d841e7ff1b693dc9e894909612f340bf99ec97d426e8000a5bbe96c3

# BtbN autobuild release tags are PRUNED after ~2 weeks (a stale pin 404s and fails any cold-cache
# image build — proven live 2026-07-11). The exact asset is mirrored to the public pairputer launch
# bucket: immutable, still sha256-verified below. Upstream: BtbN/FFmpeg-Builds
# autobuild-2026-07-11-13-13 (GPL, linuxarm64).
ARG FFMPEG_URL=https://pairputer-launch-932930471665.s3.amazonaws.com/third-party/ffmpeg/ffmpeg-n8.1.2-22-g94138f6973-linuxarm64-gpl-8.1.tar.xz
ARG FFMPEG_SHA256=0ea95cba11d0949bff8037ab4701770267814afe0be29e96cce2487151a1257e
ARG NOVNC_VERSION=1.5.0
ARG NOVNC_SHA256=6a73e41f98388a5348b7902f54b02d177cb73b7e5eb0a7a0dcf688cc2c79b42a
# Community portable ARM64 build published by ungoogled-software contributor clickot.
ARG CHROMIUM_VERSION=150.0.7871.100-1
ARG CHROMIUM_ASSET=ungoogled-chromium-150.0.7871.100-1-arm64_linux.tar.xz
ARG CHROMIUM_SHA256=26e2e66c8c5d94bb02c98d028b03cd80a4dace9284710c984efd7188e78a61b4
ARG CHROMIUM_SANDBOX_ASSET=chromium-sandbox_150.0.7871.100-1~deb12u1_arm64.deb
ARG CHROMIUM_SANDBOX_SHA256=9fba404e26dfd6ec0c8fd6d16d04113caad7a49832e1d60c0e87c74afdc352d5
ARG GH_CLI_VERSION=2.94.0
ARG GH_CLI_SHA256=705a23b70b0f1b7ba4c302fdcef392ce3edaacfa7ce8e85e4d93d72ea800a538
ARG RIPGREP_VERSION=15.1.0
ARG RIPGREP_SHA256=2b661c6ef508e902f388e9098d9c4c5aca72c87b55922d94abdba830b4dc885e
ARG UV_VERSION=0.11.28
ARG UV_SHA256=03e9fe0a81b0718d0bc84625de3885df6cc3f89a8b6af6121d6b9f6113fb6533
ARG CODE_SERVER_VERSION=4.128.0
ARG CODE_SERVER_SHA256=f8f02c2a81d1a433a4d132716a6f0405f690f6d70dd955942e95e87356db8a10

RUN dnf install -y \
      tigervnc-server xorg-x11-xauth xterm xsetroot dbus-broker at-spi2-core python3-gobject \
      mutter gnome-text-editor nautilus gtk3 tmux \
      python3.11 python3.11-pip tar gzip xz which procps-ng shadow-utils util-linux \
      pulseaudio pulseaudio-utils pulseaudio-libs alsa-lib \
      libX11 libXext libXrender libXrandr libXcursor libXfixes libXi libXcomposite libXinerama \
      libxcb libxkbcommon libxkbcommon-x11 libXScrnSaver libXdamage \
      mesa-libGL mesa-libGLU mesa-dri-drivers mesa-libgbm nss nspr cups-libs \
      freetype fontconfig liberation-fonts xdg-utils desktop-file-utils \
      git jq gcc gcc-c++ make nodejs npm \
      vim-enhanced htop tree unzip zip less bash-completion wget openssh-clients \
      iptables-nft nftables \
    && dnf clean all

# ``gh`` is useful for an explicitly authorized developer workflow, but the
# image contains no GitHub credential and does not auto-start authentication.
# Pin the official immutable ARM64 release asset just like Chromium/FFmpeg.
RUN set -eux; cd /tmp; \
    curl -fsSL --retry 3 --max-time 120 -o gh.tar.gz \
      "https://github.com/cli/cli/releases/download/v${GH_CLI_VERSION}/gh_${GH_CLI_VERSION}_linux_arm64.tar.gz"; \
    printf '%s  %s\n' "$GH_CLI_SHA256" gh.tar.gz | sha256sum -c -; \
    tar -xzf gh.tar.gz; \
    install -o root -g root -m 0755 "gh_${GH_CLI_VERSION}_linux_arm64/bin/gh" /usr/local/bin/gh; \
    rm -rf gh.tar.gz "gh_${GH_CLI_VERSION}_linux_arm64"; \
    gh version | grep -F "gh version ${GH_CLI_VERSION}"

RUN set -eux; cd /tmp; \
    curl -fsSL --retry 3 --max-time 120 -o ripgrep.tar.gz \
      "https://github.com/BurntSushi/ripgrep/releases/download/${RIPGREP_VERSION}/ripgrep-${RIPGREP_VERSION}-aarch64-unknown-linux-gnu.tar.gz"; \
    printf '%s  %s\n' "$RIPGREP_SHA256" ripgrep.tar.gz | sha256sum -c -; \
    tar -xzf ripgrep.tar.gz; \
    install -o root -g root -m 0755 \
      "ripgrep-${RIPGREP_VERSION}-aarch64-unknown-linux-gnu/rg" /usr/local/bin/rg; \
    rm -rf ripgrep.tar.gz "ripgrep-${RIPGREP_VERSION}-aarch64-unknown-linux-gnu"; \
    rg --version | grep -F "ripgrep ${RIPGREP_VERSION}"

# uv: fast Python env/venv manager. A single static ARM64 binary — the sane way
# to make project envs in a disposable box without fighting system pip.
RUN set -eux; cd /tmp; \
    curl -fsSL --retry 3 --max-time 120 -o uv.tar.gz \
      "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-aarch64-unknown-linux-gnu.tar.gz"; \
    printf '%s  %s\n' "$UV_SHA256" uv.tar.gz | sha256sum -c -; \
    tar -xzf uv.tar.gz; \
    install -o root -g root -m 0755 uv-aarch64-unknown-linux-gnu/uv /usr/local/bin/uv; \
    install -o root -g root -m 0755 uv-aarch64-unknown-linux-gnu/uvx /usr/local/bin/uvx; \
    rm -rf uv.tar.gz uv-aarch64-unknown-linux-gnu; \
    uv --version | grep -F "${UV_VERSION}"

# code-server: VS Code in the browser (opened at a loopback preview port and
# reached through visible Chromium). Runs on demand, not at boot. It bundles its
# own Node, so it is heavy (~200 MB) but it is the single biggest dev-UX win and
# it composes with the existing Chromium stream instead of adding a desktop app.
RUN set -eux; cd /tmp; \
    curl -fsSL --retry 3 --max-time 300 -o code-server.tar.gz \
      "https://github.com/coder/code-server/releases/download/v${CODE_SERVER_VERSION}/code-server-${CODE_SERVER_VERSION}-linux-arm64.tar.gz"; \
    printf '%s  %s\n' "$CODE_SERVER_SHA256" code-server.tar.gz | sha256sum -c -; \
    mkdir -p /opt/code-server; \
    tar -xzf code-server.tar.gz -C /opt/code-server --strip-components=1; \
    rm code-server.tar.gz; \
    ln -s /opt/code-server/bin/code-server /usr/local/bin/code-server; \
    /opt/code-server/bin/code-server --version | grep -F "${CODE_SERVER_VERSION}"

COPY requirements.txt /tmp/requirements.txt
RUN python3.11 -m pip install --no-cache-dir --only-binary=:all: --no-deps --require-hashes -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt \
    && python3.11 -c 'import grpc,google.protobuf,websockets,Xlib,websocket; print("python runtime dependencies OK")'

RUN set -eux; cd /tmp; \
    curl -fsSL --retry 3 --max-time 300 -o ffmpeg.tar.xz "${FFMPEG_URL}"; \
    printf '%s  %s\n' "$FFMPEG_SHA256" ffmpeg.tar.xz | sha256sum -c -; \
    mkdir /opt/ffmpeg; tar -xJf ffmpeg.tar.xz -C /opt/ffmpeg --strip-components=1; \
    install -m 0755 /opt/ffmpeg/bin/ffmpeg /usr/local/bin/ffmpeg; rm -rf ffmpeg.tar.xz /opt/ffmpeg; \
    ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libopus; \
    ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libx264

RUN set -eux; cd /tmp; \
    curl -fsSL --retry 3 -o novnc.tar.gz "https://github.com/noVNC/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz"; \
    printf '%s  %s\n' "$NOVNC_SHA256" novnc.tar.gz | sha256sum -c -; \
    tar -xzf novnc.tar.gz -C /opt; ln -s "/opt/noVNC-${NOVNC_VERSION}" /opt/novnc; rm novnc.tar.gz; \
    mv /opt/novnc/vnc.html /opt/novnc/vnc_app.html; \
    printf '<!doctype html><meta charset=utf-8><script>location.replace("vnc_app.html?autoconnect=1&resize=scale&reconnect=1"+location.hash)</script>' > /opt/novnc/vnc.html; \
    printf '<!doctype html><meta charset=utf-8><script>location.replace("vnc.html")</script>' > /opt/novnc/index.html; \
    websockify --help >/dev/null

RUN set -eux; cd /tmp; \
    curl -fsSL --retry 3 --max-time 300 -o chromium.tar.xz \
      "https://github.com/ungoogled-software/ungoogled-chromium-portablelinux/releases/download/${CHROMIUM_VERSION}/${CHROMIUM_ASSET}"; \
    printf '%s  %s\n' "$CHROMIUM_SHA256" chromium.tar.xz | sha256sum -c -; \
    mkdir /opt/chromium; tar -xJf chromium.tar.xz -C /opt/chromium --strip-components=1; rm chromium.tar.xz; \
    curl -fsSL --retry 3 --max-time 120 -o sandbox.deb \
      "https://security.debian.org/debian-security/pool/updates/main/c/chromium/${CHROMIUM_SANDBOX_ASSET}"; \
    printf '%s  %s\n' "$CHROMIUM_SANDBOX_SHA256" sandbox.deb | sha256sum -c -; \
    mkdir sandbox; cd sandbox; ar x ../sandbox.deb; tar -xJf data.tar.xz ./usr/lib/chromium/chrome-sandbox; \
    install -o root -g root -m 4755 ./usr/lib/chromium/chrome-sandbox /opt/chromium/chrome-sandbox; \
    cd ..; rm -rf sandbox sandbox.deb; \
    test -x /opt/chromium/chrome; test "$(stat -c '%u:%g:%a' /opt/chromium/chrome-sandbox)" = '0:0:4755'; \
    /opt/chromium/chrome --version | grep -F 'Chromium 150.0.7871.100'

RUN groupadd -r pairputer-rpc \
    && groupadd -r pairputer-state \
    && groupadd -r pairputer-input \
    && groupadd -r pairputer-x11 \
    && groupadd -r egressd \
    && useradd -m -u 1000 -G pairputer-x11 app \
    && useradd -m -u 1001 -G app,pairputer-x11 terminal \
    && useradd -M -u 1002 -d /var/lib/pairputer-brain -s /sbin/nologin -G app,pairputer-rpc,pairputer-input,pairputer-x11 agent \
    && useradd -M -N -u 1003 -d /nonexistent -s /sbin/nologin -g pairputer-state -G pairputer-input,pairputer-x11 inputd \
    && useradd -M -N -u 1004 -d /nonexistent -s /sbin/nologin -g egressd egressd \
    && useradd -m -u 1005 -g app -s /bin/bash job \
    && install -d -o app -g app -m 0770 /home/app/workspace \
    && install -d -o app -g app -m 0770 /home/app/workspace/persistent \
    && printf '%s\n' \
       "This folder is DURABLE storage." \
       "" \
       "Files under workspace/persistent/ are mirrored to your private tenant prefix in S3" \
       "when the capsule is frozen or trashed, and restored into every fresh VM." \
       "Everything OUTSIDE this folder is ephemeral and dies with the VM." \
       "The VM itself holds no cloud credentials; the platform performs the sync." \
       > /home/app/workspace/persistent/README.txt \
    && chown app:app /home/app/workspace/persistent/README.txt \
    && chmod 0710 /home/app \
    && install -d -o app -g app -m 0700 /home/app/.config/chromium /home/app/.local/state/pairputer \
    && install -d -o terminal -g terminal -m 0700 /home/terminal \
    && install -d -o job -g app -m 0710 /home/job

# Homebrew, pre-baked (Linux ARM64 is Tier 1 with official bottles since brew 5.0, 2025-11).
# This is the escape hatch for AL2023's thin repos (no EPEL — it's why the image had no
# installable image viewer): `brew install <tool>` gives the human/AI the full formulae catalog
# whenever the internet toggle is open; air-gap simply blocks the download, nothing breaks.
# Pinned source tarball like every other artifact. `brew config` AS THE DESKTOP USER bootstraps
# portable-ruby into the image so first runtime use needs no bootstrap download; the prefix is
# owned by `app` so installs need no root. Installed packages are per-session (the prefix is
# outside workspace/persistent/ — deliberately: it would blow the durable mirror's file caps).
ARG HOMEBREW_VERSION=6.0.10
ARG HOMEBREW_SHA256=e97d273fb29d86726da5535c2eeee8d430dc2b3b4b7bffc7c41cfd82042363df
RUN set -eux; cd /tmp; \
    curl -fsSL --retry 3 --max-time 300 -o brew.tar.gz \
      "https://github.com/Homebrew/brew/archive/refs/tags/${HOMEBREW_VERSION}.tar.gz"; \
    printf '%s  %s\n' "$HOMEBREW_SHA256" brew.tar.gz | sha256sum -c -; \
    mkdir -p /home/linuxbrew/.linuxbrew/Homebrew /home/linuxbrew/.linuxbrew/bin; \
    tar -xzf brew.tar.gz -C /home/linuxbrew/.linuxbrew/Homebrew --strip-components=1; \
    rm brew.tar.gz; \
    ln -sf ../Homebrew/bin/brew /home/linuxbrew/.linuxbrew/bin/brew; \
    chown -R app:app /home/linuxbrew; \
    runuser -u app -- env HOME=/home/app HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_AUTO_UPDATE=1 \
      /home/linuxbrew/.linuxbrew/bin/brew --version | grep -F "${HOMEBREW_VERSION}"; \
    runuser -u app -- env HOME=/home/app HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_AUTO_UPDATE=1 \
      /home/linuxbrew/.linuxbrew/bin/brew config | grep -F "HOMEBREW_PREFIX: /home/linuxbrew/.linuxbrew"; \
    # Trim what the bootstrap cached but boot never needs — every image megabyte makes the
    # MicroVM's lazy-load first boot colder (the 5.1GB image missed the 600s ready hook).
    rm -rf /home/app/.cache/Homebrew /home/linuxbrew/.linuxbrew/Homebrew/docs \
           /home/linuxbrew/.linuxbrew/Homebrew/.github /home/linuxbrew/.linuxbrew/Homebrew/manpages

COPY proto/ /opt/capsule/proto/
COPY generate-proto.sh /opt/capsule/generate-proto.sh
RUN chmod 0755 /opt/capsule/generate-proto.sh \
    && /opt/capsule/generate-proto.sh /opt/capsule/desktopgen \
    && PYTHONPATH=/opt/capsule python3.11 -c 'from desktopgen.pairputer.desktop.v1 import desktop_pb2; print(desktop_pb2.DESCRIPTOR.package)' \
    && python3.11 -m pip uninstall -y grpcio-tools setuptools

COPY rootfs/ /
COPY workbench_eval_runner.py eval_gates.py workbench_eval_harness.py /opt/capsule/evals/
COPY eval-cases/ /opt/capsule/evals/eval-cases/
COPY fixtures/ /opt/capsule/evals/fixtures/
RUN find /opt/capsule -type f -name '*.sh' -exec chmod 0755 {} + \
    && chmod 0755 /opt/capsule/*.py /usr/local/bin/pairputer-chromium \
    && chown -R app:app /home/app \
    && python3.11 -m compileall -q /opt/capsule \
    # Rebuild the desktop-entry MIME cache so nautilus resolves pairputer-browser.desktop for
    # images/PDF/HTML (mimeapps.list defaults + "Open With" both need it). desktop-file-utils
    # ships update-desktop-database and is already a gtk/nautilus dependency chain member; fail
    # the BUILD if it's absent rather than shipping a desktop where double-click dead-ends.
    && update-desktop-database /usr/share/applications \
    && grep -q "pairputer-browser.desktop" /usr/share/applications/mimeinfo.cache

ENV PYTHONPATH=/opt/capsule:/opt/capsule/desktopgen \
    PATH=/home/linuxbrew/.linuxbrew/bin:/home/linuxbrew/.linuxbrew/sbin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ENV_HINTS=1 \
    PAIRPUTER_WORKSPACE=/home/app/workspace \
    PAIRPUTER_STATE_DIR=/home/app/.local/state/pairputer \
    PAIRPUTER_DESKTOP_BRAIN_DB=/var/lib/pairputer-brain/brain.sqlite3 \
    PAIRPUTER_DESKTOP_AGENT_KEY_FILE=/run/pairputer/desktop-agent.key \
    PAIRPUTER_BRIDGE_CAPABILITY_FILE=/run/pairputer/bridge-ingress.key \
    PAIRPUTER_REQUIRE_JOB_SANDBOX=true \
    PAIRPUTER_WORKBENCH_AUTONOMY=true \
    PAIRPUTER_BROWSER_DOMAIN_CEILING="*" \
    PAIRPUTER_ALLOW_LOCAL_PREVIEW=true \
    PAIRPUTER_PREVIEW_PORTS="3000-5899,7000-8999" \
    PAIRPUTER_BROWSER_REMOTE_PORTS="80,443" \
    PAIRPUTER_EGRESS_PROXY_HOST=127.0.0.1 \
    PAIRPUTER_EGRESS_PROXY_PORT=6907 \
    PAIRPUTER_CDP_ENDPOINT=http://127.0.0.1:9222 \
    PAIRPUTER_DISPLAY_WIDTH=1440 PAIRPUTER_DISPLAY_HEIGHT=900
EXPOSE 6901 6902 6903 6904 6905 6906 9000
CMD ["/opt/capsule/start.sh"]
