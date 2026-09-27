/**
 * Spool writer shared by onoda's Pi extensions. Mirrors
 * `src/onoda/adapters/pi/contract.py` (spool section) field for field.
 *
 * Every object is written and fsynced under objects/<sha256> before any line
 * that cites it, and every line is fsynced before the caller returns, so the
 * spool is a write-ahead log: whatever the model is shown was durable first.
 *
 * All onoda extensions in one Pi process share one writer through
 * `globalThis`, so `seq` is a single gap-free sequence even if the extension
 * loader instantiates this module more than once.
 *
 * When ONODA_PI_SPOOL_KEY names a key file, the writer reads the key once,
 * unlinks the file, and chains every line with an HMAC (`mac`, the byte rule
 * in contract.py `_Line`). That is tamper evidence against the agent's own
 * tools rewriting the spool, not a sandbox.
 */

import { createHash, createHmac } from "node:crypto";
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, renameSync, unlinkSync, writeSync } from "node:fs";
import { join } from "node:path";
import { childTag, type RlmLine } from "./rlm-protocol.ts";

export const ENV_SPOOL = "ONODA_PI_SPOOL";
export const ENV_SCRIPT = "ONODA_PI_SCRIPT";
export const ENV_TASK = "ONODA_PI_TASK";
export const ENV_POLICY = "ONODA_PI_POLICY";
export const ENV_RUN_INFO = "ONODA_PI_RUN_INFO";
export const ENV_SPOOL_KEY = "ONODA_PI_SPOOL_KEY";
export const ENV_PREFIX = "ONODA_PI_";

export const SPOOL_SCHEMA = "onoda.pi-spool/1";
export const SCRIPTED_PROVIDER = "onoda-scripted";
export const SCRIPTED_MODEL = "scripted";
export const SCRIPTED_REDUCER_MODEL = "scripted-reducer";

export type EndStatus =
	| "settled"
	| "aborted"
	| "budget-exhausted"
	| "replay-mismatch"
	| "script-exhausted"
	| "provider-error";

export const TOOL_CASE_INPUT = "case_input";
export const TOOL_ORCA_RUN = "orca_run";
export const TOOL_REPORT = "report_states";

export interface GatewayPolicy {
	schema: "onoda.pi-policy/1";
	allowed_tools: string[];
	max_requests: number;
	max_acquisitions: number;
	max_cost_usd?: string | null;
	unfusable_command_tokens: string[];
	path_aliases: Record<string, string>;
	read_roots?: string[] | null;
	write_roots?: string[] | null;
}

export function sha256Hex(data: string | Uint8Array): string {
	return createHash("sha256").update(data).digest("hex");
}

/** JSON with object keys sorted at every level; the replay-key serialization. */
export function stableStringify(value: unknown): string {
	if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
	if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
	const entries = Object.keys(value as Record<string, unknown>)
		.filter((k) => (value as Record<string, unknown>)[k] !== undefined)
		.sort()
		.map((k) => `${JSON.stringify(k)}:${stableStringify((value as Record<string, unknown>)[k])}`);
	return `{${entries.join(",")}}`;
}

/**
 * Pi's bash tool spills long output to `join(tmpdir(), "pi-bash-<16 hex>.log")`
 * (OutputAccumulator in 0.85.1; the id is random per call).
 */
