# pairputer MCP server container for Bedrock AgentCore Runtime.
# AgentCore REQUIRES linux/arm64 and expects streamable-http MCP on 0.0.0.0:8000/mcp.
# Digest-pinned (supply chain): python:3.12-slim-bookworm arm64. A rebuild can't silently pull a
# different base. To bump: docker buildx imagetools inspect python:3.12-slim-bookworm (arm64 digest).
FROM --platform=linux/arm64 python:3.12-slim-bookworm@sha256:49f3bd3ed3a1c554a43f1e9d03ff303716afed6eda0f3e98c34cd976c8880cd6

WORKDIR /app
# Unbuffered stdout/stderr so server logs reach CloudWatch in real time (AgentCore captures the
# process's stdout). Without this the runtime-logs stream shows only the framework's pings and our
# own logging never appears — which made a VM-launch bug take hours to diagnose. See wall #17.
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY server.py app.html ./
COPY hosts/ ./hosts/

# AgentCore probes :8000/mcp. We also serve /sse/video on the same port (Phase 2 verifies pass-through).
EXPOSE 8000
CMD ["python", "server.py"]
