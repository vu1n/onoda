/**
 * onoda task extension: loaded first.
 *
 * Registers the task tools over a staged bundle (verified against its sha256
 * pins at load), appends the bundle's allowed documentation and advisory
 * ladder to the system prompt, and taps every raw `tool_result`
 * (`stage: raw`) before any later extension (SoL-Pi's reducer) rewrites it.
 *
 * A case bundle (`onoda.pi-task/1`) gets `case_input`, `orca_run` and
 * `report_states`. `orca_run` never executes a solver: it hands the agent the
 * pinned, previously produced output and spools `task.acquire` with
 * `executed_here: false` before the agent sees a byte of it.
 *
 * A VTE bundle (`onoda.pi-vte-task/1`, A57) gets `structure`, `orca_submit`
 * and `report_vtes`. `orca_submit` sends the agent's input to the host broker
 * over its Unix socket (ORCA never runs in this process) and spools
 * `task.execute` with the broker's own word on `executed_here`/`cache_hit`,
 * input and output objects written first, before the agent sees the output.
 * The output text is returned whole; shortening it is ObservationPack's job.
 */

import { chmodSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { createConnection } from "node:net";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import {
	BROKER_PROTOCOL,
	counters,
	ENV_BROKER,
	ENV_TASK,
	FILE_NAME_RE,
	JOB_ID_RE,
	MAX_INPUT_CHARS,
	type SpoolWriter,
	sha256Hex,
	spool,
	TOOL_CASE_INPUT,
	TOOL_ORCA_OUTPUT,
	TOOL_ORCA_RUN,
	TOOL_ORCA_SUBMIT,
	TOOL_REPORT,
	TOOL_REPORT_VTE,
	TOOL_STRUCTURE,
	VTE_CLAIM_SCHEMA,
	VTE_TASK_SCHEMA,
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

/** vte_contract.py `VteTaskBundle`. */
interface VteTaskBundle {
	schema: "onoda.pi-vte-task/1";
	case_id: string;
	instruction: string;
	molecule: string;
	point_group: string;
	structure_name: string;
	structure_sha256: string;
	targets: { spin: 1 | 3; irrep: string; count: number }[];
	review_status: string;
	docs?: string[];
	ladder_sha256?: string | null;
	synthetic_ladder?: boolean;
	max_executions: number;
}

/** orca_broker.py `BrokerReply` (re-exported by vte_contract.py). */
interface BrokerReply {
	protocol: "onoda.orca-broker/1";
	status: "completed" | "failed" | "refused";
	job_id: string | null;
	cache_hit: boolean;
	executed_here: boolean;
	input_sha256: string | null;
	output_sha256: string | null;
	output_b64: string;
	files: string[];
	reason: string | null;
	checks: string[]; // advisory ladder checks the broker read from the output (A66)
	summary: string[]; // the parser's reading of the output, with line numbers (A74)
}

const DECIMAL_RE = /^[+-]?(\d+\.\d*|\.\d+|\d+)([eE][+-]?\d+)?$/;
const DOC_NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]*\.md$/; // contract.py DOC_NAME_RE
const SPIN_NAMES: Record<number, string> = { 1: "singlet", 3: "triplet" };

/** `pin` (`<hex>` or `sha256:<hex>`) as bare hex. */
function bareHex(pin: string): string {
	return pin.replace(/^sha256:/, "");
}

/** Read a bundle file and check it against a pin given as `<hex>` or `sha256:<hex>`. */
function readPinned(dir: string, name: string, pin: string): Buffer {
	const bytes = readFileSync(join(dir, name));
	const actual = sha256Hex(bytes);
	if (bareHex(pin) !== actual) {
		throw new Error(`onoda-task: ${name} does not match its pinned sha256 (${pin}; actual ${actual})`);
	}
	return bytes;
}

/** The bundle's allowed docs and advisory ladder, as system-prompt sections. */
function promptSections(dir: string, task: TaskBundle | VteTaskBundle): string {
	const sections: string[] = [];
	for (const name of [...(task.docs ?? [])].sort()) {
		if (!DOC_NAME_RE.test(name)) throw new Error(`onoda-task: doc name ${JSON.stringify(name)} is not a plain *.md name`);
		sections.push(`## Allowed documentation: ${name}\n\n${readFileSync(join(dir, "docs", name), "utf8")}`);
	}
	if (task.ladder_sha256) {
		const ladder = readPinned(dir, "ladder.md", task.ladder_sha256).toString("utf8");
		sections.push(`## Advisory ladder${task.synthetic_ladder ? " (synthetic test fixture)" : ""}\n\n${ladder}`);
	}
	return sections.join("\n\n");
}

function loadBundle(dir: string, task: TaskBundle) {
	if (task.output_provenance !== "acquired") throw new Error("onoda-task: only acquired outputs are supported");
	const input = readPinned(join(dir, "input"), task.input_name, task.input_sha256);
	const output = readPinned(join(dir, "output"), task.output_name, task.output_sha256);
	return { task, input, output, prompt: promptSections(dir, task) };
}

function loadVteBundle(dir: string, task: VteTaskBundle) {
	if (!FILE_NAME_RE.test(task.structure_name)) {
		throw new Error(`onoda-task: structure name ${JSON.stringify(task.structure_name)} is not a plain file name`);
	}
	if (!Number.isInteger(task.max_executions) || task.max_executions < 1) {
		throw new Error("onoda-task: max_executions must be a positive integer");
	}
	for (const t of task.targets) {
		if (!SPIN_NAMES[t.spin] || typeof t.irrep !== "string" || !Number.isInteger(t.count) || t.count < 1) {
			throw new Error(`onoda-task: malformed target ${JSON.stringify(t)}`);
		}
	}
	const structure = readPinned(join(dir, "structure"), task.structure_name, task.structure_sha256);
	return { task, structure, prompt: promptSections(dir, task) };
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

function registerCaseTools(pi: ExtensionAPI, w: SpoolWriter, dir: string, raw: TaskBundle): string {
	const { task, input, output, prompt } = loadBundle(dir, raw);
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

	return prompt;
}

const SubmitParams = Type.Object(
	{
		input: Type.String({
			description: "The complete ORCA input file (.inp text), submitted exactly as written",
			minLength: 1,
			maxLength: MAX_INPUT_CHARS,
		}),
		attach: Type.Optional(
			Type.Array(
				Type.Object(
					{
						job_id: Type.String({ description: "An earlier job of this run, e.g. j0001" }),
						name: Type.String({
							description: 'A file that job produced, e.g. job.gbw; it arrives as j0001.gbw (%moinp "j0001.gbw")',
						}),
					},
					{ additionalProperties: false },
				),
				{ description: "Files of earlier jobs to place next to the input (e.g. orbitals for MOREAD)" },
			),
		),
	},
	{ additionalProperties: false },
);

const VteParams = Type.Object(
	{
		states: Type.Optional(
			Type.Array(
				Type.Object(
					{
						spin: Type.Union([Type.Literal(1), Type.Literal(3)], { description: "1 singlet, 3 triplet" }),
						irrep: Type.String({ description: "Irreducible representation, e.g. B1 or A''" }),
						character: Type.Optional(
							Type.String({ description: "Dominant excitation, e.g. n,pi* or pi,3s", maxLength: 64 }),
						),
						energy: Type.Object(
							{
								text: Type.String({ description: "Excitation energy exactly as printed, e.g. 3.912" }),
								unit: Type.String({ description: "eV" }),
							},
							{ additionalProperties: false },
						),
						job_id: Type.String({ description: "The orca_submit job whose output prints this energy" }),
					},
					{ additionalProperties: false },
				),
			),
		),
		notes: Type.Optional(Type.String()),
	},
	{ additionalProperties: false },
);

/** One request, one reply line, one connection (orca_broker.py broker protocol). */
function brokerRequest(socketPath: string, request: Record<string, unknown>): Promise<unknown> {
	return new Promise((resolve, reject) => {
		const sock = createConnection(socketPath);
		let buf = "";
		let done = false;
		const finish = (fn: () => void) => {
			if (done) return;
			done = true;
			sock.destroy();
			fn();
		};
		const parse = (line: string) => {
			try {
				resolve(JSON.parse(line));
			} catch {
				reject(new Error("orca_submit: the broker's reply is not JSON"));
			}
		};
		sock.setEncoding("utf8");
		sock.on("connect", () => sock.end(`${JSON.stringify(request)}\n`));
		sock.on("data", (chunk: string) => {
			buf += chunk;
			const nl = buf.indexOf("\n");
			if (nl >= 0) finish(() => parse(buf.slice(0, nl)));
		});
		sock.on("end", () => finish(() => (buf ? parse(buf) : reject(new Error("orca_submit: the broker closed without a reply")))));
		sock.on("error", (err) => finish(() => reject(new Error(`orca_submit: broker unreachable: ${err.message}`))));
	});
}

/**
 * Check a broker reply against the contract and against what was sent; the
 * output bytes must hash to the pin the broker journaled. Anything else
 * throws, so the agent never sees an output that is not what was recorded.
 */
function checkReply(raw: unknown, inputHex: string): { reply: BrokerReply; output: Buffer } {
	const r = raw as Partial<BrokerReply>;
	if (!r || typeof r !== "object" || r.protocol !== BROKER_PROTOCOL) {
		throw new Error(`orca_submit: the broker's reply is not ${BROKER_PROTOCOL}`);
	}
	const reply: BrokerReply = {
		protocol: BROKER_PROTOCOL,
		status: r.status as BrokerReply["status"],
		job_id: r.job_id ?? null,
		cache_hit: r.cache_hit === true,
		executed_here: r.executed_here === true,
		input_sha256: r.input_sha256 ?? null,
		output_sha256: r.output_sha256 ?? null,
		output_b64: r.output_b64 ?? "",
		files: r.files ?? [],
		reason: r.reason ?? null,
		checks: r.checks ?? [],
		summary: r.summary ?? [],
	};
	if (reply.cache_hit && reply.executed_here) throw new Error("orca_submit: broker reply is a cache hit and an execution");
	if (reply.status === "refused") {
		if (reply.job_id !== null || reply.executed_here || reply.cache_hit) {
			throw new Error("orca_submit: a refused broker reply names a job");
		}
		return { reply, output: Buffer.alloc(0) };
	}
	if (reply.status !== "completed" && reply.status !== "failed") {
		throw new Error(`orca_submit: unknown broker status ${JSON.stringify(r.status)}`);
	}
	if (typeof reply.job_id !== "string" || !JOB_ID_RE.test(reply.job_id)) {
		throw new Error(`orca_submit: broker job id ${JSON.stringify(reply.job_id)} is not j0001, ...`);
	}
	if (typeof reply.input_sha256 !== "string" || bareHex(reply.input_sha256) !== inputHex) {
		throw new Error("orca_submit: the broker pinned a different input than was sent");
	}
	if (!Array.isArray(reply.files) || !reply.files.every((f) => typeof f === "string" && FILE_NAME_RE.test(f))) {
		throw new Error("orca_submit: the broker's file list is malformed");
	}
	for (const [name, list] of [["checks", reply.checks], ["summary", reply.summary]] as const) {
		if (!Array.isArray(list) || !list.every((c) => typeof c === "string")) {
			throw new Error(`orca_submit: the broker's ${name} is malformed`);
		}
	}
	const output = Buffer.from(reply.output_b64, "base64");
	if (typeof reply.output_sha256 !== "string" || bareHex(reply.output_sha256) !== sha256Hex(output)) {
		throw new Error("orca_submit: the broker's output bytes do not match its output_sha256");
	}
	return { reply, output };
}

const OutputParams = Type.Object(
	{
		job_id: Type.Optional(
			Type.String({ description: "A job of this run, e.g. j0003; leave out to list every job of the run" }),
		),
		find: Type.Optional(
			Type.String({
				description: "Show only lines containing this text (case-insensitive), e.g. NEVPT2 or TRANSITION ENERGIES",
				minLength: 1,
				maxLength: 200,
			}),
		),
		context: Type.Optional(
			Type.Integer({ description: "Lines shown around each match (default 3)", minimum: 0, maximum: 40 }),
		),
	},
	{ additionalProperties: false },
);
const MAX_FOUND_LINES = 600;
const TAIL_LINES = 25; // of each output, shown in the orca_submit reply (where ORCA prints errors)
const JOBS_DIR = "jobs"; // in the workspace (Pi's cwd): read-only copies of each job's input and output

/**
 * Write jobs/<job_id>/{input.inp,output.out} under the workspace, read-only; returns the folder,
 * or null if the workspace refused it (the reply then omits the path; orca_output still works).
 */
function copyJob(jobId: string, input: Buffer, output: Buffer): string | null {
	const rel = join(JOBS_DIR, jobId);
	const dir = join(process.cwd(), rel);
	try {
		mkdirSync(dir, { recursive: true });
		for (const [name, data] of [["input.inp", input], ["output.out", output]] as const) {
			const path = join(dir, name);
			writeFileSync(path, data, { flag: "wx" });
			chmodSync(path, 0o444);
		}
	} catch {
		return null;
	}
	return rel;
}

/** The lines of `text` containing `find` (case-insensitive) with `context` lines around each, numbered. */
export function findLines(text: string, find: string, context: number): { shown: string; matches: number } {
	const lines = text.split("\n");
	const needle = find.toLowerCase();
	const keep = new Set<number>();
	let matches = 0;
	lines.forEach((line, i) => {
		if (!line.toLowerCase().includes(needle)) return;
		matches++;
		for (let j = Math.max(0, i - context); j <= Math.min(lines.length - 1, i + context); j++) keep.add(j);
	});
	const out: string[] = [];
	let last = -2;
	for (const i of [...keep].sort((a, b) => a - b)) {
		if (out.length >= MAX_FOUND_LINES) {
			out.push(`... (more matches; narrow the search)`);
			break;
		}
		if (i !== last + 1 && out.length) out.push("--");
		out.push(`${i + 1}: ${lines[i]}`);
		last = i;
	}
	return { shown: out.join("\n"), matches };
}

function registerVteTools(pi: ExtensionAPI, w: SpoolWriter, dir: string, raw: VteTaskBundle): string {
	const { task, structure, prompt } = loadVteBundle(dir, raw);
	const socketPath = process.env[ENV_BROKER];
	// Job id -> output bytes, as each orca_submit reply verified them (orca_output reads these).
	const outputs = new Map<string, Buffer>();
	// Job id -> its ledger entry: the reply's first line, the input's keyword lines and the parser summary (A74).
	const ledger = new Map<string, string[]>();
	if (!socketPath) throw new Error("onoda-task: ONODA_PI_BROKER is not set");

	const targets = task.targets.map((t) => `- ${t.count} ${SPIN_NAMES[t.spin]} ${t.irrep} state${t.count > 1 ? "s" : ""}`);
	const structureText = [
		`Molecule: ${task.molecule}`,
		`Point group: ${task.point_group}`,
		`Structure file: ${task.structure_name} (xyz, angstrom)`,
		"",
		structure.toString("utf8").trimEnd(),
		"",
		"Target excited states (vertical, from the ground state):",
		...targets,
		"",
		`At most ${task.max_executions} orca_submit calls in this run (repeats of an identical job count).`,
	].join("\n");

	pi.registerTool({
		name: TOOL_STRUCTURE,
		label: "Structure",
		description: `Read the molecular structure (xyz), point group and target states for case ${task.case_id} (read-only).`,
		promptSnippet: "Read the molecule's structure and the excited states to compute",
		parameters: Type.Object({}, { additionalProperties: false }),
		async execute() {
			return {
				content: [{ type: "text", text: structureText }],
				details: { case_id: task.case_id, name: task.structure_name, sha256: sha256Hex(structure), targets: task.targets },
			};
		},
	});

	pi.registerTool({
		name: TOOL_ORCA_SUBMIT,
		label: "ORCA submit",
		description:
			"Run one ORCA job on the host. The reply gives the job's status, what the deterministic parser read from " +
			"its output (termination, symmetry, CASSCF convergence, each transition energy with its line number) and " +
			"the output's last lines, not the whole output: read more with orca_output. Pass the full input file text; to reuse files " +
			"an earlier job produced (e.g. orbitals), list them in attach by job id and file name. An attached file is " +
			'placed next to the input as <job_id><suffix> (j0001 + job.gbw -> j0001.gbw), so read it with %moinp "j0001.gbw". ' +
			"An input identical to an earlier one (with the same attachments) returns that job's output from the cache " +
			"under a new job id. " +
			"The job directory holds only the input and its attachments: give the geometry inline (* xyz ... *), name " +
			"files only by their attached names, and keep %pal nprocs and %maxcore within the host's caps; an input that " +
			"names a path, sets %base, or starts other programs (%compound, ExtOpt, ProgExt) is refused (input_rejected). " +
			"Every call counts against the run's job limit. Cite the job id when you report a value from its output. " +
			"The job itself runs on the host; a read-only copy of each job's input and output is placed in your " +
			"workspace as jobs/<job_id>/input.inp and output.out. orca_output without a job id lists every job of " +
			"this run with that summary, so you need not search for them. " +
			"For a CASSCF job the reply also lists host checks read from its output: the lowest CASSCF root against " +
			"the Hartree-Fock energy of the same basis and geometry from this run, the active orbitals per irrep, and " +
			"whether every state-averaged root has the same weight.",
		promptSnippet: "Run an ORCA input on the host and read its output",
		parameters: SubmitParams,
		executionMode: "sequential",
		async execute(toolCallId, params) {
			const input = Buffer.from(params.input, "utf8");
			const attach = params.attach ?? [];
			for (const a of attach) {
				if (!JOB_ID_RE.test(a.job_id)) throw new Error(`attach: ${JSON.stringify(a.job_id)} is not a job id (j0001, ...)`);
				if (!FILE_NAME_RE.test(a.name)) throw new Error(`attach: ${JSON.stringify(a.name)} is not a plain file name`);
			}
			const inputHex = sha256Hex(input);
			const raw = await brokerRequest(socketPath, {
				protocol: BROKER_PROTOCOL,
				op: "submit",
				tool_call_id: toolCallId,
				input: params.input,
				attach,
			});
			// Counted as the broker counts (A59): a reply that names a job (completed
			// or failed, cache hits included), before its bytes are checked. A refusal
			// or a broker error spends nothing; the gateway blocks once the count
			// reaches max_executions.
			const named = raw as Partial<BrokerReply> | null;
			if (named && (named.status === "completed" || named.status === "failed") && typeof named.job_id === "string") {
				counters().executions++;
			}
			const { reply, output } = checkReply(raw, inputHex);
			if (reply.status === "refused") throw new Error(`orca_submit refused: ${reply.reason ?? "no reason given"}`);
			const jobId = reply.job_id as string; // checkReply: a reply that is not refused names its job
			w.line({
				type: "task.execute",
				tool_call_id: toolCallId,
				job_id: reply.job_id,
				status: reply.status,
				input: w.putObject(input),
				output: w.putObject(output),
				cache_hit: reply.cache_hit,
				executed_here: reply.executed_here,
			});
			// A72: a read-only copy in the workspace, after the bytes are spooled, so the
			// agent's own tools can find and search its jobs. The grader never reads it.
			const copy = copyJob(jobId, input, output);
			const how = reply.cache_hit ? "cache hit: an identical earlier job's output, not run again" : "executed";
			const first = `ORCA job ${jobId}: ${reply.status} (${how})${reply.reason ? `; ${reply.reason}` : ""}`;
			const keywords = params.input.split("\n").filter((l) => l.trimStart().startsWith("!")).map((l) => `input: ${l.trim()}`);
			outputs.set(jobId, output);
			ledger.set(jobId, [first, ...keywords, ...reply.summary]);
			const text = output.toString("utf8");
			const lines = text.split("\n");
			const tail = lines.slice(-TAIL_LINES).join("\n");
			const header = [
				first,
				`Files for attach: ${reply.files.length ? reply.files.join(", ") : "none"}`,
				`orca_submit calls left in this run: ${Math.max(0, task.max_executions - counters().executions)}`,
				...(reply.summary.length ? ["What the parser read from this output:", ...reply.summary] : []),
				...(reply.checks.length ? ["Host checks on this output (advisory):", ...reply.checks.map((c) => `- ${c}`)] : []),
				`Full output: ${lines.length} lines, read with orca_output (job_id ${jobId}, optionally find)` +
					(copy ? ` or in your workspace at ${copy}/output.out (input at ${copy}/input.inp)` : ""),
				`Last ${Math.min(TAIL_LINES, lines.length)} lines of the output:`,
			].join("\n");
			return {
				content: [
					{ type: "text", text: header },
					{ type: "text", text: tail },
				],
				details: {
					job_id: reply.job_id,
					status: reply.status,
					cache_hit: reply.cache_hit,
					executed_here: reply.executed_here,
					input_sha256: inputHex,
					output_sha256: sha256Hex(output),
					files: reply.files,
					checks: reply.checks,
					summary: reply.summary,
				},
			};
		},
	});

	pi.registerTool({
		name: TOOL_ORCA_OUTPUT,
		label: "ORCA output",
		description:
			"List this run's ORCA jobs (leave out job_id): each job's status, input keywords and what the parser read " +
			"from its output (termination, symmetry, CASSCF convergence, transition energies with line numbers). " +
			"With job_id, read that job's output again, whole or only the lines containing some text (with line " +
			"numbers). Costs no job.",
		promptSnippet: "Re-read an earlier ORCA job's output",
		parameters: OutputParams,
		async execute(_toolCallId, params) {
			if (params.job_id === undefined) {
				const text = ledger.size
					? [...ledger.values()].map((entry) => entry.join("\n")).join("\n\n")
					: "No ORCA jobs in this run yet.";
				return { content: [{ type: "text", text }], details: { jobs: [...ledger.keys()] } as Record<string, unknown> };
			}
			const output = outputs.get(params.job_id);
			if (output === undefined) {
				const known = [...outputs.keys()].join(", ") || "none yet";
				throw new Error(`orca_output: no job ${JSON.stringify(params.job_id)} in this run (jobs: ${known})`);
			}
			const text = output.toString("utf8");
			const details = { job_id: params.job_id, output_sha256: sha256Hex(output), find: params.find ?? null };
			if (params.find === undefined) {
				return { content: [{ type: "text", text: `ORCA job ${params.job_id}: whole output` }, { type: "text", text }], details };
			}
			const { shown, matches } = findLines(text, params.find, params.context ?? 3);
			const header = `ORCA job ${params.job_id}: ${matches} line(s) containing ${JSON.stringify(params.find)}`;
			return { content: [{ type: "text", text: matches ? `${header}\n${shown}` : header }], details: { ...details, matches } };
		},
	});

	pi.registerTool({
		name: TOOL_REPORT_VTE,
		label: "Report VTEs",
		description:
			"Report your final answer: each target state's vertical excitation energy in eV exactly as printed in the " +
			"output of the job you cite, with its spin, irrep and dominant character. Report only states you identified. " +
			"Write the character as the two orbitals separated by a comma, like n,pi* or pi,3s. " +
			"Cite a job run with point-group symmetry (UseSym) so its output prints each state's irrep; " +
			"a state from a job without printed irreps cannot be matched to a target.",
		promptSnippet: "Report the excited states' vertical transition energies as your answer",
		parameters: VteParams,
		async execute(toolCallId, params) {
			const states = params.states ?? [];
			for (const s of states) {
				if (!DECIMAL_RE.test(s.energy.text)) throw new Error(`not a decimal: ${JSON.stringify(s.energy.text)}`);
				if (s.energy.unit !== "eV") throw new Error(`excitation energies are reported in eV, not ${JSON.stringify(s.energy.unit)}`);
				if (!JOB_ID_RE.test(s.job_id)) throw new Error(`job id ${JSON.stringify(s.job_id)} is not a job id (j0001, ...)`);
			}
			const claim = {
				schema: VTE_CLAIM_SCHEMA,
				case_id: task.case_id,
				states: states.map((s) => ({
					spin: s.spin,
					irrep: s.irrep,
					character: s.character ?? "",
					energy: s.energy,
					job_id: s.job_id,
				})),
				notes: params.notes ?? "",
			};
			w.line({ type: "task.claim", tool_call_id: toolCallId, claim: w.putJson(claim) });
			return { content: [{ type: "text", text: "Claim recorded." }], details: claim };
		},
	});

	return prompt;
}

export default function onodaTask(pi: ExtensionAPI): void {
	const w = spool();
	if (!w) throw new Error("onoda-task: ONODA_PI_SPOOL is not set");
	const dir = process.env[ENV_TASK];
	if (!dir) throw new Error("onoda-task: ONODA_PI_TASK is not set");
	const raw = JSON.parse(readFileSync(join(dir, "task.json"), "utf8")) as TaskBundle | VteTaskBundle;
	let prompt: string;
	if (raw.schema === VTE_TASK_SCHEMA) {
		prompt = registerVteTools(pi, w, dir, raw);
	} else if (raw.schema === "onoda.pi-task/1") {
		prompt = registerCaseTools(pi, w, dir, raw);
	} else {
		throw new Error(`onoda-task: ${dir}/task.json is neither onoda.pi-task/1 nor ${VTE_TASK_SCHEMA}`);
	}

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
