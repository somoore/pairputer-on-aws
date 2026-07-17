# pairputer capsule: agent-doom — RESTful-DOOM (somoore/restful-doom) streamed to the browser
# for the HUMAN (same Xvnc/:6903/:6904 contract as hellbox-doom) PLUS an in-process
# protobuf/gRPC DoomAgent service for the AGENT, fronted by agent_bridge.py on :6905.
# One process, one live game, two interfaces. See capsule.yaml (the capability manifest)
# and dream.md / interaction.md for the model.
#
# NOTE: the AWS::Lambda::MicrovmImage build derives its base from the template's BaseImageArn
# (AWS-managed, versioned: aws:microvm-image:al2023-1) — that is the effective, AWS-controlled pin for
# the MicroVM build. This FROM is for LOCAL/standalone container builds only (same policy as hellbox-doom).
FROM public.ecr.aws/lambda/microvms:al2023-minimal

# BtbN autobuild release tags are PRUNED after ~2 weeks (a stale pin 404s and fails any cold-cache
# image build — proven live on computer-use-desktop 2026-07-11; only warm layer caches masked it
# here). The exact asset is mirrored to the public pairputer launch bucket: immutable, still
# sha256-verified below. Upstream: BtbN/FFmpeg-Builds autobuild-2026-07-11-13-13 (GPL, linuxarm64).
ARG FFMPEG_URL=https://pairputer-launch-932930471665.s3.amazonaws.com/third-party/ffmpeg/ffmpeg-n8.1.2-22-g94138f6973-linuxarm64-gpl-8.1.tar.xz
ARG FFMPEG_SHA256=0ea95cba11d0949bff8037ab4701770267814afe0be29e96cce2487151a1257e
ARG NOVNC_VERSION=1.5.0
ARG NOVNC_SHA256=6a73e41f98388a5348b7902f54b02d177cb73b7e5eb0a7a0dcf688cc2c79b42a
ARG SDL2_VERSION=2.30.9
ARG SDL2_SHA256=24b574f71c87a763f50704bbb630cbe38298d544a1f890f099a4696b1d6beba4
ARG SDL2_MIXER_VERSION=2.8.0
ARG SDL2_MIXER_SHA256=1cfb34c87b26dbdbc7afd68c4f545c0116ab5f90bbfecc5aebe2a9cb4bb31549
ARG SDL2_NET_VERSION=2.2.0
ARG SDL2_NET_SHA256=4e4a891988316271974ff4e9585ed1ef729a123d22c08bd473129179dc857feb
# The restful-doom fork, pinned by commit (the fetch is verified against this exact tree).
ARG RESTFUL_DOOM_REPO=somoore/restful-doom
ARG RESTFUL_DOOM_COMMIT=518d7dea75a3b69e2bbc54b5687ab4e62dac3bd0
# gRPC client deps for the bridge (version-pinned).
# TODO(hash-pin): add sha256 hashes like requirements.txt before graduating to pairputer-platform.
ARG GRPCIO_VERSION=1.66.2
ARG PROTOBUF_VERSION=5.28.2

# Display, audio, build/runtime deps (same set as hellbox-doom).
RUN dnf install -y tigervnc-server xterm xsetroot python3.11 python3.11-pip tar gzip xz binutils which procps-ng \
        shadow-utils pulseaudio pulseaudio-utils \
    && dnf clean all
RUN dnf install -y glib2 libX11 libXext libXrender libXrandr libXcursor libXfixes \
        libXi libXcomposite libXinerama libxcb libxkbcommon libxkbcommon-x11 \
        libXScrnSaver freetype fontconfig libjpeg-turbo \
    && dnf clean all
RUN dnf install -y alsa-lib libusbx systemd-libs gnutls mesa-libGL mesa-libGLU \
        mesa-dri-drivers pulseaudio-libs ncurses-libs krb5-libs || true
COPY requirements.txt /tmp/capsule-requirements.txt
RUN python3.11 -m pip install --no-cache-dir --only-binary=:all: --require-hashes \
        -r /tmp/capsule-requirements.txt \
    && rm -f /tmp/capsule-requirements.txt
# gRPC client for the agent bridge (separate invocation: version-pinned, not yet hash-pinned).
RUN python3.11 -m pip install --no-cache-dir --only-binary=:all: \
        "grpcio==${GRPCIO_VERSION}" "protobuf==${PROTOBUF_VERSION}"

# Pinned ffmpeg build with libopus (video encode + the bridge's x11grab read_screen).
RUN set -e; \
    cd /tmp; \
    curl -fsSL --retry 3 --max-time 240 -o btbn.tar.xz "${FFMPEG_URL}"; \
    printf '%s  %s\n' "$FFMPEG_SHA256" btbn.tar.xz | sha256sum -c -; \
    mkdir -p /opt/ff && tar -xJf btbn.tar.xz -C /opt/ff --strip-components=1; \
    install -m 0755 /opt/ff/bin/ffmpeg /usr/local/bin/ffmpeg; \
    rm -rf /tmp/btbn.tar.xz /opt/ff; \
    ffmpeg -version | head -1; \
    ffmpeg -hide_banner -encoders 2>/dev/null | grep -q libopus || { echo "FATAL: ffmpeg lacks libopus encoder"; exit 1; }

