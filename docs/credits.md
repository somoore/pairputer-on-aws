# Credits and acknowledgments

pairputer stands on a large amount of open-source and cloud infrastructure. This page credits the
projects, tools, and services it is built from. pairputer itself is released under the
[MIT License](../LICENSE).

If we have missed or misattributed anything, please
[open an issue](https://github.com/somoore/pairputer-on-aws/issues) and we will correct it.

## DOOM

The Agent DOOM and Hellbox DOOM capsules are built on the DOOM open-source lineage:

- **DOOM** and the shareware `DOOM1.WAD` game data, originally by **id Software**. The capsules ship only
  the freely redistributable shareware WAD; they do not include or distribute the retail `DOOM.WAD` or
  `DOOM2.WAD`.
- **[Chocolate Doom](https://github.com/chocolate-doom/chocolate-doom)** (GPLv2), the source port the
  Hellbox DOOM capsule builds from.
- **RESTful-DOOM**, which adds a programmatic control surface on top of the Chocolate Doom lineage. Agent
  DOOM builds from the [somoore/restful-doom](https://github.com/somoore/restful-doom) fork, itself based
  on the original [jeff-1amstudios/restful-doom](https://github.com/jeff-1amstudios/restful-doom).
- The shareware WAD is fetched from
  **[nneonneo/universal-doom](https://github.com/nneonneo/universal-doom)**.

## Streaming and desktop

The capsules render and stream a live Linux desktop with these projects:

- **[TigerVNC](https://tigervnc.org/)** (`Xvnc`), the X server the capsules display on.
- **[FFmpeg](https://ffmpeg.org/)**, which encodes the video (H.264) and audio (Opus) streams. The static
  ARM64 builds come from **[BtbN/FFmpeg-Builds](https://github.com/BtbN/FFmpeg-Builds)**.
- **[PulseAudio](https://www.freedesktop.org/wiki/Software/PulseAudio/)** and **ALSA**, for audio capture.
- **[Mutter](https://gitlab.gnome.org/GNOME/mutter)**, the window manager, plus **[X.Org](https://www.x.org/)**,
  **[Mesa](https://www.mesa3d.org/)**, **dbus**, and **[AT-SPI](https://www.freedesktop.org/wiki/Accessibility/AT-SPI2/)**
  for accessibility.
- **[noVNC](https://github.com/noVNC/noVNC)** and **[websockify](https://github.com/novnc/websockify)**,
  the in-VM VNC fallback view.
- **[SDL2](https://github.com/libsdl-org/SDL)**, with **[SDL2_mixer](https://github.com/libsdl-org/SDL_mixer)**
  and **[SDL2_net](https://github.com/libsdl-org/SDL_net)**, used by the DOOM engine.

The Pairputer Workbench desktop additionally bundles:

- **[Visual Studio Code](https://github.com/microsoft/vscode)** in the browser, via
  **[code-server](https://github.com/coder/code-server)** by Coder.
- **[ungoogled-chromium](https://github.com/ungoogled-software/ungoogled-chromium)**, using the portable
  ARM64 build from
  **[ungoogled-software/ungoogled-chromium-portablelinux](https://github.com/ungoogled-software/ungoogled-chromium-portablelinux)**,
  with the Chromium sandbox binary from Debian.
- **[Homebrew](https://github.com/Homebrew/brew)**, **[GitHub CLI](https://github.com/cli/cli)**,
  **[ripgrep](https://github.com/BurntSushi/ripgrep)**, and **[uv](https://github.com/astral-sh/uv)** by
  Astral.

## Runtime libraries

The MCP control-plane server is a Python service built on:

- **[Model Context Protocol Python SDK](https://github.com/modelcontextprotocol/python-sdk)**, including
  **FastMCP**, the server framework.
- **[boto3](https://github.com/boto/boto3)**, **[cryptography](https://github.com/pyca/cryptography)**,
  and **[PyYAML](https://github.com/yaml/pyyaml)**.

The streaming relay is a Node.js service built on the **[AWS SDK for JavaScript v3](https://github.com/aws/aws-sdk-js-v3)**.

The in-VM capsule services use **[python-xlib](https://github.com/python-xlib/python-xlib)** (XTEST
input), **[websockets](https://github.com/python-websockets/websockets)**,
**[NumPy](https://github.com/numpy/numpy)**, and, for the agent bridge,
**[gRPC](https://github.com/grpc/grpc)** with **[Protocol Buffers](https://github.com/protocolbuffers/protobuf)**.

## AWS

pairputer runs entirely on AWS, using:

- **AWS Lambda MicroVM images**, which run each capsule.
- **Amazon Bedrock AgentCore**, which hosts the MCP server.
- **Amazon ECS on AWS Fargate** with **Application Auto Scaling**, for the streaming relay.
- **Amazon CloudFront** and **AWS WAF**, the streaming front door.
- **Amazon Cognito**, for OAuth and identity.
- **Amazon DynamoDB**, **AWS Secrets Manager**, and **Amazon ECR**.
- **Elastic Load Balancing**, **Amazon VPC**, **AWS CodeBuild** (private-image verify and copy), and
  **Amazon CloudWatch Logs**.

## Supply chain and build tooling

The signed-image supply chain uses:

- **[Sigstore cosign](https://github.com/sigstore/cosign)**, for keyless signing and verification.
- **[SLSA](https://slsa.dev/)** build provenance.
- **[crane](https://github.com/google/go-containerregistry)**, which copies verified images into private
  ECR.
- **GitHub Actions** with OIDC, for keyless AWS authentication.

The default networking mode uses **[fck-nat](https://github.com/AndrewGuenther/fck-nat)** by Andrew
Guenther, a low-cost NAT instance, instead of a managed NAT Gateway.
