/**
 * onoda RLM extension (M4, arm S1r): a persistent `ipython` tool backed by the
 * stdlib-only kernel `pi/rlm/onoda_rlm_kernel.py`, with `rlm()` spawning a
 * child Pi process and `commit_state()` externalizing state (A39).
 *
 * Load order: after the task extension and SoL-Pi, before the scripted
 * provider and the recorder. Every setting is read at load, before the
 * recorder scrubs ONODA_PI_* from the environment.
 *
 * Spooling (the M3 tap rule): each cell is spooled as `rlm.cell` after the
 * kernel replies and before Pi hands the result to the model; each
 * `commit_state` value is spooled as `rlm.state` before the kernel is
 * acknowledged; each child is bracketed by `rlm.child.start` (before spawn)
 * and `rlm.child.end` (after exit, before the cell sees the answer).
 *
 * A child is the same `pi` command line (same extensions, flags, provider,
 * model and tools) with its own session dir and prompt, and its own spool at
 * `<spool>/children/<nnnn>` keyed with `SpoolWriter.childKey`. Its policy caps
 * `max_requests` at the parent's remaining budget, and its spooled requests
 * (grandchildren included) are charged back to the parent (A40). A call past
 * max depth, max children, or the budget is refused before any process starts
 * and spooled as `rlm.child.refused`.
 */

