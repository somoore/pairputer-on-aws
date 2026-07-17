import http from "node:http";
import crypto from "node:crypto";
import { connect as tlsConnect } from "node:tls";
import {
  CreateMicrovmAuthTokenCommand,
  GetMicrovmCommand,
  LambdaMicrovmsClient,
} from "@aws-sdk/client-lambda-microvms";
import { DynamoDBClient, QueryCommand, TransactGetItemsCommand } from "@aws-sdk/client-dynamodb";
import { ECSClient, UpdateServiceCommand } from "@aws-sdk/client-ecs";
import { GetSecretValueCommand, SecretsManagerClient } from "@aws-sdk/client-secrets-manager";
import {
  CloudWatchLogsClient,
  CreateLogGroupCommand,
  CreateLogStreamCommand,
  PutLogEventsCommand,
} from "@aws-sdk/client-cloudwatch-logs";

const REGION = process.env.AWS_REGION || process.env.AWS_DEFAULT_REGION || "us-east-1";
const PORT = Number.parseInt(process.env.PORT || "8080", 10);
const VIDEO_PORT = process.env.PAIRPUTER_VIDEO_PORT || "6903";
const AUDIO_PORT = process.env.PAIRPUTER_AUDIO_PORT || "6902";
const INPUT_PORT = process.env.PAIRPUTER_INPUT_PORT || "6904";
const COPLAY_PORT = process.env.PAIRPUTER_COPLAY_PORT || "6906";  // input arbiter state (whose turn)
const SESSION_SECRET_ARN = process.env.PAIRPUTER_SESSION_SECRET_ARN || "";
const ALLOW_UNSIGNED_DEV = process.env.PAIRPUTER_ALLOW_UNSIGNED_DEV === "1";
const MIN_SESSION_SECRET_BYTES = 32;
const SESSION_TABLE = process.env.PAIRPUTER_SESSION_TABLE || "";
const RELAY_ACTIVE_SHARDS = 64;
const RELAY_CLUSTER = process.env.PAIRPUTER_RELAY_CLUSTER || "";
const RELAY_SERVICE = process.env.PAIRPUTER_RELAY_SERVICE || "";
const RELAY_WARM_SECONDS = Number.isFinite(Number.parseInt(process.env.PAIRPUTER_RELAY_WARM_SECONDS || "900", 10))
  ? Number.parseInt(process.env.PAIRPUTER_RELAY_WARM_SECONDS || "900", 10)
  : 900;
// Opt-in debug: exposes /vmdbg to proxy capsule logs from the VM's :9000 hook.
const DEBUG = ["1", "true", "yes", "on"].includes((process.env.PAIRPUTER_DEBUG || "").toLowerCase());
// Runtime log shipping: a RunMicrovm VM doesn't stream its console to CloudWatch, so the relay pulls the
// capsule's service logs from the loopback :9000 /dbg endpoints and PutLogEvents them (from the relay's
// own task role) to a per-capsule group. On by default; set PAIRPUTER_SHIP_CAPSULE_LOGS=off to disable.
const SHIP_LOGS = !["0", "false", "no", "off"].includes(
  (process.env.PAIRPUTER_SHIP_CAPSULE_LOGS || "on").toLowerCase());
const LOG_SHIP_INTERVAL_MS = Number.parseInt(process.env.PAIRPUTER_LOG_SHIP_INTERVAL_MS || "5000", 10);
const MAX_UPSTREAM_HTTP_BYTES = 1024 * 1024;
const MAX_UPSTREAM_WS_HEADER_BYTES = 16 * 1024;
const MAX_UPSTREAM_WS_FRAME_BYTES = 8 * 1024 * 1024;
const MAX_UPSTREAM_WS_BUFFER_BYTES = 16 * 1024 * 1024;
// 400ms (was 1000ms): the embedded player's EventSource auto-retry is 1000ms (`retry: 1000`). A
// same-viewer reconnect that arrives during the handoff window used to 409, and the player treats a
// 409 as a hard error → re-mint → restart storm. Making the handoff SHORTER than the client retry
// clears the old reservation before the retry fires, so a legitimate reconnect wins instead of 409ing.
const VIEWER_STREAM_HANDOFF_MS = 400;
// A reservation is installed before the AWS control-plane checks and guest WebSocket handshake.
// If that work is interrupted in a way Node never reports as a downstream close, the placeholder
// must not block this viewer (or a human-takeover viewer) forever.
const VIEWER_STREAM_RESERVATION_TTL_MS = 20_000;
const UPSTREAM_TOTAL_TIMEOUT_MS = 15_000;
const STREAM_REVALIDATE_MS = 3_000;
const MAX_RETIRED_VIEWERS = 128;
const MAX_EVENT_SUBSCRIBERS = 4;
const MAX_EVENT_PAYLOAD_BYTES = 128 * 1024;
const MAX_EVENT_BUFFER_BYTES = 64 * 1024;
const EVENT_POLL_INTERVAL_MS = 250;
const EVENT_HEARTBEAT_MS = 15_000;
// The capsule service logs to tail (the teed files served by :9000). agent_bridge only exists on
// agent-interactive capsules; a missing file just returns {size:0} and is skipped.
const CAPSULE_LOG_SOURCES = [
  { key: "inputws", path: "/dbg/inputws" },
  { key: "bridge", path: "/dbg/bridge" },
  { key: "session", path: "/dbg/session" },
];

const mvm = new LambdaMicrovmsClient({ region: REGION });
const ecs = new ECSClient({ region: REGION });
const ddb = new DynamoDBClient({ region: REGION });
const sm = new SecretsManagerClient({ region: REGION });
const cwl = new CloudWatchLogsClient({ region: REGION });

let secret = null;
let secretInflight = null;
const sessions = new Map();
const sessionFreshness = new Map();
const SESSION_FRESHNESS_CACHE_MS = 1000;
const LEGACY_VIEWER = Symbol("legacy-viewer");
let relayScaleDownTimer = null;
let relayScaleDownInflight = null;

function b64url(buf) {
  return Buffer.from(buf).toString("base64url");
}

function unb64url(str) {
  return Buffer.from(str, "base64url");
}

async function getSecret() {
  if (secret !== null) return secret;
  if (!SESSION_SECRET_ARN) {
    if (ALLOW_UNSIGNED_DEV) {
      secret = "";
      return secret;
    }
    throw new Error("PAIRPUTER_SESSION_SECRET_ARN is required");
  }
  if (secretInflight) return secretInflight;
  secretInflight = (async () => {
    const r = await sm.send(new GetSecretValueCommand({ SecretId: SESSION_SECRET_ARN }));
    const value = typeof r.SecretString === "string" ? r.SecretString : "";
    if (Buffer.byteLength(value, "utf8") < MIN_SESSION_SECRET_BYTES) {
      throw new Error("relay session secret is missing or too short");
    }
    secret = value;
    return secret;
  })().finally(() => {
    secretInflight = null;
  });
  return secretInflight;
}

async function verifySessionToken(token) {
  const s = await getSecret();
  if (!token || token.indexOf(".") < 0) return null;
  const [payloadB64, sigB64] = token.split(".");
  if (!payloadB64 || !sigB64) return null;
  const unsignedDev = ALLOW_UNSIGNED_DEV && !SESSION_SECRET_ARN;
  if (!unsignedDev) {
    const want = crypto.createHmac("sha256", s).update(payloadB64).digest();
    const got = unb64url(sigB64);
    if (got.length !== want.length || !crypto.timingSafeEqual(got, want)) return null;
  }
  let claims;
  try {
    claims = JSON.parse(unb64url(payloadB64).toString("utf8"));
  } catch {
    return null;
  }
  if (!claims || typeof claims !== "object") return null;
  for (const key of ["tenantId", "sessionId", "microvmId", "imageId"]) {
    if (typeof claims[key] !== "string" || !claims[key]) return null;
  }
  if (!unsignedDev) {
    for (const key of ["releaseDigest", "manifestDigest", "imageArn", "imageVersion"]) {
      if (typeof claims[key] !== "string" || !claims[key]) return null;
    }
  }
  const sessionVersion = Number.parseInt(String(claims.sessionVersion || "1"), 10);
  if (!Number.isFinite(sessionVersion)) return null;
  claims.sessionVersion = sessionVersion;
  if (!Number.isFinite(claims.exp) || claims.exp < Math.floor(Date.now() / 1000)) return null;
  if (!Array.isArray(claims.channels)) return null;
  return claims;
}

function hasChannel(claims, channel) {
  return claims.channels.includes("*") || claims.channels.includes(channel);
}

function ddbAttrString(attr) {
  if (!attr) return "";
  if (typeof attr.S === "string") return attr.S;
  if (typeof attr.N === "string") return attr.N;
  return "";
}

function ddbAttrNumber(attr, fallback = 1) {
  const n = Number.parseInt(ddbAttrString(attr) || String(fallback), 10);
  return Number.isFinite(n) ? n : fallback;
}

function ddbAttrBool(attr) {
  if (!attr) return false;
  if (typeof attr.BOOL === "boolean") return attr.BOOL;
  return ddbAttrString(attr) === "true";
}

// Channels that open an upstream to the VM (and thus auto-resume a suspended one). The suspend guard
// in authorize() applies ONLY to these — never to "control" (/drain, which quiesces the VM during
// freeze) or "state" (a read that opens no upstream).
const STREAM_CHANNELS = new Set(["video", "audio", "input", "player"]);

async function loadActiveSession(claims) {
  if (!SESSION_TABLE) return null;
  const out = await ddb.send(new TransactGetItemsCommand({
    TransactItems: [
      { Get: { TableName: SESSION_TABLE, Key: {
        pk: { S: `TENANT#${claims.tenantId}` },
        sk: { S: `IMAGE#${claims.imageId}` },
      } } },
      { Get: { TableName: SESSION_TABLE, Key: {
        pk: { S: `MICROVM#${claims.microvmId}` },
        sk: { S: "OWNER" },
      } } },
    ],
  }));
  const session = out.Responses?.[0]?.Item || null;
  const owner = out.Responses?.[1]?.Item || null;
  if (!session || !owner) return null;
  if (
    ddbAttrString(owner.tenant_id) !== claims.tenantId
    || ddbAttrString(owner.image_id) !== claims.imageId
    || ddbAttrString(owner.microvm_id) !== claims.microvmId
    || ddbAttrString(owner.session_id) !== claims.sessionId
    || ddbAttrNumber(owner.session_version, 1) !== claims.sessionVersion
    || ddbAttrString(owner.release_digest) !== claims.releaseDigest
    || ddbAttrString(owner.manifest_digest) !== claims.manifestDigest
    || ddbAttrString(owner.image_arn) !== claims.imageArn
    || ddbAttrString(owner.image_version) !== claims.imageVersion
  ) return null;
  return session;
}

