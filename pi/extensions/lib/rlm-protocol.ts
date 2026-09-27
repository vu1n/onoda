/**
 * RLM kernel protocol and RLM spool-line shapes. Mirrors the RLM section of
 * `src/onoda/adapters/pi/contract.py` field for field (KERNEL_PROTOCOL, the
 * Kernel* models, RlmComponents, and the rlm.* spool lines).
 *
 * JSON Lines over one AF_UNIX stream socket: the host (onoda-rlm.ts) listens,
 * the kernel connects and sends `ready`. Host -> kernel: `init` (first),
 * `exec`, `call.reply`, `shutdown`. Kernel -> host: `ready`, `exec.reply`,
 * `call`. A running cell may send `call` (rlm / commit_state) and blocks until
 * the matching `call.reply`. Ids are per-sender integers from 0.
 */

export const KERNEL_PROTOCOL = "onoda.rlm-kernel/1";
export const TOOL_IPYTHON = "ipython";

export const ENV_RLM_DEPTH = "ONODA_PI_RLM_DEPTH";
export const ENV_RLM_COMPONENTS = "ONODA_PI_RLM_COMPONENTS";
export const ENV_RLM_WORK = "ONODA_PI_RLM_WORK";
export const ENV_RLM_PYTHON = "ONODA_PI_RLM_PYTHON";

export interface RlmLimits {
	max_depth: number;
	max_children: number;
	max_output_chars: number;
}

export interface RlmComponents {
	schema: "onoda.pi-rlm-components/1";
	system_prompt: string;
	tool_doc: string;
	/** Template with {prompt} and {context}; rendered as the child's `-p` text. */
	child_prompt: string;
	helpers: string;
	limits: RlmLimits;
	candidate_digest: string;
}

// ----------------------------------------------------------- host -> kernel

export interface KernelInit {
	op: "init";
	id: number;
	protocol: typeof KERNEL_PROTOCOL;
	helpers: string;
	max_output_chars: number;
}

export interface KernelExec {
	op: "exec";
	id: number;
	code: string;
}

/** `ok: false` raises RlmError(error) inside the cell. */
export interface KernelCallReply {
	op: "call.reply";
	id: number;
	ok: boolean;
	value?: string | null;
	error?: string | null;
}

export interface KernelShutdown {
	op: "shutdown";
	id: number;
}

export type KernelRequest = KernelInit | KernelExec | KernelCallReply | KernelShutdown;

// ----------------------------------------------------------- kernel -> host

export interface KernelReady {
	op: "ready";
	protocol: typeof KERNEL_PROTOCOL;
	pid: number;
}

/** Also the reply to `init`. stdout ends with repr() of a trailing expression. */
export interface KernelExecReply {
	op: "exec.reply";
	id: number;
	stdout: string;
	stderr: string;
	error: string | null;
	truncated: boolean;
}

export interface KernelCall {
	op: "call";
	id: number;
	fn: "rlm" | "commit_state";
	/** rlm: {prompt, context}; commit_state: {name, value}. */
	args: Record<string, unknown>;
}

export type KernelReply = KernelReady | KernelExecReply | KernelCall;

// ------------------------------------------------------------- spool lines
// Fields a caller passes to SpoolWriter; `seq` and `mac` are assigned there.

export interface RlmCellLine {
	type: "rlm.cell";
	tool_call_id: string;
	cell_index: number;
	code: string;
	output: string;
	is_error: boolean;
	truncated: boolean;
}

export interface RlmChildStartLine {
	type: "rlm.child.start";
	tool_call_id: string;
	child_index: number;
	depth: number;
	/** `children/<nnnn>`, relative to the parent spool dir. */
	spool_subdir: string;
	prompt: string;
	max_requests: number;
}

export interface RlmChildEndLine {
	type: "rlm.child.end";
	child_index: number;
	exit_code: number | null;
	end_status: string | null;
	requests: number;
	cost_usd: string | null;
	answer: string | null;
	error?: string | null;
}

export interface RlmChildRefusedLine {
	type: "rlm.child.refused";
	tool_call_id: string;
	reason: "max-depth" | "max-children" | "budget";
	detail: string;
}

export interface RlmStateLine {
	type: "rlm.state";
	tool_call_id: string;
	name: string;
	value: string;
}

export type RlmLine = RlmCellLine | RlmChildStartLine | RlmChildEndLine | RlmChildRefusedLine | RlmStateLine;

/** `children/<nnnn>` for child index n. */
export function childSubdir(index: number): string {
	return `children/${String(index).padStart(4, "0")}`;
}
