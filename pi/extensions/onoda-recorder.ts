/**
 * onoda recorder: loaded last, so every hook here sees what the model is
 * actually shown after all other extensions ran.
 *
 * It writes `spool.start`, provider requests for real providers (the scripted
 * provider spools its own requests and its reducer responses), main-model
 * responses, real-provider reducer and compaction-summary requests with their
 * responses (see `recordSideRequests`), every tool call with the gateway decision (before the tool runs),
 * the visible tool results, compactions, and exactly one `spool.end`.
 * `spool.end` is always the last line: other extensions ask for a terminal
 * status with `requestEnd`; the recorder aborts at the next turn and writes it
 * when the agent settles (or at shutdown, as `aborted`, if it never did).
 *
 * `max_requests` counts every provider request, reducer and summary calls
 * included: the budget is the budget. `max_cost_usd` (live runs) sums every
 * spooled response's cost and fails closed on a response without one.
 *
 * A call Pi rejects before the `tool_call` hook (unknown tool, arguments that
 * fail the schema, a truncated message, an abort) still gets a visible error
 * result; the recorder spools a blocked `tool.call` for it first, so every
 * result cites a call.
 *
 * At the end of its load it removes every ONODA_PI_* variable from the
 * process environment, so no tool subprocess inherits the spool path or any
 * other onoda setting.
 */

import { realpathSync } from "node:fs";
import { basename, dirname, isAbsolute, join, resolve, sep } from "node:path";
import type { ExtensionAPI, ExtensionContext, MessageEndEvent } from "@earendil-works/pi-coding-agent";
import {
	addCost,
	costExhausted,
	counters,
	ENV_POLICY,
	ENV_RUN_INFO,
	type EndStatus,
	type GatewayPolicy,
	readJsonEnv,
	replayAliases,
	replayKey,
	requestsUsed,
	SCRIPTED_PROVIDER,
	SPOOL_SCHEMA,
	type SpoolWriter,
	scrubOnodaEnv,
	spool,
	TOOL_ORCA_RUN,
	TOOL_ORCA_SUBMIT,
} from "./lib/spool.ts";

type AssistantMessage = Extract<MessageEndEvent["message"], { role: "assistant" }>;
type ToolResultMessage = Extract<MessageEndEvent["message"], { role: "toolResult" }>;

export type { EndStatus };

interface PendingEnd {
	status: EndStatus;
	reason: string | null;
}

const PENDING_END = Symbol.for("onoda.pi.pendingEnd");
const MAIN_REQUEST = Symbol.for("onoda.pi.mainRequest");

/** Note that main-model request `index` was spooled; its response is the next assistant message_end. */
export function mainRequestSent(index: number): void {
	(globalThis as Record<symbol, number | undefined>)[MAIN_REQUEST] = index;
}

function lastMainRequest(): number | undefined {
	return (globalThis as Record<symbol, number | undefined>)[MAIN_REQUEST];
}

/** Record why the run is ending; the first request wins and the recorder writes it at settle. */
export function requestEnd(status: EndStatus, reason: string): void {
	const g = globalThis as Record<symbol, PendingEnd | undefined>;
	g[PENDING_END] ??= { status, reason };
}

function pendingEnd(): PendingEnd | undefined {
	return (globalThis as Record<symbol, PendingEnd | undefined>)[PENDING_END];
}

function writeEnd(w: SpoolWriter, fallback: EndStatus, reason: string | null): void {
	const c = counters();
	if (c.ended) return;
	c.ended = true;
	const p = pendingEnd();
	w.line({ type: "spool.end", status: p?.status ?? fallback, requests: c.requests, reason: p?.reason ?? reason });
}

const REJECTED_BEFORE_TOOL_CALL = "rejected by Pi before tool_call";

/** Pi's own file tools and the root set their `path` must stay under (sandboxed runs, A50). */
const FILE_TOOL_ROOTS: Record<string, "read_roots" | "write_roots"> = { read: "read_roots", edit: "write_roots", write: "write_roots" };

/** `p` with symlinks resolved as far as it exists; the missing tail is appended unresolved. */
function realpathLoose(p: string): string {
	const rest: string[] = [];
	let cur = p;
	for (;;) {
		try {
			return join(realpathSync(cur), ...rest.reverse());
		} catch {
			const parent = dirname(cur);
			if (parent === cur) return p;
			rest.push(basename(cur));
			cur = parent;
		}
	}
}