async function loadActiveSessionCoalesced(claims) {
  const key = sessionKey(claims);
  const now = Date.now();
  const cached = sessionFreshness.get(key);
  if (cached && cached.item && cached.expiresAt > now) return cached.item;
  if (cached && cached.inflight) return cached.inflight;
  const inflight = loadActiveSession(claims).then(item => {
    sessionFreshness.set(key, { item, expiresAt: Date.now() + SESSION_FRESHNESS_CACHE_MS });
    return item;
  }).catch(err => {
    sessionFreshness.delete(key);
    throw err;
  });
  sessionFreshness.set(key, { inflight, expiresAt: 0 });
  return inflight;
}

function sessionClaimsFresh(claims, item) {
  if (!item) return false;
  const currentSessionVersion = ddbAttrNumber(item.session_version || item.sessionVersion, 1);
  return (
    ddbAttrString(item.tenant_id) === claims.tenantId
    && ddbAttrString(item.image_id) === claims.imageId
    && ddbAttrString(item.microvm_id) === claims.microvmId
    && ddbAttrString(item.session_id) === claims.sessionId
    && currentSessionVersion === claims.sessionVersion
    && ddbAttrString(item.release_digest) === claims.releaseDigest
    && ddbAttrString(item.manifest_digest) === claims.manifestDigest
    && ddbAttrString(item.image_arn) === claims.imageArn
    && ddbAttrString(item.image_version) === claims.imageVersion
  );
}

// The control plane sets state=SUSPENDING/SUSPENDED + frozen=true in DynamoDB BEFORE it calls
// suspend_microvm. Opening an upstream to the VM auto-resumes it (idlePolicy.autoResumeEnabled),
// so a browser EventSource that auto-reconnects during the freeze window would otherwise fight the
// suspend forever (the "stuck freezing…" bug). Refusing here — on the authoritative session state,
// not the possibly-stale token claim — makes freeze win the race deterministically. A SUSPENDED VM
// is legitimately resumed via the thaw path (which flips state back to RUNNING first), not via a
// relay stream reconnect.
function sessionSuspending(item) {
  if (!item) return false;
  const state = ddbAttrString(item.state).toUpperCase();
  return state === "SUSPENDING" || state === "SUSPENDED" || ddbAttrBool(item.frozen);
}

function json(res, statusCode, body) {
  res.writeHead(statusCode, {
    "content-type": "application/json",
    "cache-control": "no-store",
    "access-control-allow-origin": "*",
  });
  res.end(JSON.stringify(body));
}

function text(res, statusCode, body) {
  res.writeHead(statusCode, {
    "content-type": "text/plain; charset=utf-8",
    "cache-control": "no-store",
    "access-control-allow-origin": "*",
  });
  res.end(body);
}

function safeStatusCode(err, fallback = 500) {
  const statusCode = Number(err && err.statusCode);
  return [400, 403, 409, 413, 429, 502, 503, 504].includes(statusCode) ? statusCode : fallback;
}

function publicRelayError(statusCode) {
  if (statusCode === 400) return "bad request";
  if (statusCode === 403) return "forbidden";
  if (statusCode === 409) return "conflict";
  if (statusCode === 413) return "request too large";
  if (statusCode === 429) return "rate limit exceeded";
  if (statusCode === 503) return "microvm unavailable";
  if (statusCode === 504) return "upstream timeout";
  return "upstream unavailable";
}

function logFailure(scope, err) {
  const code = typeof (err && err.code) === "string" && /^[A-Z0-9_]{1,40}$/.test(err.code)
    ? err.code
    : "UNAVAILABLE";
  console.error(scope, { code });
}

function sessionKey(claims) {
  return `${claims.tenantId}:${claims.imageId}:${claims.microvmId}:${claims.sessionId}:${claims.releaseDigest || "dev"}`;
}

function getSession(claims) {
  const key = sessionKey(claims);
  let sess = sessions.get(key);
  if (!sess) {
    sess = {
      key,
      claims,
      streamClosers: new Set(),
      audioStreamCloser: null,
      videoStreamCloser: null,
      activeViewerId: null,
      activeViewerGeneration: 0,
      viewerAware: false,
      retiredViewerIds: new Set(),
      viewerReadyAt: 0,
      viewerClaimInflight: null,
      audioReservationInflight: null,
      videoReservationInflight: null,
      inputOperationInflight: null,
      viewerInputState: new Map(),
      inputClients: new Set(),
      inputUpstream: null,
      inputInflight: null,
      token: null,
      tokenExp: 0,
      draining: false,
      lastActive: Date.now(),
      expiresAt: Number(claims.exp || 0) * 1000,
      postInputWindowStart: Date.now(),
      postInputEventCount: 0,
      postInputMouseCount: 0,
      eventSubscribers: new Set(),
      eventPoller: null,
      eventLastBody: null,
      eventLastBodyAt: 0,
      eventLastFreshnessCheckAt: 0,
      eventLastErrorAt: 0,
    };
    sessions.set(key, sess);
  }
  sess.claims = claims;
  sess.lastActive = Date.now();
  sess.expiresAt = Math.max(sess.expiresAt || 0, Number(claims.exp || 0) * 1000);
  if (!sess.draining) cancelRelayScaleDown();
  return sess;
}

function viewerIdFrom(reqUrl) {
  if (!reqUrl.searchParams.has("viewer")) return { present: false, valid: false, value: null };
  const value = reqUrl.searchParams.get("viewer") || "";
  if (!/^[A-Za-z0-9_-]{8,128}$/.test(value)) return { present: true, valid: false, value: null };
  return { present: true, valid: true, value };
}

function closeViewerStreams(sess) {
  // Do not cut across a stream whose upstream handshake is still being established. The new
  // player's EventSource will retry, then take over as soon as the closer is attached.
  const now = Date.now();
  for (const field of ["audioStreamCloser", "videoStreamCloser"]) {
    const slot = sess[field];
    if (!slot || slot.close) continue;
    if (Number(slot.expiresAt || 0) > now) return false;
    // The in-flight opener will fail its identity check before attaching and close its own socket.
    if (sess[field] === slot) sess[field] = null;
  }
  for (const field of ["audioStreamCloser", "videoStreamCloser"]) {
    const slot = sess[field];
    if (!slot || typeof slot.close !== "function") continue;
    sess[field] = null;
    sess.streamClosers.delete(slot.close);
    try { slot.close(); } catch {}
  }
  return true;
}

function sameViewerLease(a, b) {
  return !!a && !!b && a.viewerId === b.viewerId && a.generation === b.generation;
}

function ownsViewerLease(sess, lease) {
  if (!lease) return false;
  if (lease.legacy) return !sess.viewerAware && sess.activeViewerId === null;
  return sess.viewerAware
    && sess.activeViewerId === lease.viewerId
    && sess.activeViewerGeneration === lease.generation
    && !sess.retiredViewerIds.has(lease.viewerId);
}

async function withSessionLock(sess, field, fn) {
  const previous = sess[field];
  let release;
  const current = new Promise(resolve => { release = resolve; });
  sess[field] = current;
  if (previous) await previous;
  try {
    return await fn();
  } finally {
    release();
    if (sess[field] === current) sess[field] = null;
  }
}

function viewerStateKey(lease) {
  return lease && lease.legacy ? LEGACY_VIEWER : lease && lease.viewerId;
}

function getViewerInputState(sess, lease) {
  const key = viewerStateKey(lease);
  let state = sess.viewerInputState.get(key);
  if (!state) {
    state = { heldKeys: new Set(), heldButtons: new Set() };
    sess.viewerInputState.set(key, state);
  }
  return state;
}

function closeEventSubscriber(sess, subscriber) {
  if (!subscriber || subscriber.closed) return;
  subscriber.closed = true;
  sess.eventSubscribers.delete(subscriber);
  try { subscriber.res.end(); } catch {}
}

function closeViewerEventSubscribers(sess, lease) {
  for (const subscriber of [...sess.eventSubscribers]) {
    if (sameViewerLease(subscriber.lease, lease)) closeEventSubscriber(sess, subscriber);
  }
}

async function claimViewer(sess, viewerIdentity, options = {}) {
  return withSessionLock(sess, "viewerClaimInflight", async () => {
    if (options.signal && options.signal.aborted) {
      return { ok: false, statusCode: 409, reason: "viewer request closed" };
    }
    if (viewerIdentity.present && !viewerIdentity.valid) {
      return { ok: false, statusCode: 400, reason: "invalid viewer id" };
    }
    if (!viewerIdentity.present) {
      if (sess.viewerAware) return { ok: false, statusCode: 409, reason: "viewer id required" };
      return { ok: true, waitMs: 0, lease: { viewerId: null, generation: 0, legacy: true } };
    }

    const viewerId = viewerIdentity.value;
    if (sess.retiredViewerIds.has(viewerId)) {
      return { ok: false, statusCode: 409, reason: "viewer superseded" };
    }
    if (sess.activeViewerId === viewerId) {
      return {
        ok: true,
        waitMs: Math.max(0, sess.viewerReadyAt - Date.now()),
        lease: { viewerId, generation: sess.activeViewerGeneration, legacy: false },
      };
    }
    if (sess.activeViewerId && sess.retiredViewerIds.size >= MAX_RETIRED_VIEWERS) {
      return { ok: false, statusCode: 429, reason: "viewer handoff limit reached" };
    }
    const hadViewerStreams = !!sess.audioStreamCloser || !!sess.videoStreamCloser;
    if (!closeViewerStreams(sess)) {
      return { ok: false, statusCode: 409, reason: "viewer handoff in progress" };
    }

    const hasLegacyResources = !sess.viewerAware && (
      sess.viewerInputState.has(LEGACY_VIEWER)
      || sess.eventSubscribers.size > 0
      || hadViewerStreams
    );
    const oldLease = sess.activeViewerId ? {
      viewerId: sess.activeViewerId,
      generation: sess.activeViewerGeneration,
      legacy: false,
    } : hasLegacyResources ? { viewerId: null, generation: 0, legacy: true } : null;
    try {
      await withSessionLock(sess, "inputOperationInflight", async () => {
        if (options.signal && options.signal.aborted) throw new Error("viewer request closed");
        if (oldLease && !ownsViewerLease(sess, oldLease)) throw new Error("viewer changed");
        if (oldLease) {
          const oldState = sess.viewerInputState.get(viewerStateKey(oldLease));
          if (oldState) await releaseHeld(sess, oldState.heldKeys, oldState.heldButtons);
          if (options.signal && options.signal.aborted) throw new Error("viewer request closed");
          sess.viewerInputState.delete(viewerStateKey(oldLease));
          closeViewerEventSubscribers(sess, oldLease);
          if (!oldLease.legacy) sess.retiredViewerIds.add(oldLease.viewerId);
        }
        sess.viewerAware = true;
        sess.activeViewerGeneration += 1;
        sess.activeViewerId = viewerId;
      });
    } catch {
      return { ok: false, statusCode: 503, reason: "viewer handoff failed" };
    }
    // Give the guest's single-client WebSocket service a brief moment to observe the old TLS close.
    sess.viewerReadyAt = oldLease ? Date.now() + VIEWER_STREAM_HANDOFF_MS : 0;
    return {
      ok: true,
      waitMs: oldLease ? VIEWER_STREAM_HANDOFF_MS : 0,
      lease: { viewerId, generation: sess.activeViewerGeneration, legacy: false },
    };
  });
}