# Non-root app user shares the PulseAudio session with the game.
RUN useradd -m -u 1000 app

# noVNC web client.
RUN set -e; \
    curl -fsSL --retry 3 -o /tmp/novnc.tgz \
      "https://github.com/novnc/noVNC/archive/refs/tags/v${NOVNC_VERSION}.tar.gz"; \
    printf '%s  %s\n' "$NOVNC_SHA256" /tmp/novnc.tgz | sha256sum -c -; \
    tar -xzf /tmp/novnc.tgz -C /opt; \
    ln -s "/opt/noVNC-${NOVNC_VERSION}" /opt/novnc; \
    rm -f /tmp/novnc.tgz; \
    # Ensure the viewer SCALES the small 320x200 frame to fill the window. The ?resize=scale URL param is
    # what reliably applies noVNC's scaling (the ui.js default setting does not). So make the bare
    # /vnc.html and index redirect to the scaling URL — anyone opening :6901 gets a filled view, and the
    # Codex widget's viewerUrl already carries the param. (Verified: ?resize=scale -> canvas fills window.)
    mv /opt/novnc/vnc.html /opt/novnc/vnc_app.html; \
    printf '<!doctype html><meta charset=utf-8><title>capsule</title><script>location.replace("vnc_app.html?autoconnect=1&resize=scale&reconnect=1"+location.hash)</script>' > /opt/novnc/vnc.html; \
    printf '<!doctype html><meta charset=utf-8><script>location.replace("vnc.html")</script>' > /opt/novnc/index.html

# Build toolchain: C for SDL/doom + Rust/protoc for the in-process gRPC bridge (doom-grpc-server).
RUN dnf install -y gcc make autoconf automake libtool pkgconfig \
        cargo rust protobuf-compiler protobuf-devel \
        libX11-devel libXext-devel pulseaudio-libs-devel alsa-lib-devel \
    && dnf clean all
RUN for p in libXcursor-devel libXi-devel libXrandr-devel libXfixes-devel \
        libXScrnSaver-devel libxkbcommon-devel; do \
      dnf install -y "$p" || echo "[build] optional dep $p unavailable, skipping"; \
    done; dnf clean all || true
ENV PKG_CONFIG_PATH=/usr/local/lib/pkgconfig:/usr/lib64/pkgconfig
ENV PYTHONPATH=/opt/capsule:/opt/capsule/rdgen
ENV RESTFULDOOM_PROTO_STUBS=/opt/capsule/rdgen

# SDL2: X11 video, PulseAudio/ALSA audio, no GL/Wayland/Vulkan (same flags as hellbox-doom).
WORKDIR /tmp/sdl
RUN curl -fsSL --retry 3 -o sdl.tgz "https://github.com/libsdl-org/SDL/releases/download/release-${SDL2_VERSION}/SDL2-${SDL2_VERSION}.tar.gz" \
    && printf '%s  %s\n' "$SDL2_SHA256" sdl.tgz | sha256sum -c - \
    && tar -xzf sdl.tgz --strip-components=1 \
    && ./configure --disable-video-wayland --disable-video-opengl \
        --disable-video-opengles --disable-video-vulkan \
        --enable-video-x11 --enable-pulseaudio --enable-alsa --disable-sdltest \
    && make -j"$(nproc)" && make install \
    && echo "/usr/local/lib" > /etc/ld.so.conf.d/local.conf && ldconfig \
    && rm -rf /tmp/sdl
WORKDIR /

# SDL2_mixer and SDL2_net against source SDL2.
WORKDIR /tmp/mixer
RUN curl -fsSL --retry 3 -o mixer.tgz "https://github.com/libsdl-org/SDL_mixer/releases/download/release-${SDL2_MIXER_VERSION}/SDL2_mixer-${SDL2_MIXER_VERSION}.tar.gz" \
    && printf '%s  %s\n' "$SDL2_MIXER_SHA256" mixer.tgz | sha256sum -c - \
    && tar -xzf mixer.tgz --strip-components=1 \
    && ./configure --disable-music-mod-mikmod --disable-music-midi-fluidsynth \
        --disable-music-ogg --disable-music-flac --disable-music-mp3-mpg123 \
        --disable-music-opus --disable-sdltest \
    && make -j"$(nproc)" && make install && ldconfig && rm -rf /tmp/mixer
