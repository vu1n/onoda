/**
 * Test-only extension: a stand-in for a live provider whose every request
 * fails (stopReason "error"), the way a real provider reports an API error.
 * It calls `onPayload`, so the recorder spools its requests as it would a
 * live provider's. Its output is synthetic.
 */
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
import type { ExtensionAPI, ProviderConfig } from "@earendil-works/pi-coding-agent";

type StreamSimple = NonNullable<ProviderConfig["streamSimple"]>;

export default function failingProvider(pi: ExtensionAPI): void {
	const zero = { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 };
	const reply: StreamSimple = (model, context, options) => {
		const stream = createAssistantMessageEventStream() as unknown as ReturnType<StreamSimple>;
		void (async () => {
			await options?.onPayload?.(context, model);
			const out = {
				role: "assistant" as const,
				content: [],
				api: model.api,
				provider: model.provider,
				model: model.id,
				usage: { ...zero, totalTokens: 0, cost: { ...zero, total: 0 } },
				stopReason: "error" as const,
				errorMessage: "synthetic invalid request (test fixture)",
				timestamp: Date.now(),
			};
			stream.push({ type: "error", reason: "error", error: out });
			stream.end();
		})();
		return stream;
	};
	pi.registerProvider("onoda-test-failing", {
		baseUrl: "http://onoda-test-failing.invalid",
		apiKey: "none",
		api: "onoda-test-failing" as Parameters<StreamSimple>[0]["api"],
		models: [
			{ id: "failing", name: "failing", reasoning: false, input: ["text"], cost: zero, contextWindow: 200000, maxTokens: 16384 },
		],
		streamSimple: reply,
	});
}
