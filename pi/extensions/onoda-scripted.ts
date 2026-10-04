/**
 * onoda scripted provider (A32): serves a `Script`'s pre-written turns to the
 * real Pi process, offline. Its outputs are synthetic, never model results.
 *
 * Each request has a purpose: `reducer` (model `scripted-reducer`, SoL-Pi's
 * reducer) is served from `reducer_turns`; `summary` (a compaction summary:
 * Pi calls the provider without `options.onPayload`, which the agent loop
 * always passes) from `summary_turns`; `agent` from `turns`; each strictly in
 * order. Every request is spooled (`source: scripted-provider`, with its
 * `purpose`) with the context it received after all `context` hooks and
 * `before_provider_request` payload rewrites, before any reply is produced.
 * Reducer and summary responses are spooled here too (they fire no
 * message_end). It fails closed: a turn whose `expect_replay_key` differs from
 * the actual key spools `replay.mismatch` and errors; an exhausted script
 * errors. There is no default reply.
 *
 * A reducer or summary request made once `max_requests` requests were sent is
 * refused before it is sent (not spooled, not counted): the reply is an error,
 * SoL-Pi falls back to the unreduced output, and the run ends budget-exhausted.
 * A turn's `usage_cost` is a synthetic cost reported as Pi's cost estimate, so
 * tests and replays can drive `max_cost_usd`.
 *
 * Handoff (a fork's live continuation, `ENV_HANDOFF`): the script is the
 * parent's replayed prefix. At the first turn end after its last agent and
 * summary turn were served (at session start, for an empty prefix), the
 * session switches to the named live model, so every later request goes to
 * it and the recorder spools it as a live request. A model Pi cannot find, or
 * one without configured auth, ends the run `provider-error` before any live
 * request is sent.
 */

import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ExtensionContext, MessageEndEvent, ProviderConfig } from "@earendil-works/pi-coding-agent";
import { mainRequestSent, requestEnd } from "./onoda-recorder.ts";
import {
	addCost,
	counters,
	ENV_HANDOFF,
	ENV_POLICY,
	ENV_SCRIPT,
	type EndStatus,
	type GatewayPolicy,
	readJsonEnv,
	replayAliases,
	replayKey,
	requestsUsed,
	SCRIPTED_MODEL,
	SCRIPTED_PROVIDER,
	SCRIPTED_REDUCER_MODEL,
	spool,
} from "./lib/spool.ts";

// Pi aliases @earendil-works/pi-ai to its own bundled copy at runtime; take
// the types from Pi's API so they match that copy, not the hoisted one.
type StreamSimple = NonNullable<ProviderConfig["streamSimple"]>;
type Model = Parameters<StreamSimple>[0];
type Context = Parameters<StreamSimple>[1];
type SimpleStreamOptions = Parameters<StreamSimple>[2];
type EventStream = ReturnType<StreamSimple>;
type AssistantMessage = Extract<MessageEndEvent["message"], { role: "assistant" }>;
type Api = Model["api"];
type JsonObject = Extract<AssistantMessage["content"][number], { type: "toolCall" }>["arguments"];

type Block =
	| { type: "text"; text: string }
	| { type: "toolCall"; id: string; name: string; arguments: JsonObject };

interface ScriptTurn {
	content: Block[];
	expect_replay_key?: string | null;
	usage_cost?: string | null;
}

type Purpose = "agent" | "reducer" | "summary";

interface Handoff {
	provider: string;
	model: string;
}

interface Script {
	schema: "onoda.pi-script/1";
	synthetic: true;
	source: string;
	turns: ScriptTurn[];
	reducer_turns?: ScriptTurn[];
	summary_turns?: ScriptTurn[];
}

const ZERO_COST = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };

function loadScript(): Script | undefined {
	const script = readJsonEnv<Script>(ENV_SCRIPT);
	if (script && (script.schema !== "onoda.pi-script/1" || script.synthetic !== true)) {
		throw new Error(`onoda-scripted: ${process.env[ENV_SCRIPT]} is not a onoda.pi-script/1 synthetic script`);
	}
	return script;
}

function modelConfig(id: string) {
	return { id, name: id, reasoning: false, input: ["text" as const], cost: ZERO_COST, contextWindow: 200000, maxTokens: 16384 };
}

