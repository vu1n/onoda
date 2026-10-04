/**
 * Test-only extension: a stand-in for a live provider (not the scripted one),
 * so the recorder treats its calls as a real provider's. One model, `live`,
 * serves both the agent and SoL-Pi's reducer: agent turns come from the run's
 * script `turns`, reducer turns (told apart by the reducer's system prompt)
 * from `reducer_turns`. Like a real provider it calls `onPayload` and reports
 * a cost on every response. Its output is synthetic.
 *
 * `ONODA_TEST_LIVE_TURNS`, when set, names a script whose turns it serves
 * instead (a fork's handoff, where the run's own script is the prefix).
 */
import { readFileSync } from "node:fs";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ProviderConfig } from "@earendil-works/pi-coding-agent";

type StreamSimple = NonNullable<ProviderConfig["streamSimple"]>;
interface Turn {
	content: { type: string }[]; // text and toolCall blocks, already in Pi's shape
}

const COST = 0.001;

export default function liveProvider(pi: ExtensionAPI): void {
	// Read now: the recorder, loaded last, clears ONODA_PI_* from the environment.
	const source = process.env.ONODA_TEST_LIVE_TURNS ?? (process.env.ONODA_PI_SCRIPT as string);
	const script = JSON.parse(readFileSync(source, "utf8")) as {
		turns: Turn[];
		reducer_turns: Turn[];
	};
	const served = { agent: 0, reducer: 0 };
	const zero = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
	const reply: StreamSimple = (model, context, options) => {
		const stream = createAssistantMessageEventStream() as unknown as ReturnType<StreamSimple>;
		void (async () => {
			const role = context.systemPrompt?.startsWith("You are a lossless test/build output reducer") ? "reducer" : "agent";
			const out = {
				role: "assistant" as const,
				content: [] as unknown[],
				api: model.api,
				provider: model.provider,
				model: model.id,
				usage: { ...zero, input: 10, output: 5, totalTokens: 15, cost: { ...zero, total: COST } },
				stopReason: "stop" as "stop" | "toolUse" | "error",
				errorMessage: undefined as string | undefined,
				timestamp: Date.now(),
			};
			try {
				await options?.onPayload?.(context, model);
			} catch (error) {
				out.stopReason = "error";
				out.errorMessage = String(error);
				stream.push({ type: "error", reason: "error", error: out } as never);
				stream.end();
				return;
			}
			const turn = (role === "reducer" ? script.reducer_turns : script.turns)[served[role]++];
			out.content.push(...(turn?.content ?? [{ type: "text", text: "script exhausted (test fixture)" }]));
			if (out.content.some((b) => (b as { type: string }).type === "toolCall")) out.stopReason = "toolUse";
			stream.push({ type: "done", reason: out.stopReason, message: out } as never);
			stream.end();
		})();
		return stream;
	};
	pi.registerProvider("onoda-test-live", {
		baseUrl: "http://onoda-test-live.invalid",
		apiKey: "none",
		api: "onoda-test-live" as Parameters<StreamSimple>[0]["api"],
		models: [
			{ id: "live", name: "live", reasoning: false, input: ["text"], cost: zero, contextWindow: 200000, maxTokens: 16384 },
		],
		streamSimple: reply,
	});
}
