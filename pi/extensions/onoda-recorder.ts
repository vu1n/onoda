/**
 * onoda recorder: loaded last, so every hook here sees what the model is
 * actually shown after all other extensions ran.
 *
 * It writes `spool.start`, provider requests for real providers (the scripted
 * provider spools its own requests and its reducer responses), main-model
 * responses, every tool call with the gateway decision (before the tool runs),
 * the visible tool results, compactions, and exactly one `spool.end`.
 * `spool.end` is always the last line: other extensions ask for a terminal
 * status with `requestEnd`; the recorder aborts at the next turn and writes it
 * when the agent settles (or at shutdown, as `aborted`, if it never did).
 *
 * `max_requests` counts every provider request, reducer calls included: the
 * budget is the budget.
 */

import type { ExtensionAPI, ExtensionContext, MessageEndEvent } from "@earendil-works/pi-coding-agent";
import {
	counters,
	ENV_POLICY,
	ENV_RUN_INFO,
	type GatewayPolicy,
	readJsonEnv,
	replayKey,
	SCRIPTED_PROVIDER,
	SPOOL_SCHEMA,
	type SpoolWriter,
	spool,
	TOOL_ORCA_RUN,
} from "./lib/spool.ts";

type AssistantMessage = Extract<MessageEndEvent["message"], { role: "assistant" }>;
type ToolResultMessage = Extract<MessageEndEvent["message"], { role: "toolResult" }>;

export type EndStatus = "settled" | "aborted" | "budget-exhausted" | "replay-mismatch" | "script-exhausted";

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

/** Blocking reason for a call under `policy`, or undefined when it may run. */
function gatewayDecision(policy: GatewayPolicy, toolName: string, input: Record<string, unknown>): string | undefined {
	const c = counters();
	// Calls proposed by a request past the budget (reducer requests count too).
	if (c.requests > policy.max_requests) return `request budget exhausted (${c.requests} > ${policy.max_requests})`;
	if (!policy.allowed_tools.includes(toolName)) return `tool ${toolName} is not allowed by the gateway policy`;
	const thenRun = input.then_run as { command?: unknown } | undefined;
	if (thenRun && typeof thenRun.command === "string") {
		const command = thenRun.command.toLowerCase();
		const token = policy.unfusable_command_tokens.find((t) => command.includes(t.toLowerCase()));
		if (token) return `then_run command contains unfusable token ${JSON.stringify(token)}`;
	}
	if (toolName === TOOL_ORCA_RUN && c.acquisitions >= policy.max_acquisitions) {
		return `acquisition budget exhausted (${policy.max_acquisitions})`;
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

export default function onodaRecorder(pi: ExtensionAPI): void {
	const w = spool();
	if (!w) throw new Error("onoda-recorder: ONODA_PI_SPOOL is not set");
	const policy = readJsonEnv<GatewayPolicy>(ENV_POLICY);
	if (!policy) throw new Error("onoda-recorder: ONODA_PI_POLICY is not set");
	const runInfo = readJsonEnv<Record<string, unknown>>(ENV_RUN_INFO);
	if (!runInfo) throw new Error("onoda-recorder: ONODA_PI_RUN_INFO is not set");

	let started = false;
	let lastResponseRequest = 0; // request whose response proposed the current tool calls
	let answered = -1;

	pi.on("session_start", (_event, ctx: ExtensionContext) => {
		if (started) return;
		started = true;
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
			replay_key: replayKey(event.payload, policy.path_aliases),
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
			const cost = a.usage?.cost?.total;
			w.line({
				type: "provider.response",
				request_index: lastResponseRequest,
				message: w.putJson(a),
				stop_reason: a.stopReason,
				usage_tokens: usageTokens(a),
				cost_usd: typeof cost === "number" && Number.isFinite(cost) ? String(cost) : null,
			});
		} else if (m.role === "toolResult") {
			// Every result the model will see, including blocked and unknown-tool errors.
			const r = m as ToolResultMessage;
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

	pi.on("tool_call", (event) => {
		const input = (event.input ?? {}) as Record<string, unknown>;
		const reason = gatewayDecision(policy, event.toolName, input);
		w.line({
			type: "tool.call",
			tool_call_id: event.toolCallId,
			tool_name: event.toolName,
			input: w.putJson(input),
			request_index: lastResponseRequest,
			decision: reason ? "blocked" : "allowed",
			reason: reason ?? null,
		});
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
		if (counters().requests >= policy.max_requests) {
			requestEnd("budget-exhausted", `max_requests ${policy.max_requests} reached`);
		}
		// Also catches a failed reducer request, which SoL-Pi would otherwise absorb.
		if (pendingEnd()) ctx.abort();
	});

	pi.on("agent_settled", (_event, ctx) => {
		// An earlier extension (SoL-Pi's compactor) may have queued a continuation;
		// the run ends at the settle after it.
		if (!ctx.isIdle() || ctx.hasPendingMessages()) return;
		writeEnd(w, "settled", null);
	});
	pi.on("session_shutdown", () => writeEnd(w, "aborted", "session shut down before the agent settled"));
}