/** Blocking reason when a file tool's path leaves its roots; undefined when it stays inside. */
function pathDecision(policy: GatewayPolicy, toolName: string, input: Record<string, unknown>): string | undefined {
	const key = FILE_TOOL_ROOTS[toolName];
	const roots = key ? policy[key] : undefined;
	if (!key || !roots) return undefined;
	const raw = input.path;
	if (typeof raw !== "string" || raw.length === 0) return `${toolName} has no string path`;
	// Pi strips one leading "@" and expands "~" and file:// URLs; refuse the forms it would reinterpret.
	const stripped = raw.startsWith("@") ? raw.slice(1) : raw;
	if (stripped.startsWith("~") || stripped.startsWith("file:") || /[^\x20-\x7e]/.test(stripped)) {
		return `${toolName} path ${JSON.stringify(raw)} is not a plain path`;
	}
	const target = realpathLoose(isAbsolute(stripped) ? resolve(stripped) : resolve(process.cwd(), stripped));
	const inside = [...roots, ...(key === "read_roots" ? (policy.write_roots ?? []) : [])].some((r) => {
		const root = realpathLoose(r);
		return target === root || target.startsWith(root.endsWith(sep) ? root : root + sep);
	});
	return inside ? undefined : `${toolName} path ${JSON.stringify(raw)} is outside the sandbox roots`;
}

/** Blocking reason for a call under `policy`, or undefined when it may run. */
function gatewayDecision(policy: GatewayPolicy, toolName: string, input: Record<string, unknown>): string | undefined {
	const c = counters();
	// Calls proposed by a request past the budget (reducer and RLM child requests count too).
	const used = requestsUsed();
	if (used > policy.max_requests) return `request budget exhausted (${used} > ${policy.max_requests})`;
	const cost = costExhausted(policy);
	if (cost) return cost;
	if (!policy.allowed_tools.includes(toolName)) return `tool ${toolName} is not allowed by the gateway policy`;
	const outside = pathDecision(policy, toolName, input);
	if (outside) return outside;
	// Context: doc://onoda/agent-sandbox@0002#sandbox-or-refuse — this then_run token filter is a check, not a boundary; the sandbox is the boundary
	const thenRun = input.then_run;
	if (thenRun !== undefined && thenRun !== null) {
		// Anything but {command: string} cannot be checked, so it never runs.
		const command = typeof thenRun === "object" ? (thenRun as { command?: unknown }).command : undefined;
		if (typeof command !== "string") return "then_run has no string command";
		const lowered = command.toLowerCase();
		const token = policy.unfusable_command_tokens.find((t) => lowered.includes(t.toLowerCase()));
		if (token) return `then_run command contains unfusable token ${JSON.stringify(token)}`;
	}
	if (toolName === TOOL_ORCA_RUN && c.acquisitions >= policy.max_acquisitions) {
		return `acquisition budget exhausted (${policy.max_acquisitions})`;
	}
	// Cache hits count: the cap is on submissions, not on ORCA time (A59).
	const maxExecutions = policy.max_executions ?? 0;
	if (toolName === TOOL_ORCA_SUBMIT && c.executions >= maxExecutions) {
		return `execution budget exhausted (${maxExecutions})`;
	}
	return undefined;
}

function usageTokens(m: AssistantMessage): Record<string, number> {
	const u = m.usage;
	return {
		input: u?.input ?? 0,
		output: u?.output ?? 0,
		cacheRead: u?.cacheRead ?? 0,
		cacheWrite: u?.cacheWrite ?? 0,
		totalTokens: u?.totalTokens ?? 0,
	};
}

type SideMessage = AssistantMessage;
interface SideStream {
	result(): Promise<SideMessage>;
}
type RuntimeCall = (
	model: { id: string; provider: string },
	context: unknown,
	options?: Record<string, unknown>,
) => SideStream;

/**
 * Record real-provider requests Pi sends outside the agent loop. Those fire no
 * `before_provider_request` (Pi emits it only from the agent's own `onPayload`)
 * and no `message_end`, so without this a live run's reducer and compaction
 * calls would be neither spooled nor charged to the budget.
 *
 * Pi 0.85.1 routes every call through one `ModelRuntime`, shared by the model
 * registry: the agent loop and compaction call `streamSimple` (only the agent
 * passes `onPayload`), and SoL-Pi's reducer calls `modelRegistry.complete`,
 * which is `runtime.stream`. So the role comes from the call path, never from
 * the model id, and the reducer may use the agent's own model. The scripted
 * provider spools its own requests and is left alone.
 */
