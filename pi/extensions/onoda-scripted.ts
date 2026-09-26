/**
 * onoda scripted provider (A32): serves a `Script`'s pre-written turns to the
 * real Pi process, offline. Its outputs are synthetic, never model results.
 *
 * Model `scripted` serves `turns`, `scripted-reducer` serves `reducer_turns`,
 * each strictly in order. Every request is spooled (`source:
 * scripted-provider`) with the context it received after all `context` hooks
 * and `before_provider_request` payload rewrites, before any reply is produced.
 * It fails closed: a turn whose `expect_replay_key` differs from the actual key
 * spools `replay.mismatch` and errors; an exhausted script errors. There is no
 * default reply.
 */

import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import type { ExtensionAPI, MessageEndEvent, ProviderConfig } from "@earendil-works/pi-coding-agent";
import { type EndStatus, mainRequestSent, requestEnd } from "./onoda-recorder.ts";
import {
	counters,
	ENV_POLICY,
	ENV_SCRIPT,
	type GatewayPolicy,
	readJsonEnv,
	replayKey,
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
}

interface Script {
	schema: "onoda.pi-script/1";
	synthetic: true;
	source: string;
	turns: ScriptTurn[];
	reducer_turns?: ScriptTurn[];
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
	const aliases = readJsonEnv<GatewayPolicy>(ENV_POLICY)?.path_aliases ?? {};
	const cursor = { [SCRIPTED_MODEL]: 0, [SCRIPTED_REDUCER_MODEL]: 0 } as Record<string, number>;

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
		let index: number | undefined;
		// Reducer calls go through complete(), which fires no message_end, so the
		// recorder never sees their responses; spool them here instead.
		const finish = (event: Parameters<EventStream["push"]>[0]) => {
			if (index !== undefined && model.id === SCRIPTED_REDUCER_MODEL) {
				w.line({
					type: "provider.response",
					request_index: index,
					message: w.putJson(out),
					stop_reason: out.stopReason,
					usage_tokens: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0 },
					cost_usd: "0",
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
				index = counters().requests++;
				if (model.id !== SCRIPTED_REDUCER_MODEL) mainRequestSent(index);
				const key = replayKey(payload, aliases);
				w.line({
					type: "provider.request",
					request_index: index,
					model: model.id,
					context: w.putJson(payload),
					source: "scripted-provider",
					replay_key: key,
				});
				const turns = model.id === SCRIPTED_REDUCER_MODEL ? (script?.reducer_turns ?? []) : (script?.turns ?? []);
				const turn = turns[cursor[model.id]++];
				if (!turn) return fail("script-exhausted", `script exhausted at request ${index} (model ${model.id})`);
				if (turn.expect_replay_key && turn.expect_replay_key !== key) {
					w.line({ type: "replay.mismatch", request_index: index, expected: turn.expect_replay_key, actual: key });
					return fail("replay-mismatch", `replay key mismatch at request ${index}`);
				}
				if (options?.signal?.aborted) {
					out.stopReason = "aborted";
					return finish({ type: "error", reason: "aborted", error: out });
				}
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