function waitForViewerStreamRelease(signal) {
  return new Promise(resolve => {
    if (signal?.aborted) { resolve(false); return; }
    let timer = null;
    const onAbort = () => {
      if (timer) clearTimeout(timer);
      signal?.removeEventListener("abort", onAbort);
      resolve(false);
    };
    timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve(true);
    }, VIEWER_STREAM_HANDOFF_MS);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

async function reserveViewerStream(sess, field, lockField, lease, options = {}) {
  return withSessionLock(sess, lockField, async () => {
    if (options.signal?.aborted) return false;
    let current = sess[field];
    if (current && !current.close && Number(current.expiresAt || 0) <= Date.now()) {
      if (sess[field] === current) sess[field] = null;
      current = null;
    }
    if (!current) {
      sess[field] = { lease, close: null, expiresAt: Date.now() + VIEWER_STREAM_RESERVATION_TTL_MS };
      return true;
    }
    // EventSource can reconnect a channel before the old same-viewer TLS close reaches the guest.
    // Atomically install a placeholder before closing the old channel. A third reconnect sees the
    // placeholder and fails closed instead of racing another upstream into the single-client guest.
    if (!sameViewerLease(current.lease, lease) || typeof current.close !== "function") return false;
    const oldClose = current.close;
    const placeholder = {
      lease,
      close: null,
      expiresAt: Date.now() + VIEWER_STREAM_RESERVATION_TTL_MS,
    };
    sess[field] = placeholder;
    sess.streamClosers.delete(oldClose);
    try { oldClose(); } catch {}
    const released = await waitForViewerStreamRelease(options.signal);
    if (!released || sess[field] !== placeholder || !ownsViewerLease(sess, lease)) {
      if (sess[field] === placeholder) sess[field] = null;
      return false;
    }
    return true;
  });
}

async function reserveAudioStream(sess, lease, options = {}) {
  return reserveViewerStream(sess, "audioStreamCloser", "audioReservationInflight", lease, options);
}

function attachAudioStream(sess, lease, closeUpstream) {
  const slot = sess.audioStreamCloser;
  if (!slot || !sameViewerLease(slot.lease, lease) || slot.close) return false;
  slot.close = closeUpstream;
  return true;
}

function releaseAudioStream(sess, lease, closeUpstream = null) {
  const slot = sess.audioStreamCloser;
  if (slot && sameViewerLease(slot.lease, lease) && (!closeUpstream || slot.close === closeUpstream)) {
    sess.audioStreamCloser = null;
  }
}

async function reserveVideoStream(sess, lease, options = {}) {
  return reserveViewerStream(sess, "videoStreamCloser", "videoReservationInflight", lease, options);
}

function attachVideoStream(sess, lease, closeUpstream) {
  const slot = sess.videoStreamCloser;
  if (!slot || !sameViewerLease(slot.lease, lease) || slot.close) return false;
  slot.close = closeUpstream;
  return true;
}

function releaseVideoStream(sess, lease, closeUpstream = null) {
  const slot = sess.videoStreamCloser;
  if (slot && sameViewerLease(slot.lease, lease) && (!closeUpstream || slot.close === closeUpstream)) {
    sess.videoStreamCloser = null;
  }
}

function cancelRelayScaleDown() {
  if (!relayScaleDownTimer) return;
  clearTimeout(relayScaleDownTimer);
  relayScaleDownTimer = null;
}

async function scaleRelayToZero() {
  if (!RELAY_CLUSTER || !RELAY_SERVICE) return;
  if (relayScaleDownInflight) return relayScaleDownInflight;
  relayScaleDownInflight = ecs.send(new UpdateServiceCommand({
    cluster: RELAY_CLUSTER,
    service: RELAY_SERVICE,
    desiredCount: 0,
  })).catch(err => {
    logFailure("relay scale down failed", err);
  }).finally(() => {
    relayScaleDownInflight = null;
  });
  return relayScaleDownInflight;
}

async function activeRelaySessionCount() {
  if (!SESSION_TABLE) {
    let active = 0;
    for (const sess of sessions.values()) {
      if (!sess.draining) active += 1;
    }
    return active;
  }
  const now = Math.floor(Date.now() / 1000);
  async function countShard(shard) {
    let count = 0;
    let ExclusiveStartKey;
    do {
      const out = await ddb.send(new QueryCommand({
        TableName: SESSION_TABLE,
        IndexName: "GSI2",
        KeyConditionExpression: "#g = :active",
        FilterExpression: "#s = :running OR #w > :now",
        ExpressionAttributeNames: { "#g": "gsi2pk", "#s": "state", "#w": "relay_warm_until" },
        ExpressionAttributeValues: {
          ":active": { S: `RELAY#ACTIVE#${String(shard).padStart(2, "0")}` },
          ":running": { S: "RUNNING" }, ":now": { N: String(now) },
        },
        Select: "COUNT", ExclusiveStartKey,
      }));
      count += out.Count || 0;
      ExclusiveStartKey = out.LastEvaluatedKey;
    } while (ExclusiveStartKey);
    return count;
  }
  const counts = await Promise.all(Array.from({ length: RELAY_ACTIVE_SHARDS }, (_, shard) => countShard(shard)));
  return counts.reduce((total, value) => total + value, 0);
}

async function scaleRelayToZeroIfIdle() {
  // RELAY_WARM_SECONDS policy: -1 = always-on (never scale down); 0 = scale to zero as soon as the
  // relay is genuinely idle; N>0 = handled by the N-second timer in scheduleRelayScaleDown().
  // SAFETY: the historical objection is real — an eventually-consistent count could kill a live
  // session. So we scale down ONLY when the count read SUCCEEDS and is EXACTLY 0 AND this process
  // holds no live in-memory sessions. A stale/failed read (-1) or any active session aborts.
  if (RELAY_WARM_SECONDS < 0) {
    return { scaled: false, activeSessions: -1, reason: "always_on" };
  }
  if (sessions.size > 0) {
    return { scaled: false, activeSessions: sessions.size, reason: "local_sessions_live" };
  }
  let active;
  try {
    active = await activeRelaySessionCount();
  } catch (err) {
    logFailure("relay idle-count read failed; staying warm (fail-safe)", err);
    return { scaled: false, activeSessions: -1, reason: "count_read_failed" };
  }
  if (active !== 0) {
    return { scaled: false, activeSessions: active, reason: "sessions_active" };
  }
  await scaleRelayToZero();
  return { scaled: true, activeSessions: 0, reason: "idle" };
}

function scheduleRelayScaleDown() {
  cancelRelayScaleDown();
  if (RELAY_WARM_SECONDS < 0) return "kept_warm";           // always-on
  if (RELAY_WARM_SECONDS === 0) {
    // Immediate: try now (still gated on a genuine idle read inside scaleRelayToZeroIfIdle).
    scaleRelayToZeroIfIdle().catch(err => logFailure("immediate relay scale-down failed", err));
    return "scaling_to_zero";
  }
  // Warm for N seconds, then scale down if still idle.
  relayScaleDownTimer = setTimeout(() => {
    relayScaleDownTimer = null;
    scaleRelayToZeroIfIdle().catch(err => logFailure("scheduled relay scale-down failed", err));
  }, RELAY_WARM_SECONDS * 1000);
  return `warm_for_${RELAY_WARM_SECONDS}s`;
}

async function getVmState(microvmId) {
  const g = await mvm.send(new GetMicrovmCommand({ microvmIdentifier: microvmId }));
  return {
    state: g.state || "UNKNOWN",
    endpoint: g.endpoint || "",
    imageArn: g.imageArn || "",
    imageVersion: String(g.imageVersion || ""),
  };
}

async function requireRunning(claims) {
  const vm = await getVmState(claims.microvmId);
  if (vm.state !== "RUNNING") {
    const err = new Error(`microvm ${vm.state}`);
    err.statusCode = 503;
    throw err;
  }
  if (!ALLOW_UNSIGNED_DEV && (
    vm.imageArn !== claims.imageArn || vm.imageVersion !== claims.imageVersion
  )) {
    const err = new Error("microvm release identity mismatch");
    err.statusCode = 403;
    throw err;
  }
  return vm;
}

async function getUpstreamToken(sess) {
  const now = Date.now();
  if (sess.token && now < sess.tokenExp - 60_000) return sess.token;
  const t = await mvm.send(new CreateMicrovmAuthTokenCommand({
    microvmIdentifier: sess.claims.microvmId,
    expirationInMinutes: 10,
    allowedPorts: [{ allPorts: {} }],
  }));
  sess.token = t.authToken["X-aws-proxy-auth"];
  sess.tokenExp = Date.now() + 10 * 60 * 1000;
  return sess.token;
}

// Debug helper — plain HTTP GET to a VM loopback port via the aws-proxy hop, used by
// the PAIRPUTER_DEBUG-gated /vmdbg route to read capsule diagnostic logs.
async function upstreamHttpGet(sess, path, port, options = {}) {
  const vm = await requireRunning(sess.claims);
  const token = await getUpstreamToken(sess);
  const totalTimeoutMs = Math.max(250, Math.min(
    UPSTREAM_TOTAL_TIMEOUT_MS,
    Number(options.totalTimeoutMs) || UPSTREAM_TOTAL_TIMEOUT_MS,
  ));
  return new Promise((resolve, reject) => {
    const sock = tlsConnect(443, vm.endpoint, { servername: vm.endpoint }, () => {
      sock.write(
        `GET ${path} HTTP/1.1\r\nHost: ${vm.endpoint}\r\nConnection: close\r\n` +
        `X-aws-proxy-auth: ${token}\r\nX-aws-proxy-port: ${port}\r\n\r\n`,
      );
    });
    const chunks = [];
    let size = 0;
    const deadline = setTimeout(() => {
      try { sock.destroy(); } catch {}
      reject(new Error("upstream total timeout"));
    }, totalTimeoutMs);
    sock.on("data", c => {
      size += c.length;
      if (size > MAX_UPSTREAM_HTTP_BYTES) {
        clearTimeout(deadline);
        try { sock.destroy(); } catch {}
        reject(new Error("upstream response exceeds bounded size"));
        return;
      }
      chunks.push(c);
    });
    sock.on("end", () => {
      clearTimeout(deadline);
      const data = Buffer.concat(chunks, size);
      const idx = data.indexOf("\r\n\r\n");
      resolve(idx >= 0 ? data.slice(idx + 4).toString("utf8") : data.toString("utf8"));
    });
    sock.on("error", err => { clearTimeout(deadline); reject(err); });
    sock.setTimeout(Math.min(10_000, totalTimeoutMs), () => {
      clearTimeout(deadline);
      try { sock.destroy(); } catch {}
      reject(new Error("upstream inactivity timeout"));
    });
  });
}

// --- Runtime log shipping: pull the capsule's /dbg/* tail and PutLogEvents to CloudWatch -------------
// State per (microvmId:source): the byte offset already shipped + whether the log stream exists yet.
const _logShipState = new Map();   // "vmId:key" -> { offset }
const _logGroupsEnsured = new Set();
const _logShipInflight = new Set();

function capsuleLogGroup(imageId) {
  // A relay-owned group, distinct from the MicroVM image's build/Ready group. Sanitize the id for the
  // CloudWatch group charset ([.\-_/#A-Za-z0-9]).
  const safe = String(imageId || "unknown").replace(/[^A-Za-z0-9._/#-]/g, "_");
  return `/pairputer/capsule-runtime/${safe}`;
}

async function ensureLogTarget(group, stream) {
  if (!_logGroupsEnsured.has(group)) {
    try { await cwl.send(new CreateLogGroupCommand({ logGroupName: group })); } catch (e) {
      if (e.name !== "ResourceAlreadyExistsException") throw e;
    }
    _logGroupsEnsured.add(group);
  }
  try { await cwl.send(new CreateLogStreamCommand({ logGroupName: group, logStreamName: stream })); } catch (e) {
    if (e.name !== "ResourceAlreadyExistsException") throw e;
  }
}

async function shipSessionLogs(sess) {
  const claims = sess.claims;
  if (!claims || !claims.microvmId) return;
  const group = capsuleLogGroup(claims.imageId);
  for (const src of CAPSULE_LOG_SOURCES) {
    const stateKey = `${claims.microvmId}:${src.key}`;
    const st = _logShipState.get(stateKey) || { offset: 0 };
    let payload;
    try {
      const raw = await upstreamHttpGet(sess, `${src.path}?offset=${st.offset}`, 9000);
      payload = JSON.parse(raw);
    } catch { continue; }               // VM not reachable / not this capsule's file — skip quietly
    if (!payload || typeof payload.size !== "number") continue;
    if (payload.offset === 0 && st.offset !== 0) st.offset = 0;   // rotation/truncate -> resync
    const text = payload.data || "";
    if (!text) { st.offset = payload.size; _logShipState.set(stateKey, st); continue; }
    const lines = text.split("\n").filter(l => l.length > 0);
    if (lines.length) {
      const now = Date.now();
      const events = lines.slice(-8000).map((l, i) => ({ timestamp: now + i, message: l.slice(0, 8000) }));
      const stream = `${src.key}/${claims.microvmId}`;
      try {
        await ensureLogTarget(group, stream);
        await cwl.send(new PutLogEventsCommand({ logGroupName: group, logStreamName: stream, logEvents: events }));
        st.offset = payload.size;       // advance ONLY after a successful put (don't drop lines on failure)
      } catch (e) {
        if (DEBUG) logFailure("relay log shipping failed", e);
        // leave offset as-is; retry the same range next tick
      }
    } else {
      st.offset = payload.size;
    }
    _logShipState.set(stateKey, st);
  }
}

if (SHIP_LOGS) {
  setInterval(() => {
    for (const sess of sessions.values()) {
      // Best-effort, per session, non-blocking — a slow/dead VM must never stall the loop.
      if (_logShipInflight.has(sess.key)) continue;
      _logShipInflight.add(sess.key);
      shipSessionLogs(sess).catch(() => {}).finally(() => _logShipInflight.delete(sess.key));
    }
  }, LOG_SHIP_INTERVAL_MS);
}

function wsTextFrame(str) {
  const data = Buffer.from(str, "utf8");
  const mask = crypto.randomBytes(4);
  const masked = Buffer.alloc(data.length);
  for (let i = 0; i < data.length; i += 1) masked[i] = data[i] ^ mask[i & 3];
  let header;
  if (data.length < 126) {
    header = Buffer.from([0x81, 0x80 | data.length]);
  } else if (data.length < 65536) {
    header = Buffer.alloc(4);
    header[0] = 0x81;
    header[1] = 0xfe;
    header.writeUInt16BE(data.length, 2);
  } else {
    header = Buffer.alloc(10);
    header[0] = 0x81;
    header[1] = 0xff;
    header.writeBigUInt64BE(BigInt(data.length), 2);
  }
  return Buffer.concat([header, mask, masked]);
}

async function openUpstream(sess, wsPath, port, onPayload, onClose, options = {}) {
  const vm = await requireRunning(sess.claims);
  const token = await getUpstreamToken(sess);
  const safeImageId = String(sess.claims.imageId || "capsule").replace(/[^A-Za-z0-9._-]/g, "_").slice(0, 80);
  const upstreamLabel = `${safeImageId}:${port}${wsPath}`;
  const sock = tlsConnect(443, vm.endpoint, { servername: vm.endpoint }, () => {
    const key = crypto.randomBytes(16).toString("base64");
    sock.write(
      `GET ${wsPath} HTTP/1.1\r\nHost: ${vm.endpoint}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n` +
      `Sec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n` +
      `X-aws-proxy-auth: ${token}\r\nX-aws-proxy-port: ${port}\r\n\r\n`,
    );
  });

  let upgraded = false;
  let headerBuf = Buffer.alloc(0);
  let buf = Buffer.alloc(0);
  let closed = false;
  let sawPayload = false;
  let readyResolve;
  let readyReject;
  let readySettled = false;
  let abortListener = null;
  let deliveryActive = false;
  let pendingPayloadBytes = 0;
  const pendingPayloads = [];
  const sendWaiters = new Set();
  const ready = new Promise((resolve, reject) => {
    readyResolve = resolve;
    readyReject = reject;
  });
  const upstreamUnavailable = () => Object.assign(new Error("upstream unavailable"), { statusCode: 502 });
  const close = (reason = "downstream_close") => {
    if (closed) return;
    closed = true;
    if (abortListener && options.signal) options.signal.removeEventListener("abort", abortListener);
    if (DEBUG || (!sawPayload && reason !== "downstream_close")) {
      console.warn(`[upstream ${upstreamLabel}] closed`, {
        reason,
        upgraded,
        sawPayload,
      });
    }
    if (!readySettled) {
      readySettled = true;
      readyReject(upstreamUnavailable());
    }
    for (const waiter of sendWaiters) waiter.reject(upstreamUnavailable());
    sendWaiters.clear();
    try { sock.destroy(); } catch {}
    try { onClose && onClose(); } catch {}
  };
  if (options.signal) {
    abortListener = () => close("downstream_abort");
    if (options.signal.aborted) abortListener();
    else options.signal.addEventListener("abort", abortListener, { once: true });
  }

  sock.on("data", chunk => {
    if (!upgraded) {
      headerBuf = Buffer.concat([headerBuf, chunk]);
      if (headerBuf.length > MAX_UPSTREAM_WS_HEADER_BYTES) {
        close("handshake_header_too_large");
        return;
      }
      const idx = headerBuf.indexOf("\r\n\r\n");
      if (idx < 0) return;
      const head = headerBuf.slice(0, idx).toString("utf8");
      if (!/^HTTP\/1\.[01] 101\b/.test(head)) {
        close("handshake_rejected");
        return;
      }
      upgraded = true;
      if (!readySettled) {
        readySettled = true;
        readyResolve();
      }
      if (DEBUG) console.log(`[upstream ${upstreamLabel}] upgraded`);
      buf = headerBuf.slice(idx + 4);
      headerBuf = Buffer.alloc(0);
    } else {
      buf = Buffer.concat([buf, chunk]);
      if (buf.length > MAX_UPSTREAM_WS_BUFFER_BYTES) {
        close("buffer_too_large");
        return;
      }
    }
    while (buf.length >= 2) {
      const op = buf[0] & 0x0f;
      let len = buf[1] & 0x7f;
      let off = 2;
      if (len === 126) {
        if (buf.length < 4) break;
        len = buf.readUInt16BE(2);
        off = 4;
      } else if (len === 127) {
        if (buf.length < 10) break;
        const wideLength = buf.readBigUInt64BE(2);
        if (wideLength > BigInt(MAX_UPSTREAM_WS_FRAME_BYTES)) {
          close("frame_too_large");
          return;
        }
        len = Number(wideLength);
        off = 10;
      }
      if (len > MAX_UPSTREAM_WS_FRAME_BYTES) {
        close("frame_too_large");
        return;
      }
      if (buf.length < off + len) break;
      const payload = buf.slice(off, off + len);
      buf = buf.slice(off + len);
      if (op === 0x8) {
        close("upstream_close_frame");
        return;
      }
      if (op === 0x1 || op === 0x2) {
        sawPayload = true;
        if (deliveryActive) {
          onPayload(payload);
        } else {
          pendingPayloadBytes += payload.length;
          if (pendingPayloadBytes > MAX_UPSTREAM_WS_BUFFER_BYTES) {
            close("pending_payloads_too_large");
            return;
          }
          pendingPayloads.push(payload);
        }
      }
    }
  });
  sock.setTimeout(15_000, () => close("upstream_inactivity_timeout"));
  sock.on("close", () => close("upstream_socket_closed"));
  sock.on("error", () => close("upstream_socket_error"));
  await ready;
  return {
    close,
    isClosed: () => closed,
    pause: () => {
      if (!closed) sock.pause();
    },
    resume: () => {
      if (!closed) sock.resume();
    },
    activate: () => {
      if (closed) throw upstreamUnavailable();
      deliveryActive = true;
      for (const payload of pendingPayloads.splice(0)) onPayload(payload);
      pendingPayloadBytes = 0;
    },
    sendText: str => {
      if (closed || !upgraded) return Promise.reject(upstreamUnavailable());
      const frame = wsTextFrame(str);
      return new Promise((resolve, reject) => {
        const waiter = { reject };
        sendWaiters.add(waiter);
        try {
          sock.write(frame, () => {
            if (!sendWaiters.delete(waiter)) return;
            if (closed) reject(upstreamUnavailable());
            else resolve(true);
          });
        } catch {
          sendWaiters.delete(waiter);
          reject(upstreamUnavailable());
        }
      });
    },
  };
}

async function ensureInputUpstream(sess) {
  if (sess.inputUpstream) return sess.inputUpstream;
  if (sess.inputInflight) return sess.inputInflight;
  sess.inputInflight = openUpstream(sess, "/pairputer/input", INPUT_PORT, () => {}, () => {
    sess.inputUpstream = null;
    sess.inputInflight = null;
  }).then(up => {
    if (up.isClosed()) throw Object.assign(new Error("upstream unavailable"), { statusCode: 502 });
    sess.inputUpstream = up;
    sess.inputInflight = null;
    return up;
  }).catch(err => {
    sess.inputInflight = null;
    throw err;
  });
  return sess.inputInflight;
}

function validInputEvent(event) {
  if (!event || typeof event !== "object") return false;
  if (event.t === "k") return typeof event.key === "string" && typeof event.down === "boolean";
  if (event.t === "b") return Number.isInteger(event.button) && typeof event.down === "boolean";
  if (event.t === "m") return Number.isInteger(event.x) && Number.isInteger(event.y);
  return false;
}

async function sendInput(sess, event) {
  const upstream = await ensureInputUpstream(sess);
  try {
    await upstream.sendText(JSON.stringify(event));
  } catch (err) {
    if (sess.inputUpstream === upstream) sess.inputUpstream = null;
    try { upstream.close(); } catch {}
    throw err;
  }
  return true;
}

async function releaseHeld(sess, heldKeys, heldButtons) {
  const events = [];
  for (const key of heldKeys) events.push({ t: "k", down: false, key });
  for (const button of heldButtons) events.push({ t: "b", down: false, button });
  if (!events.length) return;
  for (const event of events) await sendInput(sess, event);
  heldKeys.clear();
  heldButtons.clear();
}

function readBody(req, maxBytes) {
  return new Promise((resolve, reject) => {
    let body = "";
    let bytes = 0;
    req.on("data", chunk => {
      bytes += chunk.length;
      if (bytes > maxBytes) {
        reject(Object.assign(new Error("input body too large"), { statusCode: 413 }));
        req.destroy();
        return;
      }
      body += chunk;
    });
    req.on("end", () => resolve(body));
    req.on("error", reject);
  });
}

// Codex's widget CSP allows only https:// on connect-src, not wss:// (proven in v1: see blog.md).
// A raw browser WebSocket to /input is silently refused before it ever reaches this relay, so input
// rides the same HTTPS transport as the rest of the widget: one POST per input event/batch.
async function handleInputPost(req, res, reqUrl) {
  const claims = await authorize(reqUrl, "input");
  if (!claims) {
    text(res, 403, "forbidden: valid relay session required");
    return;
  }
  const sess = getSession(claims);
  const requestViewer = viewerIdFrom(reqUrl);
  if (requestViewer.present && !requestViewer.valid) {
    text(res, 400, "invalid viewer id");
    return;
  }
  if (claims.frozen || sess.draining) {
    text(res, 409, "frozen");
    return;
  }
  const requestAbort = new AbortController();
  req.once("aborted", () => requestAbort.abort());
  const viewerLease = await claimViewer(sess, requestViewer, { signal: requestAbort.signal });
  if (!viewerLease.ok) {
    text(res, viewerLease.statusCode || 409, viewerLease.reason);
    return;
  }
  try {
    await requireRunning(claims);
  } catch {
    text(res, 503, "microvm unavailable");
    return;
  }

  let bodyText;
  try {
    bodyText = await readBody(req, 4096);
  } catch (err) {
    const statusCode = safeStatusCode(err, 400);
    text(res, statusCode, publicRelayError(statusCode));
    return;
  }
  let events;
  try {
    const parsed = JSON.parse(bodyText || "[]");
    events = Array.isArray(parsed) ? parsed : [parsed];
  } catch {
    text(res, 400, "invalid JSON");
    return;
  }
  if (events.length > 32) {
    text(res, 400, "too many events");
    return;
  }
  if (!events.every(validInputEvent)) {
    text(res, 400, "invalid input event");
    return;
  }

  sess.lastActive = Date.now();
  const mouseEvents = events.filter(event => event.t === "m").length;
  try {
    await withSessionLock(sess, "inputOperationInflight", async () => {
      if (!ownsViewerLease(sess, viewerLease.lease)) throw Object.assign(new Error("superseded"), { statusCode: 409 });
      if (sess.draining) throw Object.assign(new Error("draining"), { statusCode: 409 });
      const now = Date.now();
      if (now - sess.postInputWindowStart >= 1000) {
        sess.postInputWindowStart = now;
        sess.postInputEventCount = 0;
        sess.postInputMouseCount = 0;
      }
      if (sess.postInputEventCount + events.length > 240
          || sess.postInputMouseCount + mouseEvents > 60) {
        throw Object.assign(new Error("input rate limit"), { statusCode: 429 });
      }
      const state = getViewerInputState(sess, viewerLease.lease);
      try {
        for (const event of events) {
          if (!ownsViewerLease(sess, viewerLease.lease)) {
            throw Object.assign(new Error("superseded"), { statusCode: 409 });
          }
          await sendInput(sess, event);
          if (event.t === "k") {
            if (event.down) state.heldKeys.add(event.key);
            else state.heldKeys.delete(event.key);
          } else if (event.t === "b") {
            if (event.down) state.heldButtons.add(event.button);
            else state.heldButtons.delete(event.button);
          }
        }
      } catch (err) {
        try { await releaseHeld(sess, state.heldKeys, state.heldButtons); } catch {}
        throw err;
      }
      sess.postInputEventCount += events.length;
      sess.postInputMouseCount += mouseEvents;
    });
  } catch (err) {
    const statusCode = safeStatusCode(err, 502);
    text(res, statusCode, publicRelayError(statusCode));
    return;
  }
  res.writeHead(204, { "access-control-allow-origin": "*" });
  res.end();
}

async function drainSession(sess) {
  sess.draining = true;
  let releaseFailed = false;
  await withSessionLock(sess, "inputOperationInflight", async () => {
    for (const state of sess.viewerInputState.values()) {
      try {
        await releaseHeld(sess, state.heldKeys, state.heldButtons);
      } catch {
        releaseFailed = true;
      }
    }
  });
  if (releaseFailed) {
    sess.draining = false;
    throw Object.assign(new Error("input release failed"), { statusCode: 502 });
  }
  for (const ws of sess.inputClients) {
    try { ws.close(1001, "draining"); } catch {}
  }
  sess.inputClients.clear();
  for (const subscriber of [...sess.eventSubscribers]) closeEventSubscriber(sess, subscriber);
  sess.viewerInputState.clear();
  for (const close of sess.streamClosers) {
    try { close(); } catch {}
  }
  sess.streamClosers.clear();
  sess.audioStreamCloser = null;
  sess.videoStreamCloser = null;
  if (sess.inputUpstream) {
    try { sess.inputUpstream.close(); } catch {}
  }
  sess.inputUpstream = null;
  sess.inputInflight = null;
}

async function drainTenantMicrovmSessions(claims) {
  const draining = [];
  for (const sess of sessions.values()) {
    if (
      sess.claims
      && sess.claims.tenantId === claims.tenantId
      && sess.claims.microvmId === claims.microvmId
    ) {
      draining.push(drainSession(sess));
    }
  }
  await Promise.all(draining);
}

function writeEventSubscriber(sess, subscriber, frame) {
  if (subscriber.closed
      || subscriber.res.destroyed
      || subscriber.res.writableEnded
      || !ownsViewerLease(sess, subscriber.lease)
      || subscriber.res.writableLength > MAX_EVENT_BUFFER_BYTES) {
    closeEventSubscriber(sess, subscriber);
    return false;
  }
  try {
    if (!subscriber.res.write(frame)) {
      closeEventSubscriber(sess, subscriber);
      return false;
    }
    subscriber.lastWriteAt = Date.now();
    return true;
  } catch {
    closeEventSubscriber(sess, subscriber);
    return false;
  }
}

function fanoutEvent(sess, eventName, data) {
  const frame = `event: ${eventName}\ndata: ${data}\n\n`;
  for (const subscriber of [...sess.eventSubscribers]) {
    if (eventName === "state" && subscriber.lastBody === data) continue;
    if (writeEventSubscriber(sess, subscriber, frame) && eventName === "state") {
      subscriber.lastBody = data;
    }
  }
}

async function pollSessionEvents(sess) {
  try {
    while (sess.eventSubscribers.size > 0 && !sess.draining) {
      const now = Date.now();
      for (const subscriber of [...sess.eventSubscribers]) {
        const expiresAt = Number(subscriber.claims.exp || 0) * 1000;
        if ((expiresAt && now >= expiresAt) || !ownsViewerLease(sess, subscriber.lease)) {
          closeEventSubscriber(sess, subscriber);
        }
      }
      if (!sess.eventSubscribers.size) break;

      if (now - sess.eventLastFreshnessCheckAt >= STREAM_REVALIDATE_MS) {
        sess.eventLastFreshnessCheckAt = now;
        let current;
        try {
          current = await loadActiveSessionCoalesced(sess.claims);
        } catch {
          for (const subscriber of [...sess.eventSubscribers]) closeEventSubscriber(sess, subscriber);
          break;
        }
        if (!sessionClaimsFresh(sess.claims, current)) {
          for (const subscriber of [...sess.eventSubscribers]) closeEventSubscriber(sess, subscriber);
          break;
        }
      }

      try {
        const body = await upstreamHttpGet(sess, "/", COPLAY_PORT, {
          totalTimeoutMs: STREAM_REVALIDATE_MS - EVENT_POLL_INTERVAL_MS,
        });
        const normalized = JSON.stringify(JSON.parse(body));
        if (Buffer.byteLength(normalized, "utf8") > MAX_EVENT_PAYLOAD_BYTES) {
          throw Object.assign(new Error("event payload too large"), { statusCode: 502 });
        }
        sess.eventLastBody = normalized;
        sess.eventLastBodyAt = Date.now();
        fanoutEvent(sess, "state", normalized);
      } catch {
        if (Date.now() - sess.eventLastErrorAt >= STREAM_REVALIDATE_MS) {
          sess.eventLastErrorAt = Date.now();
          fanoutEvent(sess, "upstream-error", JSON.stringify({ error: "upstream unavailable" }));
        }
      }

      const afterPoll = Date.now();
      for (const subscriber of [...sess.eventSubscribers]) {
        if (afterPoll - subscriber.lastWriteAt >= EVENT_HEARTBEAT_MS) {
          writeEventSubscriber(sess, subscriber, ": keepalive\n\n");
        }
      }
      await new Promise(resolve => setTimeout(resolve, EVENT_POLL_INTERVAL_MS));
    }
  } finally {
    sess.eventPoller = null;
    if (sess.eventSubscribers.size > 0 && !sess.draining) queueMicrotask(() => ensureEventPoller(sess));
  }
}

function ensureEventPoller(sess) {
  if (sess.eventPoller) return;
  sess.eventPoller = pollSessionEvents(sess).catch(() => {
    for (const subscriber of [...sess.eventSubscribers]) closeEventSubscriber(sess, subscriber);
  });
}

async function handleEvents(req, res, reqUrl) {
  const claims = await authorize(reqUrl, "state");
  if (!claims) { text(res, 403, "forbidden"); return; }
  const sess = getSession(claims);
  if (sess.draining) { text(res, 409, "frozen"); return; }
  const requestAbort = new AbortController();
  req.once("aborted", () => requestAbort.abort());
  const viewerLease = await claimViewer(sess, viewerIdFrom(reqUrl), { signal: requestAbort.signal });
  if (!viewerLease.ok) {
    text(res, viewerLease.statusCode || 409, viewerLease.reason);
    return;
  }
  if (requestAbort.signal.aborted || req.destroyed || !ownsViewerLease(sess, viewerLease.lease)) {
    text(res, 409, "viewer superseded");
    return;
  }
  if (sess.eventSubscribers.size >= MAX_EVENT_SUBSCRIBERS) {
    text(res, 429, "event subscriber limit reached");
    return;
  }

  const subscriber = {
    req,
    res,
    claims,
    lease: viewerLease.lease,
    closed: false,
    lastBody: null,
    lastWriteAt: Date.now(),
  };
  sess.eventSubscribers.add(subscriber);
  res.writeHead(200, {
    "content-type": "text/event-stream; charset=utf-8",
    "cache-control": "no-store, no-transform",
    "connection": "keep-alive",
    "x-accel-buffering": "no",
  });
  writeEventSubscriber(sess, subscriber, "retry: 1000\n\n");
  if (sess.eventLastBody
      && Date.now() - sess.eventLastBodyAt <= STREAM_REVALIDATE_MS
      && Buffer.byteLength(sess.eventLastBody, "utf8") <= MAX_EVENT_PAYLOAD_BYTES) {
    if (writeEventSubscriber(sess, subscriber, `event: state\ndata: ${sess.eventLastBody}\n\n`)) {
      subscriber.lastBody = sess.eventLastBody;
    }
  }
  const close = () => closeEventSubscriber(sess, subscriber);
  req.once("aborted", close);
  res.once("close", close);
  ensureEventPoller(sess);
}

function playerHtml() {
  return `<!doctype html><meta charset=utf-8><title>pairputer player</title>
<style>html,body{margin:0;background:#000;height:100%;overflow:hidden}
#c{position:absolute;inset:0;width:100%;height:100%;object-fit:contain;image-rendering:pixelated}
#s{position:absolute;left:6px;bottom:4px;color:#6a6;font:11px ui-monospace,monospace;opacity:.7}</style>
<canvas id=c width=1280 height=720 tabindex=0></canvas><div id=s>ready</div>
<script>
const canvas=document.getElementById('c'),ctx=canvas.getContext('2d'),S=m=>document.getElementById('s').textContent=m;
let vidW=1280,vidH=720,started=false,lastFrameAt=0,connection='starting';
let inputOpen=false;
let streamRestartTimer=null;
const tell=(t,x)=>{try{parent.postMessage(Object.assign({pairputer:t},x||{}),'*')}catch(e){}};
const params=new URLSearchParams(location.search);
let TOK=params.get('t')||'';
const VIEWER_ID=params.get('viewer')||(crypto.randomUUID?crypto.randomUUID():Array.from(crypto.getRandomValues(new Uint8Array(16)),b=>b.toString(16).padStart(2,'0')).join(''));
const EDGE_KEYS=['Policy','Signature','Key-Pair-Id','Expires','Hash-Algorithm'];
let EDGE_AUTH=(()=>{const q=new URLSearchParams();for(const k of EDGE_KEYS){const v=params.get(k);if(v)q.set(k,v);}return q.toString();})();
const AUTOSTART=params.get('autostart')!=='0';
function signedQuery(){const q=new URLSearchParams();q.set('t',TOK);q.set('viewer',VIEWER_ID);if(EDGE_AUTH){const e=new URLSearchParams(EDGE_AUTH);e.forEach((v,k)=>q.set(k,v));}return q.toString();}
const U=p=>p+(p.indexOf('?')<0?'?':'&')+signedQuery();
const delay=ms=>new Promise(r=>setTimeout(r,ms));
function relayTokenExpired(){try{const b=TOK.split('.')[0].replace(/-/g,'+').replace(/_/g,'/');const p=JSON.parse(atob(b.padEnd(Math.ceil(b.length/4)*4,'=')));return Number(p.exp||0)*1000<=Date.now()+1000;}catch(e){return false;}}
function reply(id,ok,result,error){if(id)tell('reply',{id,ok,result,error});}
async function jsonFetch(p){
  const r=await fetch(U(p),{cache:'no-store'});
  const text=await r.text();
  let body={};
  try{body=text?JSON.parse(text):{};}catch{}
  if(!r.ok)throw new Error('HTTP '+r.status+' '+(body.error||text||r.statusText||'relay error'));
  return body;
}
// Codex's widget CSP allows only https:// on connect-src, not wss:// (proven in v1). Input rides
// the same HTTPS transport as everything else: batched POSTs, not a browser WebSocket.
let inputQueue=[],inputFlushTimer=null,inputInflight=false;
function closeInput(){
  inputOpen=false;
  if(inputFlushTimer){clearTimeout(inputFlushTimer);inputFlushTimer=null;}
  inputQueue=[];
}
function openInput(){
  inputOpen=true;tell('status',{s:'input ready'});
}
function flushInput(){
  inputFlushTimer=null;
  if(!inputQueue.length||inputInflight)return;
  const batch=inputQueue;inputQueue=[];
  inputInflight=true;
  fetch(U('/input'),{method:'POST',body:JSON.stringify(batch),cache:'no-store'})
    .catch(()=>{})
    .finally(()=>{inputInflight=false;if(inputQueue.length)scheduleFlush();});
}
function scheduleFlush(){
  if(inputFlushTimer)return;
  inputFlushTimer=setTimeout(flushInput,16);
}
function wsSend(o){
  if(!inputOpen)return;
  inputQueue.push(o);
  if(inputQueue.length>32)inputQueue.shift();
  scheduleFlush();
}
// ---- video: H.264 SSE -> WebCodecs -> canvas ----
function startVideo(){
  if(window._v)return;
  if(!('VideoDecoder'in window)){S('needs WebCodecs (Chromium)');return;}
  lastFrameAt=0;
  let frames=0,lastFrames=0,bytes=0,sawKey=false,vts=0,lastTick=performance.now(),streamStartedAt=lastTick;
  const dec=new VideoDecoder({output:f=>{const w=f.displayWidth||f.codedWidth,h=f.displayHeight||f.codedHeight;
    if(w&&h&&(vidW!==w||vidH!==h||canvas.width!==w||canvas.height!==h)){
      vidW=w;vidH=h;canvas.width=w;canvas.height=h;tell('display',{encodedWidth:w,encodedHeight:h});}
    ctx.drawImage(f,0,0,canvas.width,canvas.height);f.close();frames++;lastFrameAt=performance.now();if(frames===1)tell('playing');},
    error:e=>S('decode: '+e.message)});
  window._vd=dec;
  dec.configure({codec:'avc1.42E01F',optimizeForLatency:true,hardwareAcceleration:'no-preference'});
  const es=new EventSource(U('/video'));window._v=es;
  es.onopen=()=>{S('connected - waiting for keyframe');tell('status',{s:'video SSE open'});};
  es.onerror=()=>{if(parent===window&&relayTokenExpired()){stopStreams();S('session expired · reconnect to continue');return;}
    // KEY jitter fix: if frames have EVER arrived, this is a mid-stream hiccup — let EventSource
    // auto-reconnect quietly on the SAME still-valid signed URL. Do NOT re-mint the token or tear
    // down the decoder (that fought the relay's viewer handoff and caused a reconnect storm). Only a
    // channel that NEVER delivered a byte is a true 403/expired-URL that needs a fresh token.
    if(bytes>0){tell('status',{s:'reconnecting…'});return;}
    connection='terrible';S('connecting…');tell('status',{s:'connecting…'});
    // Zero bytes ever = expired CloudFront edgeAuth / rotated relay token. EventSource hides the HTTP
    // status, so ask the parent widget to re-mint (debounced). The widget replies with cmd:'setToken'.
    const now=Date.now();if(now-(window._lastNeedTok||0)>5000){window._lastNeedTok=now;tell('needtoken',{});}};
  es.onmessage=ev=>{const raw=Uint8Array.from(atob(ev.data),c=>c.charCodeAt(0)),key=raw[0]===1,au=raw.subarray(1);
    bytes+=au.length;if(dec.state!=='configured')return;if(!sawKey){if(!key)return;sawKey=true;}
    try{dec.decode(new EncodedVideoChunk({type:key?'key':'delta',timestamp:vts,data:au}))}catch(e){}vts+=33333;};
  if(window._statusTimer)clearInterval(window._statusTimer);
  window._statusTimer=setInterval(()=>{
    const now=performance.now(),dt=Math.max(1,now-lastTick),fps=(frames-lastFrames)*1000/dt;
    lastTick=now;lastFrames=frames;
    // Stale = no frames for a while. Raised to 8s (from 3s) so normal jitter doesn't trigger a full
    // teardown; a short blip just shows "buffering", not "terrible/reconnecting".
    const gap=lastFrameAt?now-lastFrameAt:now-streamStartedAt;
    const stale=lastFrameAt?gap>8000:gap>10000;
    const soft=lastFrameAt&&gap>2000&&gap<=8000;  // brief hiccup — inform, don't restart
    connection=stale?'terrible':soft?'buffering':fps>=28?'excellent':fps>=24?'good':fps>=18?'okay':fps>=10?'bad':'okay';
    const line=stale?'reconnecting…':soft?'buffering…':'FPS '+fps.toFixed(1)+' · connection '+connection;
    if(frames||stale){S(line);tell('status',{s:line});}
    // Backoff on repeated restarts so a marginal link settles instead of thrashing: 8s, then 16s, 32s…
    const backoff=Math.min(32000,8000*Math.pow(2,window._staleRestarts||0));
    if(stale&&now-(window._lastStaleRestart||0)>backoff){
      window._lastStaleRestart=now;window._staleRestarts=(window._staleRestarts||0)+1;
      // EventSource can remain OPEN while an upstream guest encoder/socket has gone silent (onerror
      // never fires). Actively replace the same-viewer channels after this bounded deadline. The
      // relay's atomic handoff prevents overlap. A run of good frames resets the backoff (below).
      stopStreams();streamRestartTimer=setTimeout(()=>{streamRestartTimer=null;startStreams();},250);return;
    }
    if(fps>=18)window._staleRestarts=0;  // healthy stream → reset restart backoff
  },1000);
}
// ---- audio: Opus SSE -> WebCodecs AudioDecoder -> AudioContext ----
let audioCtx=null,audioGain=null,muted=false;
function startAudio(){
  if(window._a||!('AudioDecoder'in window))return;
  audioCtx=new(window.AudioContext||window.webkitAudioContext)({sampleRate:48000});
  audioGain=audioCtx.createGain();audioGain.gain.value=muted?0:1;audioGain.connect(audioCtx.destination);
  let nextTime=0,configured=false,ats=0;
  const dec=new AudioDecoder({output:ad=>{const ch=ad.numberOfChannels,n=ad.numberOfFrames,sr=ad.sampleRate;
    const buf=audioCtx.createBuffer(ch,n,sr);for(let c=0;c<ch;c++){const tt=new Float32Array(n);ad.copyTo(tt,{planeIndex:c,format:'f32-planar'});buf.copyToChannel(tt,c);}ad.close();
    nextTime=Math.max(audioCtx.currentTime,nextTime);if(nextTime-audioCtx.currentTime>0.2)nextTime=audioCtx.currentTime;
    const sx=audioCtx.createBufferSource();sx.buffer=buf;sx.connect(audioGain);sx.start(nextTime);nextTime+=buf.duration;},
    error:e=>{}});
  window._ad=dec;
  const isHead=b=>b.length>=8&&b[0]===0x4f&&b[1]===0x70&&b[2]===0x75&&b[3]===0x73;
  const es=new EventSource(U('/audio'));window._a=es;
  es.onmessage=ev=>{const b=Uint8Array.from(atob(ev.data),c=>c.charCodeAt(0));
    if(isHead(b)){if(!configured){dec.configure({codec:'opus',sampleRate:48000,numberOfChannels:b[9]||2});configured=true;}return;}
    if(!configured){dec.configure({codec:'opus',sampleRate:48000,numberOfChannels:2});configured=true;}
    if(dec.state!=='configured')return;try{dec.decode(new EncodedAudioChunk({type:'key',timestamp:ats,data:b}))}catch(e){}ats+=20000;};
}
// ---- realtime input: batched HTTPS POST -> persistent relay upstream input WS ----
const MAP={KeyW:'ArrowUp',KeyS:'ArrowDown',KeyA:'ArrowLeft',KeyD:'ArrowRight'};
const keyName=e=>MAP[e.code]||e.key,SW=new Set([' ','ArrowUp','ArrowDown','ArrowLeft','ArrowRight','Tab','Control']),held=new Set(),buttons=new Set();
function onKey(down,e){const k=MAP[e.code]||e.key;if(down){if(!held.has(k)){held.add(k);wsSend({t:'k',down:true,key:k});}}else{if(held.delete(k))wsSend({t:'k',down:false,key:k});}}
window.addEventListener('keydown',e=>{onKey(true,e);if(SW.has(keyName(e)))e.preventDefault();});
window.addEventListener('keyup',e=>{onKey(false,e);if(SW.has(keyName(e)))e.preventDefault();});
function releaseAll(){
  for(const k of held)wsSend({t:'k',down:false,key:k});held.clear();
  for(const b of buttons)wsSend({t:'b',button:b,down:false});buttons.clear();
}
window.addEventListener('blur',releaseAll);
document.addEventListener('visibilitychange',()=>{if(document.hidden)releaseAll();});
let _mmLast=0,_mmPending=null;
canvas.addEventListener('mousemove',e=>{const box=canvas.getBoundingClientRect(),scale=Math.min(box.width/vidW,box.height/vidH);
  const rw=vidW*scale,rh=vidH*scale,left=box.left+(box.width-rw)/2,top=box.top+(box.height-rh)/2;
  _mmPending={t:'m',x:Math.max(0,Math.min(vidW-1,(e.clientX-left)/rw*vidW|0)),y:Math.max(0,Math.min(vidH-1,(e.clientY-top)/rh*vidH|0))};
  const now=performance.now();if(now-_mmLast>=50){_mmLast=now;wsSend(_mmPending);_mmPending=null;}});
setInterval(()=>{if(_mmPending){_mmLast=performance.now();wsSend(_mmPending);_mmPending=null;}},50);
canvas.addEventListener('mousedown',e=>{if(audioCtx&&audioCtx.state==='suspended')audioCtx.resume();canvas.focus();buttons.add(e.button);wsSend({t:'b',button:e.button,down:true});e.preventDefault();});
window.addEventListener('mouseup',e=>{if(buttons.delete(e.button))wsSend({t:'b',button:e.button,down:false});});
canvas.addEventListener('mouseup',e=>{if(buttons.delete(e.button))wsSend({t:'b',button:e.button,down:false});e.preventDefault();});
canvas.addEventListener('pointercancel',releaseAll);
canvas.addEventListener('mouseleave',e=>{if(e.buttons===0)releaseAll();});
canvas.addEventListener('contextmenu',e=>e.preventDefault());
function stopStreams(){
  if(streamRestartTimer){clearTimeout(streamRestartTimer);streamRestartTimer=null;}
  releaseAll();closeInput();
  try{window._v&&window._v.close()}catch(e){}
  try{window._a&&window._a.close()}catch(e){}
  try{window._vd&&window._vd.close()}catch(e){}
  try{window._ad&&window._ad.close()}catch(e){}
  try{audioCtx&&audioCtx.close&&audioCtx.close()}catch(e){}
  if(window._statusTimer)clearInterval(window._statusTimer);
  window._v=window._a=window._vd=window._ad=null;window._statusTimer=null;audioCtx=null;audioGain=null;started=false;
}
function startStreams(){if(started)return;started=true;openInput();startVideo();startAudio();canvas.focus();tell('status',{s:'RUNNING · streams open'});}
async function state(){return await jsonFetch('/state');}
async function freeze(){stopStreams();S('freezing...');return await jsonFetch('/drain');}
async function thaw(){S('thawing...');startStreams();tell('status',{s:'RUNNING · billing on'});return {state:'RUNNING'};}
window.addEventListener('message',async ev=>{const m=ev.data;if(!m)return;
  if(m.cmd==='setToken'){const nextToken=m.token||TOK,nextEdge=m.edgeAuth||EDGE_AUTH;
    if(nextToken===TOK&&nextEdge===EDGE_AUTH)return;
    const wasStarted=started;TOK=nextToken;EDGE_AUTH=nextEdge;
    // A live EventSource keeps using the URL it was opened with — updating TOK/EDGE_AUTH alone won't
    // reconnect it. If streams are up (or were retrying a dead 403 URL), restart them on the fresh
    // signed URL. Let the old same-viewer TLS channels close before reopening their guest slots.
    if(wasStarted){stopStreams();streamRestartTimer=setTimeout(()=>{streamRestartTimer=null;startStreams();},200);}
    return;}
  if(m.cmd==='setMuted'){muted=!!m.muted;if(audioGain)audioGain.gain.value=muted?0:1;if(!muted&&audioCtx&&audioCtx.state==='suspended')audioCtx.resume();return;}
  if(m.cmd==='mute'){muted=!muted;if(audioGain)audioGain.gain.value=muted?0:1;if(!muted&&audioCtx&&audioCtx.state==='suspended')audioCtx.resume();return;}
  if(m.cmd==='releaseAll'){releaseAll();reply(m.id,true,{released:true});return;}
  if(m.cmd==='stop'){stopStreams();reply(m.id,true,{stopped:true});return;}
  if(m.cmd==='start'){try{if(typeof m.muted==='boolean')muted=m.muted;startStreams();reply(m.id,true,{started:true});}catch(e){reply(m.id,false,null,e.message);}return;}
  if(m.cmd==='state'){try{reply(m.id,true,await state());}catch(e){reply(m.id,false,null,e.message);}return;}
  if(m.cmd==='freeze'){try{reply(m.id,true,await freeze());}catch(e){reply(m.id,false,null,e.message);}return;}
  if(m.cmd==='thaw'){try{if(typeof m.muted==='boolean')muted=m.muted;reply(m.id,true,await thaw());}catch(e){reply(m.id,false,null,e.message);}return;}
  if(!m.pairputerInput)return;const i=m.pairputerInput;
  if(i.t==='k'){if(i.down){if(!held.has(i.key)){held.add(i.key);wsSend(i);}}else{if(held.delete(i.key))wsSend(i);}}
  else if(i.t==='b'){if(i.down){buttons.add(i.button);wsSend(i);}else{if(buttons.delete(i.button))wsSend(i);}}
  else if(i.t==='m')wsSend(i);
  else wsSend(i);
});
if(AUTOSTART)startStreams();canvas.focus();tell('display',{encodedWidth:vidW,encodedHeight:vidH});tell('booted');
</script>`;
}

async function authorize(reqUrl, channel) {
  const token = reqUrl.searchParams.get("t") || "";
  const claims = await verifySessionToken(token);
  if (!claims || !hasChannel(claims, channel)) return null;
  let current;
  try {
    current = await loadActiveSessionCoalesced(claims);
  } catch (err) {
    logFailure("relay session freshness check failed", err);
    return null;
  }
  if (!sessionClaimsFresh(claims, current)) return null;
  // Refuse to admit a STREAM while the control plane is suspending this VM — opening an upstream
  // (video/audio/input/player) auto-resumes it and defeats the freeze. Scope this to stream channels
  // only: /drain is channel "control" and IS how freeze quiesces the VM (blocking it would make freeze
  // fail closed on its own drain), and "state" is a read that opens no upstream. Thaw flips state back
  // to RUNNING before any stream, so it's unaffected.
  if (STREAM_CHANNELS.has(channel) && sessionSuspending(current)) return null;
  return claims;
}

async function handleHttp(req, res) {
  const reqUrl = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
  if (req.method === "OPTIONS") {
    res.writeHead(204, {
      "access-control-allow-origin": "*",
      "access-control-allow-methods": "GET,POST,OPTIONS",
      "access-control-allow-headers": "*",
    });
    res.end();
    return;
  }
  if (reqUrl.pathname === "/healthz") {
    json(res, 200, { ok: true, sessions: sessions.size });
    return;
  }
  if (reqUrl.searchParams.get("probe") === "frame") {
    res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
    res.end("<!doctype html><meta charset=utf-8><body>relay frame loaded</body>");
    return;
  }

  if (reqUrl.pathname === "/input" && req.method === "POST") {
    await handleInputPost(req, res, reqUrl);
    return;
  }

  // Generic low-latency event channel. Capsules may expose richer cursor/task/display state from :6906;
  // the relay emits changed snapshots over SSE. The legacy /coplay polling route remains below as a
  // compatibility fallback for clients or hosts that cannot keep EventSource open.
  if (reqUrl.pathname === "/events" && req.method === "GET") {
    await handleEvents(req, res, reqUrl);
    return;
  }

  // Co-play coordination state (whose turn / ghost cursor). Read-only proxy of the capsule's
  // input arbiter on :6906 (input_ws). Capsule-agnostic — every streamed capsule serves it.
  if (reqUrl.pathname === "/coplay" && req.method === "GET") {
    const claims = await authorize(reqUrl, "state");
    if (!claims) { text(res, 403, "forbidden"); return; }
    try {
      const sess = getSession(claims);
      const requestAbort = new AbortController();
      req.once("aborted", () => requestAbort.abort());
      const viewerLease = await claimViewer(sess, viewerIdFrom(reqUrl), { signal: requestAbort.signal });
      if (!viewerLease.ok) {
        text(res, viewerLease.statusCode || 409, viewerLease.reason);
        return;
      }
      if (requestAbort.signal.aborted || req.destroyed || !ownsViewerLease(sess, viewerLease.lease)) {
        text(res, 409, "viewer superseded");
        return;
      }
      const body = await upstreamHttpGet(sess, "/", COPLAY_PORT);
      const normalized = JSON.stringify(JSON.parse(body));
      if (Buffer.byteLength(normalized, "utf8") > MAX_EVENT_PAYLOAD_BYTES) {
        text(res, 502, "upstream unavailable");
        return;
      }
      res.writeHead(200, { "content-type": "application/json", "cache-control": "no-store" });
      res.end(normalized);
    } catch {
      text(res, 502, "upstream unavailable");
    }
    return;
  }

  // Debug route (opt-in via PAIRPUTER_DEBUG) to read capsule log files from the VM's :9000 hook.
  if (DEBUG && reqUrl.pathname === "/vmdbg") {
    const claims = await authorize(reqUrl, "state");
    if (!claims) { text(res, 403, "forbidden"); return; }
    const which = reqUrl.searchParams.get("f") || "input";
    const map = { input: "/dbg/input", focus: "/dbg/focus", inputws: "/dbg/inputws",
                  bridge: "/dbg/bridge", session: "/dbg/session" };
    const path = map[which] || map.input;
    try {
      const sess = getSession(claims);
      const body = await upstreamHttpGet(sess, path, 9000);
      text(res, 200, body);
    } catch {
      text(res, 502, "upstream unavailable");
    }
    return;
  }

  const channel = reqUrl.searchParams.has("player") || reqUrl.pathname === "/player" ? "player"
    : reqUrl.searchParams.has("state") || reqUrl.pathname === "/state" ? "state"
      : reqUrl.searchParams.has("drain") || reqUrl.pathname === "/drain" ? "control"
        : reqUrl.searchParams.has("audio") || reqUrl.pathname === "/audio" ? "audio"
          : "video";
  const claims = await authorize(reqUrl, channel);
  if (!claims) {
    text(res, 403, "forbidden: valid relay session required");
    return;
  }
  const sess = getSession(claims);
  const requestViewer = viewerIdFrom(reqUrl);
  if (requestViewer.present && !requestViewer.valid) {
    text(res, 400, "invalid viewer id");
    return;
  }
  const isAudio = channel === "audio";
  const isVideo = channel === "video";
  let audioReserved = false;
  let videoReserved = false;
  let closeUpstream = null;
  let viewerLease = null;
  let watchdog = null;
  let watchdogBusy = false;
  let finished = false;
  let upstreamClosed = false;
  let responseBackpressured = false;
  let responseDrainListener = null;
  let up = null;
  let streamAborted = req.destroyed || res.destroyed;
  const abortController = (isAudio || isVideo) ? new AbortController() : null;
  const finish = (endResponse = true) => {
    if (finished) return;
    finished = true;
    if (watchdog) {
      clearInterval(watchdog);
      watchdog = null;
    }
    if (responseDrainListener) {
      res.off("drain", responseDrainListener);
      responseDrainListener = null;
    }
    if (abortController && !abortController.signal.aborted) abortController.abort();
    const close = closeUpstream;
    closeUpstream = null;
    if (close) {
      sess.streamClosers.delete(close);
      try { close(); } catch {}
    }
    if (audioReserved && viewerLease) releaseAudioStream(sess, viewerLease.lease, close);
    if (videoReserved && viewerLease) releaseVideoStream(sess, viewerLease.lease, close);
    if (endResponse) {
      try { if (!res.writableEnded) res.end(); } catch {}
    }
  };
  const markStreamAborted = () => {
    streamAborted = true;
    finish();
  };
  if (abortController) {
    req.once("aborted", markStreamAborted);
    res.once("close", markStreamAborted);
  }

  try {
    if (channel === "player") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" });
      res.end(playerHtml());
      return;
    }
    if (channel === "state") {
      const vm = await getVmState(claims.microvmId);
      json(res, 200, {
        state: vm.state,
        tenantId: claims.tenantId,
        imageId: claims.imageId,
        microvmId: claims.microvmId,
        sessionId: claims.sessionId,
        sessionVersion: claims.sessionVersion || 1,
      });
      return;
    }
    if (channel === "control") {
      await drainTenantMicrovmSessions(claims);
      const relayAction = scheduleRelayScaleDown();
      const active = await activeRelaySessionCount().catch(() => -1);
      json(res, 200, {
        ok: true,
        action: "drained",
        tenantId: claims.tenantId,
        imageId: claims.imageId,
        microvmId: claims.microvmId,
        activeSessions: sessions.size,
        activeRelaySessions: active,
        relayAction,
        relayWarmSeconds: RELAY_WARM_SECONDS,
      });
      return;
    }
    if (claims.frozen || sess.draining) {
      text(res, 409, "frozen");
      return;
    }
    if (streamAborted || req.destroyed || res.destroyed) return;
    viewerLease = await claimViewer(sess, requestViewer, { signal: abortController.signal });
    if (!viewerLease.ok) {
      text(res, viewerLease.statusCode || 409, viewerLease.reason);
      return;
    }
    if (viewerLease.waitMs > 0) {
      await new Promise(resolve => setTimeout(resolve, viewerLease.waitMs));
    }
    if (streamAborted || req.destroyed || res.destroyed) return;
    if (!ownsViewerLease(sess, viewerLease.lease)) {
      text(res, 409, "viewer superseded");
      return;
    }
    if (isAudio) {
      if (!await reserveAudioStream(sess, viewerLease.lease, { signal: abortController.signal })) {
        text(res, 409, "audio stream already active");
        return;
      }
      audioReserved = true;
    }
    if (isVideo) {
      if (!await reserveVideoStream(sess, viewerLease.lease, { signal: abortController.signal })) {
        text(res, 409, "video stream already active");
        return;
      }
      videoReserved = true;
    }
    if (!ownsViewerLease(sess, viewerLease.lease)) {
      throw Object.assign(new Error("viewer superseded"), { statusCode: 409 });
    }

    await requireRunning(claims);
    up = await openUpstream(
      sess,
      isAudio ? "/pairputer/audio" : "/pairputer/video",
      isAudio ? AUDIO_PORT : VIDEO_PORT,
      payload => {
        if (res.destroyed || res.writableEnded || !ownsViewerLease(sess, viewerLease.lease)) {
          finish();
          return;
        }
        try {
          const writable = res.write(`data:${payload.toString("base64")}\n\n`);
          if (!writable && !responseBackpressured) {
            // False means Node accepted the frame but its bounded response buffer is full. It is
            // not a disconnect. Pause the source until the client drains so a large desktop
            // keyframe cannot create a reconnect loop or unbounded response buffering.
            responseBackpressured = true;
            up?.pause();
            responseDrainListener = () => {
              responseDrainListener = null;
              responseBackpressured = false;
              if (!finished && !res.destroyed && !res.writableEnded) up?.resume();
            };
            res.once("drain", responseDrainListener);
          }
        } catch {
          finish();
        }
      },
      () => {
        upstreamClosed = true;
        if (closeUpstream) finish();
      },
      { signal: abortController.signal },
    );
    closeUpstream = up.close;
    if (streamAborted || req.destroyed || res.destroyed || upstreamClosed || up.isClosed()
        || !ownsViewerLease(sess, viewerLease.lease)) {
      throw Object.assign(new Error("viewer lease lost"), { statusCode: 409 });
    }
    const attached = isAudio
      ? attachAudioStream(sess, viewerLease.lease, closeUpstream)
      : attachVideoStream(sess, viewerLease.lease, closeUpstream);
    if (!attached || !ownsViewerLease(sess, viewerLease.lease)) {
      throw Object.assign(new Error("viewer lease lost"), { statusCode: 409 });
    }
    sess.streamClosers.add(closeUpstream);
    res.writeHead(200, {
      "content-type": "text/event-stream",
      "cache-control": "no-cache, no-transform",
      "connection": "keep-alive",
      "access-control-allow-origin": "*",
    });
    res.write("retry: 1000\n\n");
    up.activate();
    watchdog = setInterval(async () => {
      if (watchdogBusy || res.destroyed || res.writableEnded) return;
      watchdogBusy = true;
      try {
        if (Date.now() >= Number(claims.exp || 0) * 1000) throw new Error("relay token expired");
        const current = await loadActiveSessionCoalesced(claims);
        if (!sessionClaimsFresh(claims, current)) throw new Error("relay session was revoked");
        if (!ownsViewerLease(sess, viewerLease.lease)) throw new Error("viewer superseded");
        await requireRunning(claims);
      } catch {
        finish();
      } finally {
        watchdogBusy = false;
      }
    }, STREAM_REVALIDATE_MS);
    watchdog.unref?.();
  } catch (err) {
    const statusCode = safeStatusCode(err, 502);
    const hadHeaders = res.headersSent;
    finish(hadHeaders);
    if (!hadHeaders && !res.destroyed) text(res, statusCode, publicRelayError(statusCode));
  }
}

const server = http.createServer((req, res) => {
  handleHttp(req, res).catch(err => {
    logFailure("relay request failed", err);
    if (!res.headersSent) text(res, 500, "relay error");
    else {
      try { res.end(); } catch {}
    }
  });
});

server.on("upgrade", (req, socket) => {
  // No browser-facing WebSocket upgrades: Codex's widget CSP allows only https:// on
  // connect-src, so input rides POST /input (see handleInputPost) instead.
  socket.destroy();
});

setInterval(() => {
  const now = Date.now();
  for (const [key, sess] of sessions) {
    if (sess.inputClients.size || sess.streamClosers.size || sess.eventSubscribers.size) continue;
    if (now < sess.expiresAt) continue;
    if (now - sess.lastActive < 10 * 60 * 1000) continue;
    drainSession(sess).catch(() => {}).finally(() => {
      if (sessions.get(key) === sess) sessions.delete(key);
    });
  }
}, 60_000).unref();

server.listen(PORT, "0.0.0.0", () => {
  console.log(`pairputer stateful relay listening on :${PORT}`);
});