function recordSideRequests(
	ctx: ExtensionContext,
	w: SpoolWriter,
	policy: GatewayPolicy,
	aliases: Record<string, string>,
): void {
	const runtime = (ctx.modelRegistry as unknown as { runtime?: Record<string, RuntimeCall> }).runtime;
	if (typeof runtime?.stream !== "function" || typeof runtime.streamSimple !== "function") {
		throw new Error("onoda-recorder: Pi's model runtime has no stream/streamSimple to record through");
	}
	const roles = [
		["stream", "reducer"],
		["streamSimple", "summary"],
	] as const;
	for (const [method, purpose] of roles) {
		const original = runtime[method].bind(runtime);
		runtime[method] = (model, context, options) => {
			if (options?.onPayload || model.provider === SCRIPTED_PROVIDER) return original(model, context, options);
			let index: number | undefined;
			const onPayload = async (payload: unknown) => {
				const refused =
					requestsUsed() >= policy.max_requests ? `max_requests ${policy.max_requests} reached` : costExhausted(policy);
				if (refused) {
					// Refused before it is sent: nothing to spool, nothing counted.
					requestEnd("budget-exhausted", `${purpose} request refused: ${refused}`);
					throw new Error(`onoda: ${purpose} request refused: ${refused}`);
				}
				index = counters().requests++;
				w.line({
					type: "provider.request",
					request_index: index,
					model: model.id,
					context: w.putJson(payload),
					source: "model_runtime",
					replay_key: replayKey(payload, aliases),
					purpose,
				});
				return payload;
			};
			const stream = original(model, context, { ...options, onPayload });
			// Attached before the caller awaits the result, so the response is spooled first.
			stream.result().then(
				(m) => {
					if (index === undefined) return;
					w.line({
						type: "provider.response",
						request_index: index,
						message: w.putJson(m),
						stop_reason: m.stopReason,
						usage_tokens: usageTokens(m),
						cost_usd: addCost(m.usage?.cost?.total),
					});
				},
				() => undefined,
			);
			return stream;
		};
	}
}

