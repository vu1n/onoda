/**
 * onoda task extension: loaded first.
 *
 * Registers the task tools over a staged `TaskBundle` (verified against its
 * sha256 pins at load), appends the bundle's allowed documentation and
 * advisory ladder to the system prompt, and taps every raw `tool_result`
 * (`stage: raw`) before any later extension (SoL-Pi's reducer) rewrites it.
 *
 * `orca_run` never executes a solver: it hands the agent the pinned,
 * previously produced output and spools `task.acquire` with
 * `executed_here: false` before the agent sees a byte of it.
 */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import {
	counters,
	ENV_TASK,
	sha256Hex,
	spool,
	TOOL_CASE_INPUT,
	TOOL_ORCA_RUN,
	TOOL_REPORT,
} from "./lib/spool.ts";

interface TaskBundle {
	schema: "onoda.pi-task/1";
	case_id: string;
	instruction: string;
	input_name: string;
	input_sha256: string;
	output_name: string;
	output_sha256: string;
	output_provenance: "acquired";
	review_status: string;
	docs?: string[];
	ladder_sha256?: string | null;
	synthetic_ladder?: boolean;
}

const DECIMAL_RE = /^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$/;

/** Read a bundle file and check it against a pin given as `<hex>` or `sha256:<hex>`. */
function readPinned(dir: string, name: string, pin: string): Buffer {
	const bytes = readFileSync(join(dir, name));
	const actual = sha256Hex(bytes);
	if (pin.replace(/^sha256:/, "") !== actual) {
		throw new Error(`onoda-task: ${name} does not match its pinned sha256 (${pin}; actual ${actual})`);
	}
	return bytes;
}

function loadBundle(dir: string) {
	const task = JSON.parse(readFileSync(join(dir, "task.json"), "utf8")) as TaskBundle;
	if (task.schema !== "onoda.pi-task/1") throw new Error(`onoda-task: ${dir}/task.json is not onoda.pi-task/1`);
	if (task.output_provenance !== "acquired") throw new Error("onoda-task: only acquired outputs are supported");
	const input = readPinned(join(dir, "input"), task.input_name, task.input_sha256);
	const output = readPinned(join(dir, "output"), task.output_name, task.output_sha256);
	const sections: string[] = [];
	for (const name of [...(task.docs ?? [])].sort()) {
		sections.push(`## Allowed documentation: ${name}\n\n${readFileSync(join(dir, "docs", name), "utf8")}`);
	}
	if (task.ladder_sha256) {
		const ladder = readPinned(dir, "ladder.md", task.ladder_sha256).toString("utf8");
		sections.push(`## Advisory ladder${task.synthetic_ladder ? " (synthetic test fixture)" : ""}\n\n${ladder}`);
	}
	return { task, input, output, prompt: sections.join("\n\n") };
}

const Quantity = Type.Object(
	{
		text: Type.String({ description: "Decimal value exactly as printed, e.g. -230.123456" }),
		unit: Type.String({ description: "Unit as printed, e.g. Eh or eV" }),
	},
	{ additionalProperties: false },
);

const ReportParams = Type.Object(
	{
		final_energy: Type.Optional(Quantity),
		states: Type.Optional(
			Type.Array(
				Type.Object(
					{
						root: Type.Integer({ minimum: 1 }),
						multiplicity: Type.Optional(Type.Integer({ minimum: 1 })),
						energy: Quantity,
					},
					{ additionalProperties: false },
				),
			),
		),
		notes: Type.Optional(Type.String()),
	},
	{ additionalProperties: false },
);

export default function onodaTask(pi: ExtensionAPI): void {
	const w = spool();
	if (!w) throw new Error("onoda-task: ONODA_PI_SPOOL is not set");
	const dir = process.env[ENV_TASK];
	if (!dir) throw new Error("onoda-task: ONODA_PI_TASK is not set");
	const { task, input, output, prompt } = loadBundle(dir);
	const outputHex = sha256Hex(output);

	pi.registerTool({
		name: TOOL_CASE_INPUT,
		label: "Case input",
		description: `Read the staged ORCA input file for case ${task.case_id} (read-only).`,
		promptSnippet: "Read the staged ORCA input for this task",
		parameters: Type.Object({}, { additionalProperties: false }),
		async execute() {
			return {
				content: [{ type: "text", text: input.toString("utf8") }],
				details: { case_id: task.case_id, name: task.input_name, sha256: sha256Hex(input) },
			};
		},
	});

	pi.registerTool({
		name: TOOL_ORCA_RUN,
		label: "ORCA run",
		description:
			"Obtain the ORCA output for the staged input. Nothing is executed here: the tool returns the pinned output " +
			"that was previously produced for this exact input (recorded as acquired, not as a new execution).",
		promptSnippet: "Obtain the ORCA output for the staged input (acquired, not executed here)",
		parameters: Type.Object({}, { additionalProperties: false }),
		executionMode: "sequential",
		async execute(toolCallId) {
			counters().acquisitions++;
			w.line({
				type: "task.acquire",
				tool_call_id: toolCallId,
				case_id: task.case_id,
				output: w.putObject(output),
				executed_here: false,
			});
			return {
				content: [{ type: "text", text: output.toString("utf8") }],
				details: { case_id: task.case_id, name: task.output_name, sha256: outputHex, executed_here: false },
			};
		},
	});

	pi.registerTool({
		name: TOOL_REPORT,
		label: "Report states",
		description:
			"Report your final answer: the final energy and/or excited states exactly as printed in the output " +
			"(decimal text with its unit). Omit multiplicity when the output does not print it.",
		promptSnippet: "Report the final energy and excited states as your answer",
		parameters: ReportParams,
		async execute(toolCallId, params) {
			const quantities = [params.final_energy, ...(params.states ?? []).map((s) => s.energy)];
			for (const q of quantities) {
				if (q && !DECIMAL_RE.test(q.text)) throw new Error(`not a decimal: ${JSON.stringify(q.text)}`);
			}
			const claim = {
				schema: "onoda.pi-claim/1",
				case_id: task.case_id,
				final_energy: params.final_energy ?? null,
				states: (params.states ?? []).map((s) => ({
					root: s.root,
					multiplicity: s.multiplicity ?? null,
					energy: s.energy,
				})),
				notes: params.notes ?? "",
			};
			w.line({ type: "task.claim", tool_call_id: toolCallId, claim: w.putJson(claim) });
			return { content: [{ type: "text", text: "Claim recorded." }], details: claim };
		},
	});

	if (prompt) {
		pi.on("before_agent_start", (event) => ({ systemPrompt: `${event.systemPrompt}\n\n${prompt}` }));
	}

	pi.on("tool_result", (event) => {
		w.line({
			type: "tool.result",
			tool_call_id: event.toolCallId,
			tool_name: event.toolName,
			stage: "raw",
			content: w.putJson(event.content),
			details: event.details === undefined ? null : w.putJson(event.details),
			is_error: event.isError,
		});
	});
}