WORKDIR /tmp/net
RUN curl -fsSL --retry 3 -o net.tgz "https://github.com/libsdl-org/SDL_net/releases/download/release-${SDL2_NET_VERSION}/SDL2_net-${SDL2_NET_VERSION}.tar.gz" \
    && printf '%s  %s\n' "$SDL2_NET_SHA256" net.tgz | sha256sum -c - \
    && tar -xzf net.tgz --strip-components=1 \
    && ./configure --disable-sdltest && make -j"$(nproc)" && make install && ldconfig \
    && rm -rf /tmp/net
WORKDIR /

# RESTful-DOOM (agent mode): Chocolate Doom lineage + in-process protobuf/gRPC DoomAgent service.
# Fetched at the pinned commit; the same tree provides proto/restfuldoom/v1/agent.proto, from which
# the bridge's Python stubs are generated below — engine and bridge cannot drift.
WORKDIR /tmp/rdoom
RUN curl -fsSL --retry 3 -o rdoom.tgz \
      "https://codeload.github.com/${RESTFUL_DOOM_REPO}/tar.gz/${RESTFUL_DOOM_COMMIT}" \
    && tar -xzf rdoom.tgz --strip-components=1 \
    && ACLOCAL_PATH=/usr/local/share/aclocal ./autogen.sh --disable-sdltest CFLAGS="-O2 -fcommon" \
    && make -j"$(nproc)" \
    && install -m 0755 src/restful-doom /usr/local/bin/restful-doom \
    && echo "native restful-doom at:" && ls -la /usr/local/bin/restful-doom \
    && mkdir -p /opt/capsule/rdgen \
    && python3.11 -m pip install --no-cache-dir "grpcio-tools==${GRPCIO_VERSION}" \
    && python3.11 -m grpc_tools.protoc -I proto \
         --python_out=/opt/capsule/rdgen --grpc_python_out=/opt/capsule/rdgen \
         proto/restfuldoom/v1/agent.proto \
    && python3.11 -m pip uninstall -y grpcio-tools \
    && python3.11 -c "import sys; sys.path.insert(0,'/opt/capsule/rdgen'); \
from restfuldoom.v1 import agent_pb2, agent_pb2_grpc; print('stubs OK:', agent_pb2.DESCRIPTOR.package)" \
    && mkdir -p /opt/capsule/restfuldoom_agent \
    && cp agent/restfuldoom_agent/__init__.py \
          agent/restfuldoom_agent/brain.py \
          agent/restfuldoom_agent/client.py \
          agent/restfuldoom_agent/env.py \
          agent/restfuldoom_agent/reward.py \
          agent/restfuldoom_agent/rollout_config.py \
          agent/restfuldoom_agent/schemas.py \
          agent/restfuldoom_agent/skill_policy.py \
          /opt/capsule/restfuldoom_agent/ \
    && python3.11 -c "import os; os.environ['RESTFULDOOM_PROTO_STUBS']='/opt/capsule/rdgen'; \
from restfuldoom_agent.env import SkillController; from restfuldoom_agent.brain import AgentMemory; \
print('brain OK:', SkillController.__name__, AgentMemory.__name__)" \
    && rm -rf /tmp/rdoom
WORKDIR /

# Stage app WADs, or fetch shareware DOOM1.WAD (same WAD-free-context policy as hellbox-doom).
COPY app/ /home/app/app/
COPY wad-source.json /tmp/wad-source.json
RUN set -e; \
    DOOM1_WAD_URL="$(python3.11 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["DOOM1_WAD_URL"])' /tmp/wad-source.json)"; \
    DOOM1_WAD_SHA256="$(python3.11 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["DOOM1_WAD_SHA256"])' /tmp/wad-source.json)"; \
    if ! ls /home/app/app/*.wad /home/app/app/*.WAD >/dev/null 2>&1; then \
        echo "[build] fetching shareware DOOM1.WAD"; \
        curl -fsSL --retry 3 -o /home/app/app/DOOM1.WAD "${DOOM1_WAD_URL}"; \
        printf '%s  %s\n' "$DOOM1_WAD_SHA256" /home/app/app/DOOM1.WAD | sha256sum -c -; \
    fi; \
    wad="$(ls /home/app/app/*.wad /home/app/app/*.WAD 2>/dev/null | head -1)"; \
    [ -n "$wad" ] || { echo "FATAL: no IWAD present"; exit 1; }; \
    sz=$(stat -c%s "$wad"); [ "$sz" -ge 4000000 ] || { echo "FATAL: IWAD too small ($sz)"; exit 1; }; \
    head -c 4 "$wad" | grep -q IWAD || { echo "FATAL: $wad is not an IWAD"; exit 1; }; \
    chown -R app:app /home/app/app; \
    echo "== staged app files =="; ls -l /home/app/app

# Browser page.
COPY index.html /opt/novnc/index.html

# Runtime scripts (streaming stack shared with hellbox-doom + the agent bridge).
COPY rootfs/ /
RUN chmod +x /opt/capsule/start.sh /opt/capsule/run_app.sh

EXPOSE 6901 6902 9000
CMD ["/opt/capsule/start.sh"]