export default function onodaRecorder(pi: ExtensionAPI): void {
	const w = spool();
	if (!w) throw new Error("onoda-recorder: ONODA_PI_SPOOL is not set");
	const policy = readJsonEnv<GatewayPolicy>(ENV_POLICY);
	if (!policy) throw new Error("onoda-recorder: ONODA_PI_POLICY is not set");
	const runInfo = readJsonEnv<Record<string, unknown>>(ENV_RUN_INFO);
	if (!runInfo) throw new Error("onoda-recorder: ONODA_PI_RUN_INFO is not set");
	const aliases = replayAliases();
	// Every onoda extension has read its settings by now (the recorder loads last).
	scrubOnodaEnv();

	let started = false;
	let lastResponseRequest = 0; // request whose response proposed the current tool calls
	let answered = -1;
	let lastStop: { stopReason: string; errorMessage?: string } | undefined;
	const proposed = new Map<string, unknown>(); // tool call id -> arguments, from tool_execution_start
	const spooledCalls = new Set<string>(); // ids that have a tool.call line

	const spoolCall = (id: string, toolName: string, input: unknown, reason: string | undefined) => {
		spooledCalls.add(id);
		w.line({
			type: "tool.call",
			tool_call_id: id,
			tool_name: toolName,
			input: w.putJson(input ?? {}),
			request_index: lastResponseRequest,
			decision: reason ? "blocked" : "allowed",
			reason: reason ?? null,
		});
	};

	pi.on("session_start", (_event, ctx: ExtensionContext) => {
		if (started) return;
		started = true;
		recordSideRequests(ctx, w, policy, aliases);
		const sessionId = ctx.sessionManager.getSessionId();
		if (sessionId) aliases[sessionId] = "$SESSION";
		w.line({
			...runInfo,
			type: "spool.start",
			schema: SPOOL_SCHEMA,
			session_id: ctx.sessionManager.getSessionId(),
			session_dir: ctx.sessionManager.getSessionDir(),
		});
	});

	pi.on("before_provider_request", (event, ctx) => {
		if (ctx.model?.provider === SCRIPTED_PROVIDER) return; // it spools its own request
		const c = counters();
		const index = c.requests++;
		mainRequestSent(index);
		w.line({
			type: "provider.request",
			request_index: index,
			model: ctx.model?.id ?? "unknown",
			context: w.putJson(event.payload),
			source: "before_provider_request",
			replay_key: replayKey(event.payload, aliases),
			purpose: "agent",
		});
	});

	pi.on("message_end", (event) => {
		const m = event.message;
		if (m.role === "assistant") {
			const a = m as AssistantMessage;
			// An abort before the provider was called still ends with an assistant
			// message, but no request was sent for it: nothing to pair it with.
			const index = lastMainRequest();
			if (index === undefined || index <= answered) return;
			answered = lastResponseRequest = index;
			lastStop = { stopReason: a.stopReason, errorMessage: a.errorMessage };
			w.line({
				type: "provider.response",
				request_index: lastResponseRequest,
				message: w.putJson(a),
				stop_reason: a.stopReason,
				usage_tokens: usageTokens(a),
				cost_usd: addCost(a.usage?.cost?.total),
			});
			for (const block of a.content) {
				if (block.type === "toolCall") proposed.set(block.id, block.arguments);
			}
		} else if (m.role === "toolResult") {
			// Every result the model will see, including blocked and unknown-tool errors.
			const r = m as ToolResultMessage;
			if (!spooledCalls.has(r.toolCallId)) {
				spoolCall(r.toolCallId, r.toolName, proposed.get(r.toolCallId), REJECTED_BEFORE_TOOL_CALL);
			}
			w.line({
				type: "tool.result",
				tool_call_id: r.toolCallId,
				tool_name: r.toolName,
				stage: "visible",
				content: w.putJson(r.content),
				details: r.details === undefined ? null : w.putJson(r.details),
				is_error: r.isError,
			});
		}
	});

	pi.on("tool_execution_start", (event) => {
		proposed.set(event.toolCallId, event.args);
	});

	pi.on("tool_call", (event) => {
		const input = (event.input ?? {}) as Record<string, unknown>;
		// A72: cap one bash call's run time; the spooled input is what runs.
		const cap = policy.max_bash_seconds;
		if (event.toolName === "bash" && typeof cap === "number") {
			const asked = input.timeout;
			if (typeof asked !== "number" || !(asked > 0) || asked > cap) input.timeout = cap;
		}
		const reason = gatewayDecision(policy, event.toolName, input);
		spoolCall(event.toolCallId, event.toolName, input, reason);
		return reason ? { block: true, reason: `onoda gateway: ${reason}` } : undefined;
	});

	pi.on("session_before_compact", (event) => {
		w.line({
			type: "compaction",
			phase: "before",
			details: w.putJson({ reason: event.reason, willRetry: event.willRetry, customInstructions: event.customInstructions ?? null }),
		});
	});
	pi.on("session_compact", (event) => {
		w.line({ type: "compaction", phase: "done", details: w.putJson(event) });
	});
	pi.on("session_compact_failed", (event) => {
		w.line({ type: "compaction", phase: "failed", details: w.putJson(event) });
	});

	pi.on("turn_start", (_event, ctx) => {
		if (requestsUsed() >= policy.max_requests) {
			requestEnd("budget-exhausted", `max_requests ${policy.max_requests} reached`);
		}
		const cost = costExhausted(policy);
		if (cost) requestEnd("budget-exhausted", cost);
		// Also catches a failed reducer request, which SoL-Pi would otherwise absorb.
		if (pendingEnd()) ctx.abort();
	});

	pi.on("agent_settled", (_event, ctx) => {
		// An earlier extension (SoL-Pi's compactor) may have queued a continuation;
		// the run ends at the settle after it.
		if (!ctx.isIdle() || ctx.hasPendingMessages()) return;
		// A run whose last response was a provider error did not settle on its own.
		if (lastStop?.stopReason === "error") {
			writeEnd(w, "provider-error", lastStop.errorMessage ?? "provider error");
		} else {
			writeEnd(w, "settled", null);
		}
	});
	pi.on("session_shutdown", () => writeEnd(w, "aborted", "session shut down before the agent settled"));
}