export default function onodaScripted(pi: ExtensionAPI): void {
	const w = spool();
	if (!w) throw new Error("onoda-scripted: ONODA_PI_SPOOL is not set");
	const script = loadScript();
	const policy = readJsonEnv<GatewayPolicy>(ENV_POLICY);
	const aliases = replayAliases(); // shared: the recorder adds the session id at session_start
	const cursor: Record<Purpose, number> = { agent: 0, reducer: 0, summary: 0 };
	const turnsFor: Record<Purpose, ScriptTurn[]> = {
		agent: script?.turns ?? [],
		reducer: script?.reducer_turns ?? [],
		summary: script?.summary_turns ?? [],
	};
	const handoff = readJsonEnv<Handoff>(ENV_HANDOFF);
	let handedOff = false;
	const handOff = async (ctx: ExtensionContext) => {
		if (!handoff || handedOff) return;
		if (cursor.agent < turnsFor.agent.length || cursor.summary < turnsFor.summary.length) return;
		handedOff = true;
		const model = ctx.modelRegistry.find(handoff.provider, handoff.model);
		if (!model || !(await pi.setModel(model))) {
			requestEnd("provider-error", `handoff to ${handoff.provider}/${handoff.model} failed: no such model or no auth`);
			ctx.abort();
		}
	};
	pi.on("session_start", (_event, ctx) => handOff(ctx));
	pi.on("turn_end", (_event, ctx) => handOff(ctx));

	const reply = (model: Model, context: Context, options?: SimpleStreamOptions): EventStream => {
		const stream = createAssistantMessageEventStream() as unknown as EventStream;
		const out: AssistantMessage = {
			role: "assistant",
			content: [],
			api: model.api,
			provider: model.provider,
			model: model.id,
			usage: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: { ...ZERO_COST, total: 0 } },
			stopReason: "stop",
			timestamp: Date.now(),
		};
		const purpose: Purpose =
			model.id === SCRIPTED_REDUCER_MODEL ? "reducer" : options?.onPayload ? "agent" : "summary";
		let index: number | undefined;
		// Reducer and summary calls fire no message_end, so the recorder never
		// sees their responses; spool them here instead.
		const finish = (event: Parameters<EventStream["push"]>[0]) => {
			if (index !== undefined && purpose !== "agent") {
				w.line({
					type: "provider.response",
					request_index: index,
					message: w.putJson(out),
					stop_reason: out.stopReason,
					usage_tokens: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0 },
					cost_usd: addCost(out.usage.cost.total),
				});
			}
			stream.push(event);
			stream.end();
		};
		const fail = (status: EndStatus, message: string) => {
			requestEnd(status, message);
			out.stopReason = "error";
			out.errorMessage = `onoda-scripted: ${message}`;
			finish({ type: "error", reason: "error", error: out });
		};

		void (async () => {
			try {
				const payload = ((await options?.onPayload?.(context, model)) ?? context) as Context;
				if (purpose !== "agent" && policy && requestsUsed() >= policy.max_requests) {
					// Refused before it is sent: nothing to spool, nothing counted.
					return fail("budget-exhausted", `${purpose} request refused: max_requests ${policy.max_requests} reached`);
				}
				index = counters().requests++;
				if (purpose === "agent") mainRequestSent(index);
				const key = replayKey(payload, aliases);
				w.line({
					type: "provider.request",
					request_index: index,
					model: model.id,
					context: w.putJson(payload),
					source: "scripted-provider",
					replay_key: key,
					purpose,
				});
				const turn = turnsFor[purpose][cursor[purpose]++];
				if (!turn) return fail("script-exhausted", `script exhausted at request ${index} (${purpose}, model ${model.id})`);
				if (turn.expect_replay_key && turn.expect_replay_key !== key) {
					w.line({ type: "replay.mismatch", request_index: index, expected: turn.expect_replay_key, actual: key });
					return fail("replay-mismatch", `replay key mismatch at request ${index}`);
				}
				if (options?.signal?.aborted) {
					out.stopReason = "aborted";
					return finish({ type: "error", reason: "aborted", error: out });
				}
				if (turn.usage_cost != null) out.usage.cost.total = Number(turn.usage_cost);
				stream.push({ type: "start", partial: out });
				for (const b of turn.content) {
					const contentIndex = out.content.length;
					if (b.type === "text") {
						out.content.push({ type: "text", text: b.text });
						stream.push({ type: "text_start", contentIndex, partial: out });
						stream.push({ type: "text_end", contentIndex, content: b.text, partial: out });
					} else {
						const toolCall = { type: "toolCall" as const, id: b.id, name: b.name, arguments: b.arguments };
						out.content.push(toolCall);
						stream.push({ type: "toolcall_start", contentIndex, partial: out });
						stream.push({ type: "toolcall_end", contentIndex, toolCall, partial: out });
					}
				}
				out.stopReason = turn.content.some((b) => b.type === "toolCall") ? "toolUse" : "stop";
				finish({ type: "done", reason: out.stopReason, message: out });
			} catch (error) {
				fail("aborted", `provider failure: ${error instanceof Error ? error.message : String(error)}`);
			}
		})();
		return stream;
	};

	pi.registerProvider(SCRIPTED_PROVIDER, {
		baseUrl: "http://onoda-scripted.invalid",
		apiKey: "none",
		api: SCRIPTED_PROVIDER as Api,
		models: [modelConfig(SCRIPTED_MODEL), modelConfig(SCRIPTED_REDUCER_MODEL)],
		streamSimple: reply,
	});
}
