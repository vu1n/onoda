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
 */

import { createHash } from "node:crypto";
import { closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync, renameSync, writeSync } from "node:fs";
import { join } from "node:path";

export const ENV_SPOOL = "ONODA_PI_SPOOL";
export const ENV_SCRIPT = "ONODA_PI_SCRIPT";
export const ENV_TASK = "ONODA_PI_TASK";
export const ENV_POLICY = "ONODA_PI_POLICY";
export const ENV_RUN_INFO = "ONODA_PI_RUN_INFO";

export const SPOOL_SCHEMA = "onoda.pi-spool/1";
export const SCRIPTED_PROVIDER = "onoda-scripted";
export const SCRIPTED_MODEL = "scripted";
export const SCRIPTED_REDUCER_MODEL = "scripted-reducer";

export const TOOL_CASE_INPUT = "case_input";
export const TOOL_ORCA_RUN = "orca_run";
export const TOOL_REPORT = "report_states";

export interface GatewayPolicy {
	schema: "onoda.pi-policy/1";
	allowed_tools: string[];
	max_requests: number;
	max_acquisitions: number;
	unfusable_command_tokens: string[];
	path_aliases: Record<string, string>;
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

/** Drop `timestamp` keys and alias absolute paths so a replay key is location- and time-free. */
export function normalizeForReplay(value: unknown, aliases: Record<string, string>): unknown {
	const paths = Object.keys(aliases).sort((a, b) => b.length - a.length);
	const walk = (v: unknown): unknown => {
		if (typeof v === "string") {
			let s = v;
			for (const p of paths) s = s.split(p).join(aliases[p]);
			return s;
		}
		if (Array.isArray(v)) return v.map(walk);
		if (v && typeof v === "object") {
			const out: Record<string, unknown> = {};
			for (const [k, x] of Object.entries(v as Record<string, unknown>)) {
				if (k === "timestamp") continue;
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

export class SpoolWriter {
	readonly dir: string;
	private seq = 0;
	private readonly fd: number;

	constructor(dir: string) {
		this.dir = dir;
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

	/** Append one line; `seq` is assigned here. Returns the seq. */
	line(fields: Record<string, unknown>): number {
		const seq = this.seq++;
		writeSync(this.fd, `${JSON.stringify({ seq, ...fields })}\n`);
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
	g[KEY] = new SpoolWriter(dir);
	return g[KEY];
}

export function readJsonEnv<T>(name: string): T | undefined {
	const path = process.env[name];
	return path ? (JSON.parse(readFileSync(path, "utf8")) as T) : undefined;
}

/** Shared mutable run counters (request index, acquisitions) across onoda extensions. */
export interface RunCounters {
	requests: number;
	acquisitions: number;
	ended: boolean;
}

const COUNTERS = Symbol.for("onoda.pi.counters");

export function counters(): RunCounters {
	const g = globalThis as Record<symbol, RunCounters | undefined>;
	g[COUNTERS] ??= { requests: 0, acquisitions: 0, ended: false };
	return g[COUNTERS];
}