import { type ChildProcess, spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import {
	childSubdir,
	ENV_RLM_COMPONENTS,
	ENV_RLM_DEPTH,
	ENV_RLM_PYTHON,
	ENV_RLM_WORK,
	KERNEL_PROTOCOL,
	type KernelCall,
	type KernelExecReply,
	type KernelReply,
	type KernelRequest,
	type RlmComponents,
	TOOL_IPYTHON,
} from "./lib/rlm-protocol.ts";
import {
	counters,
	ENV_POLICY,
	ENV_PREFIX,
	ENV_RUN_INFO,
	ENV_SCRIPT,
	ENV_SPOOL,
	ENV_SPOOL_KEY,
	ENV_TASK,
	type GatewayPolicy,
	readJsonEnv,
	requestsUsed,
	spool,
	type SpoolWriter,
} from "./lib/spool.ts";

const KERNEL_PATH = join(dirname(fileURLToPath(import.meta.url)), "..", "rlm", "onoda_rlm_kernel.py");
const CHILD_TIMEOUT_MS = 600_000;
const KERNEL_START_TIMEOUT_MS = 30_000;

interface ScriptLike {
	children?: unknown[];
	[k: string]: unknown;
}

interface Settings {
	components: RlmComponents;
	depth: number;
	work: string;
	python: string;
	spoolDir: string;
	task: string;
	policy: GatewayPolicy;
	runInfo: Record<string, unknown>;
	script: ScriptLike | undefined;
	/** The environment as it was at load, before the recorder scrubbed it. */
	env: NodeJS.ProcessEnv;
}

function requireEnv(name: string): string {
	const v = process.env[name];
	if (!v) throw new Error(`onoda-rlm: ${name} is not set`);
	return v;
}

function loadSettings(): Settings {
	const components = readJsonEnv<RlmComponents>(ENV_RLM_COMPONENTS);
	if (!components || components.schema !== "onoda.pi-rlm-components/1") {
		throw new Error(`onoda-rlm: ${ENV_RLM_COMPONENTS} is not an onoda.pi-rlm-components/1 file`);
	}
	const depth = Number(requireEnv(ENV_RLM_DEPTH));
	if (!Number.isInteger(depth) || depth < 0) throw new Error(`onoda-rlm: bad ${ENV_RLM_DEPTH}`);
	const policy = readJsonEnv<GatewayPolicy>(ENV_POLICY);
	if (!policy) throw new Error("onoda-rlm: ONODA_PI_POLICY is not set");
	const runInfo = readJsonEnv<Record<string, unknown>>(ENV_RUN_INFO);
	if (!runInfo) throw new Error("onoda-rlm: ONODA_PI_RUN_INFO is not set");
	return {
		components,
		depth,
		work: requireEnv(ENV_RLM_WORK),
		python: requireEnv(ENV_RLM_PYTHON),
		spoolDir: requireEnv(ENV_SPOOL),
		task: requireEnv(ENV_TASK),
		policy,
		runInfo,
		script: readJsonEnv<ScriptLike>(ENV_SCRIPT),
		env: { ...process.env },
	};
}

/** Parent argv with the session dir and the `-p` prompt replaced. */
function childArgv(sessionDir: string, prompt: string): string[] {
	const args = process.argv.slice(2);
	const out: string[] = [];
	let sawSession = false;
	let sawPrompt = false;
	for (let i = 0; i < args.length; i++) {
		if (args[i] === "--session-dir") {
			out.push("--session-dir", sessionDir);
			i++;
			sawSession = true;
		} else if (args[i] === "-p") {
			out.push("-p", prompt);
			i++;
			sawPrompt = true;
		} else {
			out.push(args[i]);
		}
	}
	if (!sawSession || !sawPrompt) throw new Error("onoda-rlm: parent argv has no --session-dir or -p");
	return [...process.execArgv, process.argv[1], ...out];
}

function writePrivate(path: string, data: string | Uint8Array): void {
	writeFileSync(path, data, { mode: 0o600, flag: "wx" });
}

interface ChildOutcome {
	exitCode: number | null;
	endStatus: string | null;
	requests: number;
	costUsd: string | null;
	answer: string | null;
	error: string | null;
}

/**
 * What a finished child spool says: its end status, its requests and cost
 * (its own plus its children's, from their rlm.child.end lines), and the text
 * of its last main-model response.
 */
function readChildSpool(dir: string): Omit<ChildOutcome, "exitCode" | "error"> {
	let raw = "";
	try {
		raw = readFileSync(join(dir, "events.jsonl"), "utf8");
	} catch {
		return { endStatus: null, requests: 0, costUsd: null, answer: null };
	}
	const agentRequests = new Set<number>();
	let requests = 0;
	let cost = 0;
	let costKnown = true;
	let endStatus: string | null = null;
	let lastAgentMessage: string | undefined;
	for (const text of raw.split("\n")) {
		if (!text.endsWith("}")) continue; // blank or torn final line
		const line = JSON.parse(text) as Record<string, unknown>;
		switch (line.type) {
			case "provider.request":
				requests++;
				if (line.purpose === "agent") agentRequests.add(line.request_index as number);
				break;
			case "provider.response":
				if (line.cost_usd == null) costKnown = false;
				else cost += Number(line.cost_usd);
				if (agentRequests.has(line.request_index as number)) lastAgentMessage = line.message as string;
				break;
			case "rlm.child.end":
				requests += line.requests as number;
				if (line.cost_usd == null) costKnown = false;
				else cost += Number(line.cost_usd);
				break;
			case "spool.end":
				endStatus = line.status as string;
				break;
		}
	}
	let answer: string | null = null;
	if (lastAgentMessage) {
		const message = JSON.parse(readFileSync(join(dir, "objects", lastAgentMessage), "utf8")) as {
			content?: { type: string; text?: string }[];
		};
		answer = (message.content ?? [])
			.filter((b) => b.type === "text")
			.map((b) => b.text ?? "")
			.join("\n");
	}
	return { endStatus, requests, costUsd: costKnown ? String(cost) : null, answer };
}

class Kernel {
	private server: Server | undefined;
	private sock: Socket | undefined;
	private proc: ChildProcess | undefined;
	private sockDir: string | undefined;
	private buffer = "";
	private nextId = 0;
	private readonly pending = new Map<number, (reply: KernelExecReply, raw: string) => void>();
	private ready: Promise<void> | undefined;

	constructor(
		private readonly python: string,
		private readonly onCall: (call: KernelCall) => Promise<{ ok: boolean; value?: string; error?: string }>,
	) {}

	start(helpers: string, maxOutputChars: number): Promise<void> {
		this.ready ??= new Promise<void>((resolve, reject) => {
			// A short path: unix socket paths are limited to ~100 bytes.
			this.sockDir = mkdtempSync(join(tmpdir(), "onoda-rlm-"));
			const path = join(this.sockDir, "k.sock");
			const timer = setTimeout(() => reject(new Error("onoda-rlm: kernel did not connect")), KERNEL_START_TIMEOUT_MS);
			this.server = createServer((sock) => {
				if (this.sock) return sock.destroy(); // one kernel per process
				this.sock = sock;
				sock.setEncoding("utf8");
				sock.on("data", (chunk: string) => this.onData(chunk, () => {
					clearTimeout(timer);
					const id = this.nextId++;
					this.pending.set(id, (reply) => {
						if (reply.error) reject(new Error(`onoda-rlm: helpers failed: ${reply.error}`));
						else resolve();
					});
					this.send({ op: "init", id, protocol: KERNEL_PROTOCOL, helpers, max_output_chars: maxOutputChars });
				}));
			});
			this.server.listen(path, () => {
				this.proc = spawn(this.python, [KERNEL_PATH, "--connect", path], { stdio: ["ignore", "ignore", "inherit"] });
				this.proc.on("error", (err) => reject(new Error(`onoda-rlm: kernel failed to start: ${err.message}`)));
				this.proc.on("exit", (code) => {
					const error = `kernel exited (${code})`;
					for (const [, done] of this.pending) {
						done({ op: "exec.reply", id: -1, stdout: "", stderr: "", error, truncated: false }, "");
					}
					this.pending.clear();
				});
			});
		});
		return this.ready;
	}

	private send(msg: KernelRequest): void {
		this.sock?.write(`${JSON.stringify(msg)}\n`);
	}

	private onData(chunk: string, onReady: () => void): void {
		this.buffer += chunk;
		for (let nl = this.buffer.indexOf("\n"); nl >= 0; nl = this.buffer.indexOf("\n")) {
			const raw = this.buffer.slice(0, nl);
			this.buffer = this.buffer.slice(nl + 1);
			const msg = JSON.parse(raw) as KernelReply;
			if (msg.op === "ready") {
				if (msg.protocol !== KERNEL_PROTOCOL) throw new Error(`onoda-rlm: kernel speaks ${msg.protocol}`);
				onReady();
			} else if (msg.op === "exec.reply") {
				const done = this.pending.get(msg.id);
				this.pending.delete(msg.id);
				done?.(msg, raw);
			} else if (msg.op === "call") {
				void this.onCall(msg).then(
					(r) => this.send({ op: "call.reply", id: msg.id, ok: r.ok, value: r.value ?? null, error: r.error ?? null }),
					(err: unknown) =>
						this.send({ op: "call.reply", id: msg.id, ok: false, error: err instanceof Error ? err.message : String(err) }),
				);
			}
		}
	}

	exec(code: string): Promise<{ reply: KernelExecReply; raw: string }> {
		return new Promise((resolve) => {
			const id = this.nextId++;
			this.pending.set(id, (reply, raw) => resolve({ reply, raw: raw || JSON.stringify(reply) }));
			this.send({ op: "exec", id, code });
		});
	}

	stop(): void {
		if (this.sock) this.send({ op: "shutdown", id: this.nextId++ });
		this.sock?.end();
		this.proc?.kill();
		this.server?.close();
		if (this.sockDir) rmSync(this.sockDir, { recursive: true, force: true });
	}
}

export default function onodaRlm(pi: ExtensionAPI): void {
	const w: SpoolWriter | undefined = spool();
	if (!w) throw new Error("onoda-rlm: ONODA_PI_SPOOL is not set");
	const s = loadSettings();
	const limits = s.components.limits;
	const live = new Set<ChildProcess>();
	let cellIndex = 0;
	let childCount = 0;
	let currentCall = ""; // the ipython tool call whose cell is running (cells are sequential)

	const refuse = (reason: "max-depth" | "max-children" | "budget", detail: string) => {
		w.rlmLine({ type: "rlm.child.refused", tool_call_id: currentCall, reason, detail });
		return { ok: false, error: `rlm() refused (${reason}): ${detail}` };
	};

	const runChild = async (prompt: string, context: string | null) => {
		const depth = s.depth + 1;
		if (depth > limits.max_depth) return refuse("max-depth", `child depth ${depth} > max_depth ${limits.max_depth}`);
		if (childCount >= limits.max_children) {
			return refuse("max-children", `${childCount} children already spawned (max_children ${limits.max_children})`);
		}
		const remaining = s.policy.max_requests - requestsUsed();
		if (remaining < 1) return refuse("budget", `no requests left (${requestsUsed()} of ${s.policy.max_requests} used)`);

		const index = childCount++;
		const nnnn = String(index).padStart(4, "0");
		const subdir = childSubdir(index);
		const childSpool = join(s.spoolDir, subdir);
		const stage = join(s.work, nnnn);
		const sessions = join(stage, "sessions");
		mkdirSync(sessions, { recursive: true });
		mkdirSync(join(stage, "rlm"), { recursive: true });
		const text = s.components.child_prompt.replaceAll("{context}", context ?? "").replaceAll("{prompt}", prompt);

		const childPolicy = { ...s.policy, max_requests: remaining };
		if (s.policy.max_cost_usd != null) {
			childPolicy.max_cost_usd = String(Math.max(0, Number(s.policy.max_cost_usd) - counters().costUsd));
		}
		const env: NodeJS.ProcessEnv = {};
		for (const [k, v] of Object.entries(s.env)) if (!k.startsWith(ENV_PREFIX)) env[k] = v;
		writePrivate(join(stage, "policy.json"), JSON.stringify(childPolicy));
		writePrivate(join(stage, "run_info.json"), JSON.stringify(s.runInfo));
		Object.assign(env, {
			[ENV_SPOOL]: childSpool,
			[ENV_TASK]: s.task,
			[ENV_POLICY]: join(stage, "policy.json"),
			[ENV_RUN_INFO]: join(stage, "run_info.json"),
			[ENV_RLM_DEPTH]: String(depth),
			[ENV_RLM_COMPONENTS]: s.env[ENV_RLM_COMPONENTS],
			[ENV_RLM_WORK]: join(stage, "rlm"),
			[ENV_RLM_PYTHON]: s.python,
		});
		const key = w.childKey(index);
		if (key) {
			writePrivate(join(stage, "spool.key"), key);
			env[ENV_SPOOL_KEY] = join(stage, "spool.key");
		}
		if (s.script) {
			const childScript = s.script.children?.[index] ?? {
				schema: "onoda.pi-script/1",
				synthetic: true,
				source: `${String(s.script.source)}:missing-child-${nnnn}`,
				turns: [],
			};
			writePrivate(join(stage, "script.json"), JSON.stringify(childScript));
			env[ENV_SCRIPT] = join(stage, "script.json");
		}

		w.rlmLine({
			type: "rlm.child.start",
			tool_call_id: currentCall,
			child_index: index,
			depth,
			spool_subdir: subdir,
			prompt: w.putObject(text),
			max_requests: remaining,
		});
		const exitCode = await new Promise<number | null>((resolve) => {
			const child = spawn(process.execPath, childArgv(sessions, text), {
				cwd: process.cwd(),
				env,
				stdio: ["ignore", "ignore", "ignore"],
			});
			live.add(child);
			const timer = setTimeout(() => child.kill("SIGKILL"), CHILD_TIMEOUT_MS);
			child.on("error", () => resolve(null));
			child.on("exit", (code) => {
				clearTimeout(timer);
				live.delete(child);
				resolve(code);
			});
		});
		const out = readChildSpool(childSpool);
		const c = counters();
		c.childRequests += out.requests;
		if (out.costUsd == null) {
			if (out.requests > 0) c.costUnknown = true;
		} else c.costUsd += Number(out.costUsd);
		const ok = out.endStatus === "settled" && out.answer !== null;
		const error = ok ? null : `child ${nnnn} ended ${out.endStatus ?? `without spool.end (exit ${exitCode})`}`;
		w.rlmLine({
			type: "rlm.child.end",
			child_index: index,
			exit_code: exitCode,
			end_status: out.endStatus,
			requests: out.requests,
			cost_usd: out.costUsd,
			answer: out.answer === null ? null : w.putObject(out.answer),
			error,
		});
		return ok ? { ok: true, value: out.answer ?? "" } : { ok: false, error: error ?? "child failed" };
	};

	const commitState = async (name: string, json: string) => {
		const hex = w.putObject(json);
		w.rlmLine({ type: "rlm.state", tool_call_id: currentCall, name, value: hex });
		return { ok: true, value: `sha256:${hex}` };
	};

	const kernel = new Kernel(s.python, async (call) => {
		const args = call.args;
		if (call.fn === "rlm") {
			if (typeof args.prompt !== "string" || (args.context != null && typeof args.context !== "string")) {
				return { ok: false, error: "rlm() takes a string prompt and an optional string context" };
			}
			return runChild(args.prompt, (args.context as string | null) ?? null);
		}
		if (typeof args.name !== "string" || typeof args.json !== "string") {
			return { ok: false, error: "commit_state() needs a name and a JSON value" };
		}
		return commitState(args.name, args.json);
	});

	pi.registerTool({
		name: TOOL_IPYTHON,
		label: "Python",
		description: s.components.tool_doc,
		promptSnippet: "Run Python in a persistent REPL (rlm() and commit_state() available)",
		parameters: Type.Object({ code: Type.String({ description: "Python source to run" }) }, { additionalProperties: false }),
		executionMode: "sequential",
		async execute(toolCallId, params) {
			await kernel.start(s.components.helpers, limits.max_output_chars);
			currentCall = toolCallId;
			const index = cellIndex++;
			const { reply, raw } = await kernel.exec(params.code);
			w.rlmLine({
				type: "rlm.cell",
				tool_call_id: toolCallId,
				cell_index: index,
				code: w.putObject(params.code),
				output: w.putObject(raw),
				is_error: reply.error !== null,
				truncated: reply.truncated,
			});
			const parts = [reply.stdout];
			if (reply.stderr) parts.push(`[stderr]\n${reply.stderr}`);
			if (reply.error) parts.push(`[error]\n${reply.error}`);
			const text = parts.filter((p) => p !== "").join("\n") || "(no output)";
			if (reply.error !== null) throw new Error(text);
			return { content: [{ type: "text", text }], details: { cell_index: index, truncated: reply.truncated } };
		},
	});

	pi.on("before_agent_start", (event) => ({ systemPrompt: `${event.systemPrompt}\n\n${s.components.system_prompt}` }));

	pi.on("session_shutdown", () => {
		for (const child of live) child.kill("SIGKILL");
		kernel.stop();
	});
}
