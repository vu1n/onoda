/**
 * Test-only extension: asks Pi to compact once, the first time the agent
 * settles, so a scripted run exercises Pi's compaction-summary requests.
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

export default function compactOnSettle(pi: ExtensionAPI): void {
	let done = false;
	pi.on("agent_settled", async (_event, ctx) => {
		if (done) return;
		done = true;
		await new Promise<void>((resolve) => ctx.compact({ onComplete: () => resolve(), onError: () => resolve() }));
	});
}
