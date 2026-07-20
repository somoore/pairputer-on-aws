<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="./brand/pairputer-logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="./brand/pairputer-logo-light.png">
    <img alt="pairputer" src="./brand/pairputer-logo-light.png" width="440">
  </picture>
</div>

**Stream a live Linux MicroVM into your AI chat — video, audio, keyboard, mouse — running entirely in your own AWS account.**

pairputer is a deployable *substrate*: it runs an interactive **capsule** (a Lambda MicroVM workload) and streams it inline into an AI chat client, where a human gets a live viewport and — soon — the model gets a controlled tool surface. It suspends on idle (**Freeze**) and resumes on demand (**Thaw**), so you only pay while you're using it.

**One server, one widget, three hosts — all live and human-confirmed:** **OpenAI Codex**, **ChatGPT** (web + desktop), and **Claude** (web + desktop). Once connected, open the reference capsule with a single prompt:

> Use the pairputer app to open the Agent DOOM capsule (play_capsule) so I can watch it live.

Per-host connector setup: [`docs/hosts/codex.md`](./docs/hosts/codex.md) · [`docs/hosts/chatgpt.md`](./docs/hosts/chatgpt.md) · [`docs/hosts/claude.md`](./docs/hosts/claude.md).

The first reference capsule is **Hellbox** — real DOOM in a MicroVM. It proves the hard part: realtime streaming + input, into the chat, with nothing running on your laptop but the chat app.

## Why pairputer

- **Runs in *your* AWS account.** No third-party SaaS holds your session; no static credentials leave your machine.
- **True 1-click.** Signed, digest-pinned public images + a public capsule build context mean zero local build.
- **Secure by construction.** OAuth (Cognito PKCE), private VPC data plane behind CloudFront + WAF, cosign-signed images with SLSA provenance you can verify yourself.
- **Bring your own workload.** DOOM is just the demo — the substrate is capsule-agnostic.

## Deploy it

Two paths. Both land in your account, `us-east-1`.

### 🚀 1-click — CloudFormation (fastest)

[![Launch Stack](https://s3.amazonaws.com/cloudformation-examples/cloudformation-launch-stack.png)](https://console.aws.amazon.com/cloudformation/home?region=us-east-1#/stacks/create/review?templateURL=https://pairputer-launch.s3.amazonaws.com/templates/pairputer.yaml&stackName=pairputer)

Click the button, review the parameters, deploy. **Zero inputs required** — it defaults to pairputer's signed public images and a playable DOOM capsule. Behind the scenes it stands up Cognito, the MCP control plane (Bedrock AgentCore), a private CloudFront-fronted data plane, and builds the DOOM MicroVM image in your account.

After it finishes, you get an **admin invite email** with your temporary password and the exact steps to connect Codex — including a one-line setup command. Then `codex mcp login pairputer` and play.

*Want to verify the images first?* Run [`scripts/verify-images.sh`](./scripts/verify-images.sh) — an offline cosign signature + SLSA check.

### 🛠️ CLI — `deploy.sh` (for building from source / customizing)

Use this when you want to **build the images from source**, use **private ECR**, or have Codex wired up **automatically**:

```bash
git clone https://github.com/somoore/pairputer && cd pairputer
substrate/deploy.sh
```

`deploy.sh` builds + pushes the MCP and relay images, packages the capsule, deploys the whole nested stack, creates your super-admin, **and wires `~/.codex/config.toml` for you** (writes the server block + registers the OAuth callback). One command, ready to log in. See [`substrate/README.md`](./substrate/README.md) for options.

## Options worth knowing

- **Image source** *(first parameter)* — `Public` (default, our signed images) or `Private` (your own private-ECR images; leave URIs blank to auto-copy ours into your account, verified first).
- **Bundle reference capsule** *(default on)* — ships DOOM so you have something playable. Turn it off for a **bare substrate** with no capsule.

## Remove everything

```bash
substrate/remove-cf.sh            # delete the stack (all nested stacks go with it)
substrate/remove-cf.sh --all      # also remove the artifact bucket + ECR repos
substrate/remove-cf.sh --all --yes  # no confirmation prompt
```

Cartridge capsule stacks (`pairputer-capsule-*`) are deleted first automatically — each one's reaper
terminates leftover MicroVMs and deletes its image. Then the root stack tears down every nested stack
in dependency order. Nothing is left running, so the bill stops.

## Learn more

- [`docs/architecture.md`](./docs/architecture.md) — how the pieces fit (diagram)
- [`SECURITY.md`](./SECURITY.md) — the end-to-end supply-chain + trust model
