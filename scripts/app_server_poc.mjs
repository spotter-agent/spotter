#!/usr/bin/env node

import assert from "node:assert/strict";
import { writeFileSync } from "node:fs";

const usage = `usage:
  node scripts/app_server_poc.mjs probe --endpoint ws://127.0.0.1:PORT
  node scripts/app_server_poc.mjs observe --endpoint URL [--cwd PATH] [--steer TEXT]
       [--expect TEXT] [--timeout SECONDS] [--out FILE]
  node scripts/app_server_poc.mjs --self-test`;

function parseArgs(argv) {
  if (argv[0] === "--self-test") return { mode: "self-test" };
  const options = { mode: argv[0], timeout: 120 };
  for (let i = 1; i < argv.length; i += 2) {
    const key = argv[i]?.replace(/^--/, "");
    if (!key || argv[i + 1] === undefined) throw new Error(usage);
    options[key] = key === "timeout" ? Number(argv[i + 1]) : argv[i + 1];
  }
  if (!["probe", "observe"].includes(options.mode) || !options.endpoint) {
    throw new Error(usage);
  }
  if (!Number.isFinite(options.timeout) || options.timeout <= 0) throw new Error(usage);
  return options;
}

function activeTurn(thread) {
  return thread?.turns?.findLast((turn) => turn.status === "inProgress");
}

function summarize(message) {
  const params = message.params ?? {};
  const item = params.item;
  return {
    at: new Date().toISOString(),
    method: message.method,
    threadId: params.threadId ?? params.thread?.id,
    turnId: params.turnId ?? params.turn?.id,
    status: params.status?.type ?? params.turn?.status,
    cwd: params.thread?.cwd,
    source: params.thread?.source,
    itemType: item?.type,
    text: item?.type === "agentMessage" ? item.text : undefined,
  };
}

const options = parseArgs(process.argv.slice(2));
if (options.mode === "self-test") {
  const event = summarize({ method: "thread/started", params: { thread: { id: "t", cwd: "/tmp" } } });
  assert.equal(event.threadId, "t");
  assert.equal(event.cwd, "/tmp");
  assert.equal(activeTurn({ turns: [{ id: "u", status: "inProgress" }] }).id, "u");
  console.log("ok");
  process.exit(0);
}

if (typeof WebSocket === "undefined") throw new Error("Node.js 22+ is required");

const report = {
  schemaVersion: 1,
  endpoint: options.endpoint,
  startedAt: new Date().toISOString(),
  server: null,
  events: [],
  threads: [],
  steer: options.steer ? { requested: false, response: null } : null,
  completed: false,
};
const pending = new Map();
const joined = new Set();
const joining = new Set();
let nextId = 1;
let targetThreadId;
let targetTurnId;
let targetCompleted = false;
let finished = false;
let socket;

function save() {
  report.finishedAt = new Date().toISOString();
  if (options.out) writeFileSync(options.out, `${JSON.stringify(report, null, 2)}\n`);
  console.log(JSON.stringify(report, null, 2));
}

function finish(code = 0) {
  if (finished) return;
  finished = true;
  clearTimeout(timeout);
  report.completed = code === 0;
  save();
  process.exitCode = code;
  socket.close();
}

function notify(method, params = undefined) {
  socket.send(JSON.stringify({ jsonrpc: "2.0", method, ...(params && { params }) }));
}

function request(method, params, context = {}) {
  const id = nextId++;
  pending.set(id, { method, ...context });
  socket.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
  return id;
}

function join(threadId, attempt = 1) {
  if (joined.has(threadId) || joining.has(threadId) || finished) return;
  joining.add(threadId);
  request("thread/resume", { threadId }, { threadId, attempt });
}

function steerSucceeded() {
  if (!report.steer?.response) return false;
  if (!options.expect) return true;
  return report.events.some(
    (event) => event.turnId === targetTurnId && event.text?.includes(options.expect),
  );
}

function steer(threadId, turnId) {
  if (!options.steer || report.steer.requested) return;
  report.steer.requested = true;
  report.steer.threadId = threadId;
  report.steer.turnId = turnId;
  request(
    "turn/steer",
    { threadId, expectedTurnId: turnId, input: [{ type: "text", text: options.steer }] },
    { threadId, turnId },
  );
}

const timeout = setTimeout(() => {
  report.error = `timed out after ${options.timeout}s`;
  finish(2);
}, options.timeout * 1000);

socket = new WebSocket(options.endpoint);
socket.onopen = () => {
  request("initialize", {
    clientInfo: { name: "spotter-app-server-poc", version: "0.1" },
    capabilities: { experimentalApi: true },
  });
};
socket.onerror = () => {
  report.error = "websocket connection failed";
  finish(1);
};
socket.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.id !== undefined) {
    const context = pending.get(message.id);
    pending.delete(message.id);
    if (!context) return;

    if (context.method === "initialize") {
      if (message.error) {
        report.error = message.error;
        finish(1);
        return;
      }
      report.server = message.result;
      notify("initialized");
      if (options.mode === "probe") {
        request("thread/list", { limit: 5, sortKey: "updated_at" });
      }
      return;
    }

    if (context.method === "thread/list") {
      report.threads = (message.result?.data ?? []).map(({ id, cwd, source, status }) => ({
        id, cwd, source, status,
      }));
      if (message.error) report.error = message.error;
      finish(message.error ? 1 : 0);
      return;
    }

    if (context.method === "thread/resume") {
      joining.delete(context.threadId);
      if (message.error) {
        if (context.attempt < 20 && message.error.message?.includes("no rollout found")) {
          setTimeout(() => join(context.threadId, context.attempt + 1), 100);
        } else {
          report.error = message.error;
          finish(1);
        }
        return;
      }
      joined.add(context.threadId);
      const turn = activeTurn(message.result?.thread);
      if (turn) {
        targetTurnId = turn.id;
        steer(context.threadId, turn.id);
      }
      return;
    }

    if (context.method === "turn/steer") {
      report.steer.response = message.error ?? message.result;
      if (message.error) finish(1);
      else if (targetCompleted) finish(steerSucceeded() ? 0 : 1);
    }
    return;
  }

  if (!message.method) return;
  const eventThreadId = message.params?.threadId ?? message.params?.thread?.id;
  const matchesCwd =
    message.method === "thread/started" &&
    (!options.cwd || message.params.thread.cwd === options.cwd);
  if (
    (matchesCwd || eventThreadId === targetThreadId) &&
    [
      "thread/started",
      "thread/status/changed",
      "turn/started",
      "item/started",
      "item/completed",
      "turn/completed",
    ].includes(message.method)
  ) {
    report.events.push(summarize(message));
  }

  if (message.method === "thread/started") {
    const thread = message.params.thread;
    if (options.cwd && thread.cwd !== options.cwd) return;
    targetThreadId = thread.id;
    report.threads.push({ id: thread.id, cwd: thread.cwd, source: thread.source });
    setTimeout(() => join(thread.id), 100);
  } else if (message.method === "thread/status/changed" && message.params.threadId === targetThreadId) {
    join(targetThreadId);
  } else if (message.method === "turn/started" && message.params.threadId === targetThreadId) {
    targetTurnId = message.params.turn.id;
    steer(targetThreadId, targetTurnId);
  } else if (message.method === "turn/completed" && message.params.threadId === targetThreadId) {
    targetCompleted = true;
    if (!options.steer) finish(0);
    else if (report.steer.response) {
      if (!steerSucceeded() && options.expect) {
        report.error = `agent response did not contain: ${options.expect}`;
      }
      finish(steerSucceeded() ? 0 : 1);
    }
  }
};
