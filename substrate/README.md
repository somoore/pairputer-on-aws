# pairputer substrate

This directory holds the pairputer substrate: the CloudFormation templates and the container images (MCP
server and streaming relay) that the deployed system runs on.

To deploy pairputer, use the 1-click CloudFormation launch from the [repository README](../README.md). It
deploys pairputer's signed public images with no clone and no Docker.

For how the deployed system works, see [`../docs/architecture.md`](../docs/architecture.md) and
[`../SECURITY.md`](../SECURITY.md). The full cost breakdown for every resource is in
[`../docs/1-click-cost.md`](../docs/1-click-cost.md).

## Connect a chat host

Connect ChatGPT and Claude with the guides in [`../docs/chatgpt.md`](../docs/chatgpt.md) and
[`../docs/claude.md`](../docs/claude.md). Each connector covers that product's web, desktop, and mobile
apps, and Codex rides the ChatGPT connector.

## Remove everything

```bash
./substrate/remove-cf.sh            # delete the stack and all nested stacks
./substrate/remove-cf.sh --all      # also remove the artifact bucket and ECR repos
```

Capsule cartridge stacks are deleted first; each one's reaper terminates leftover MicroVMs and deletes
its image, then the root stack tears down every nested stack in dependency order.