export const PI_TEMP_LOG_RE = /(?:\/[^/\s"'()[\]]+)*\/pi-(?:bash|output|powershell)-[0-9a-f]{16}\.log/g;
export const PI_TEMP_LOG_PLACEHOLDER = "$PI_TEMP_LOG";

/**
 * Drop `timestamp` keys and a tool result's `details` (Pi-internal metadata
 * that no provider is sent), alias absolute paths and the session id, and
 * replace Pi's random temp-log paths, so a replay key is location- and
 * time-free (A34).
 */
export function normalizeForReplay(value: unknown, aliases: Record<string, string>): unknown {
	const paths = Object.keys(aliases).sort((a, b) => b.length - a.length);
	const walk = (v: unknown): unknown => {
		if (typeof v === "string") {
			let s = v;
			for (const p of paths) s = s.split(p).join(aliases[p]);
			return s.replace(PI_TEMP_LOG_RE, PI_TEMP_LOG_PLACEHOLDER);
		}
		if (Array.isArray(v)) return v.map(walk);
		if (v && typeof v === "object") {
			const obj = v as Record<string, unknown>;
			const out: Record<string, unknown> = {};
			for (const [k, x] of Object.entries(obj)) {
				if (k === "timestamp") continue;
				if (k === "details" && obj.role === "toolResult") continue;
				out[k] = walk(x);
			}
			return out;
		}
		return v;
	};
	return walk(value);
}

export function replayKey(context: unknown, aliases: Record<string, string>): string {
	return sha256Hex(stableStringify(normalizeForReplay(context, aliases)));
}

const ALIASES = Symbol.for("onoda.pi.aliases");

/**
 * The process-wide replay aliases: the policy's `path_aliases` plus aliases
 * learned at run time (the session id, added by the recorder at session_start).
 */
export function replayAliases(): Record<string, string> {
	const g = globalThis as Record<symbol, Record<string, string> | undefined>;
	g[ALIASES] ??= { ...(readJsonEnv<GatewayPolicy>(ENV_POLICY)?.path_aliases ?? {}) };
	return g[ALIASES];
}

export class SpoolWriter {
	readonly dir: string;
	private seq = 0;
	private readonly fd: number;
	private readonly key: Buffer | undefined;
	private prevMac = "";

	constructor(dir: string, keyPath?: string) {
		this.dir = dir;
		if (keyPath) {
			this.key = readFileSync(keyPath);
			unlinkSync(keyPath);
			if (this.key.length < 32) throw new Error("onoda spool: HMAC key is too short");
		}
		mkdirSync(join(dir, "objects"), { recursive: true });
		this.fd = openSync(join(dir, "events.jsonl"), "a");
	}

	/** Durably store bytes; returns their sha256 hex. Idempotent. */
	putObject(data: string | Uint8Array): string {
		const bytes = typeof data === "string" ? Buffer.from(data, "utf8") : data;
		const hex = sha256Hex(bytes);
		const dest = join(this.dir, "objects", hex);
		if (!existsSync(dest)) {
			const tmp = `${dest}.tmp-${process.pid}`;
			const fd = openSync(tmp, "w");
			try {
				writeSync(fd, bytes);
				fsyncSync(fd);
			} finally {
				closeSync(fd);
			}
			renameSync(tmp, dest);
		}
		return hex;
	}

	putJson(value: unknown): string {
		return this.putObject(JSON.stringify(value));
	}

	/**
	 * A child spool's HMAC key (contract.py `derive_child_key`), or undefined
	 * when this spool is unkeyed.
	 */
	childKey(childIndex: number): Buffer | undefined {
		if (!this.key) return undefined;
		return createHmac("sha256", this.key).update(`onoda.pi-rlm-child/${childTag(childIndex)}`, "utf8").digest();
	}

	/** Append an rlm.* line (rlm-protocol.ts shapes). */
	rlmLine(fields: RlmLine): number {
		return this.line(fields as unknown as Record<string, unknown>);
	}

	/** Append one line; `seq` (and `mac`, when keyed) are assigned here. Returns the seq. */
	line(fields: Record<string, unknown>): number {
		const seq = this.seq++;
		const { mac: _ignored, ...rest } = fields;
		let text = JSON.stringify({ seq, ...rest });
		if (this.key) {
			// contract.py `_Line`: HMAC over the previous mac + this line's JSON without mac.
			const mac = createHmac("sha256", this.key).update(this.prevMac + text, "utf8").digest("hex");
			this.prevMac = mac;
			text = `${text.slice(0, -1)},"mac":"${mac}"}`;
		}
		writeSync(this.fd, `${text}\n`);
		fsyncSync(this.fd);
		return seq;
	}
}

const KEY = Symbol.for("onoda.pi.spool");

/** The process-wide writer, or undefined when ONODA_PI_SPOOL is unset. */
export function spool(): SpoolWriter | undefined {
	const g = globalThis as Record<symbol, SpoolWriter | undefined>;
	if (g[KEY]) return g[KEY];
	const dir = process.env[ENV_SPOOL];
	if (!dir) return undefined;
	g[KEY] = new SpoolWriter(dir, process.env[ENV_SPOOL_KEY]);
	return g[KEY];
}

/**
 * Remove every ONODA_PI_* variable from this process's environment so tool
 * subprocesses inherit none of them. The recorder calls it at the end of its
 * load (it loads last); every onoda extension reads its settings at load.
 */
export function scrubOnodaEnv(): void {
	for (const name of Object.keys(process.env)) {
		if (name.startsWith(ENV_PREFIX)) delete process.env[name];
	}
}

export function readJsonEnv<T>(name: string): T | undefined {
	const path = process.env[name];
	return path ? (JSON.parse(readFileSync(path, "utf8")) as T) : undefined;
}

/** Shared mutable run counters (request index, acquisitions) across onoda extensions. */
export interface RunCounters {
	/** This process's own provider requests (the next request_index). */
	requests: number;
	/** Requests spent by RLM child processes, charged to this run's budget. */
	childRequests: number;
	acquisitions: number;
	/** Sum of every spooled response's cost (Pi's estimate, or a script's synthetic usage_cost). */
	costUsd: number;
	/** A response with no cost figure arrived (a cost cap then fails closed). */
	costUnknown: boolean;
	ended: boolean;
}

const COUNTERS = Symbol.for("onoda.pi.counters");

export function counters(): RunCounters {
	const g = globalThis as Record<symbol, RunCounters | undefined>;
	g[COUNTERS] ??= { requests: 0, childRequests: 0, acquisitions: 0, costUsd: 0, costUnknown: false, ended: false };
	return g[COUNTERS];
}

/** Requests charged against `max_requests`: this process's own plus its RLM children's. */
export function requestsUsed(): number {
	const c = counters();
	return c.requests + c.childRequests;
}

/** Why the cost cap stops the run, or undefined while it may continue. */
export function costExhausted(policy: GatewayPolicy): string | undefined {
	if (policy.max_cost_usd == null) return undefined;
	const c = counters();
	if (c.costUnknown) return "a response carried no cost figure; the cost cap fails closed";
	const cap = Number(policy.max_cost_usd);
	if (c.costUsd >= cap) return `cost budget exhausted (${c.costUsd} >= ${policy.max_cost_usd} USD)`;
	return undefined;
}

/** Account one response's cost; `cost` is Pi's `usage.cost.total` (or undefined). */
export function addCost(cost: unknown): string | null {
	const c = counters();
	if (typeof cost === "number" && Number.isFinite(cost)) {
		c.costUsd += cost;
		return String(cost);
	}
	c.costUnknown = true;
	return null;
}
