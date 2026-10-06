/**
 * `orca_input`: build an ORCA 6.1.1 input from chosen parameters (a pure function, no job).
 *
 * The agent picks the chemistry (method, basis, active space, roots per irrep,
 * orbitals to read and swap); this writes the syntax in the shapes that ran
 * here (experiments/questdb/memory/memory-project.md, "Inputs that ran in ORCA
 * 6.1.1"), with the geometry inline from the staged structure and %pal/%maxcore
 * within the broker's caps. The agent still submits the text with orca_submit,
 * so the broker boundary and the job count are unchanged.
 */

/** Within orca_broker.py `BrokerLimits` (max_nprocs 4, max_maxcore_mb 4000). */
export const INPUT_NPROCS = 4;
export const INPUT_MAXCORE_MB = 2000;
const BASIS_RE = /^[A-Za-z0-9][A-Za-z0-9()+*\-/]{0,39}$/;

export type InputMethod = "rhf" | "casscf" | "nevpt2";

export interface RootBlock {
	mult: 1 | 3;
	irrep: number;
	nroots: number;
}

export interface InputParams {
	method: InputMethod;
	basis: string;
	symmetry?: boolean; // UseSym, default on
	ri?: boolean; // RIJCOSX AutoAux, default on for CASSCF/NEVPT2, off for RHF
	charge?: number; // default 0 (closed-shell singlet reference)
	read_orbitals?: string; // job id whose job.gbw is read with MORead
	rotate?: { i: number; j: number }[]; // %scf rotate {i, j, 90}: swap orbitals i and j (0-based)
	nel?: number;
	norb?: number;
	roots?: RootBlock[]; // CASSCF/NEVPT2 blocks, ground state first; default one singlet root of irrep 0
	maxiter?: number; // %casscf maxiter, default 150
}

export interface BuiltInput {
	input: string;
	attach: { job_id: string; name: string }[];
	notes: string[];
}

/** The atom lines of an xyz file (after the count and comment lines), checked against its count. */
export function xyzAtomLines(xyz: string): string[] {
	const lines = xyz.split(/\r?\n/);
	const count = Number.parseInt(lines[0]?.trim() ?? "", 10);
	const atoms = lines
		.slice(2)
		.map((l) => l.trim())
		.filter((l) => l.length > 0);
	if (!Number.isInteger(count) || count !== atoms.length) {
		throw new Error(`orca_input: the structure's xyz declares ${lines[0]?.trim()} atoms but lists ${atoms.length}`);
	}
	return atoms;
}

/**
 * Build the input text; throws with the reason on parameters that would not run.
 * `gbwJobs` are this run's jobs that produced a job.gbw (read_orbitals must name one).
 */
export function buildOrcaInput(p: InputParams, atoms: string[], gbwJobs: ReadonlySet<string>): BuiltInput {
	const fail = (why: string): never => {
		throw new Error(`orca_input: ${why}`);
	};
	if (!BASIS_RE.test(p.basis)) fail(`${JSON.stringify(p.basis)} is not a basis set name (e.g. cc-pVDZ, aug-cc-pVTZ)`);
	const cas = p.method !== "rhf";
	const notes: string[] = [];
	const attach: BuiltInput["attach"] = [];

	const keywords = [cas ? null : "RHF", p.basis];
	if (p.ri ?? cas) keywords.push("RIJCOSX", "AutoAux");
	keywords.push("TightSCF");
	if (p.symmetry ?? true) keywords.push("UseSym");
	if (p.read_orbitals !== undefined) {
		if (!gbwJobs.has(p.read_orbitals)) {
			const known = [...gbwJobs].join(", ") || "none yet";
			fail(`read_orbitals: no job ${JSON.stringify(p.read_orbitals)} with a job.gbw in this run (jobs with one: ${known})`);
		}
		keywords.push("MORead");
		attach.push({ job_id: p.read_orbitals, name: "job.gbw" });
	}

	const out = [`! ${keywords.filter((k) => k !== null).join(" ")}`];
	if (p.read_orbitals !== undefined) out.push(`%moinp "${p.read_orbitals}.gbw"`);
	out.push(`%maxcore ${INPUT_MAXCORE_MB}`, `%pal nprocs ${INPUT_NPROCS} end`);

	const rotate = p.rotate ?? [];
	if (rotate.length) {
		if (p.read_orbitals === undefined) notes.push("rotate swaps the starting orbitals; it is usually paired with read_orbitals");
		for (const r of rotate) if (r.i === r.j) fail(`rotate: {${r.i}, ${r.j}} swaps an orbital with itself`);
		out.push("%scf", "  rotate", ...rotate.map((r) => `    {${r.i}, ${r.j}, 90}`), "  end", "end");
	}

	if (cas) {
		const { nel, norb } = p;
		if (nel === undefined || norb === undefined) fail(`${p.method} needs nel and norb (the active space)`);
		const n = nel as number;
		const m = norb as number;
		if (n > 2 * m) fail(`nel ${n} electrons do not fit in norb ${m} orbitals`);
		const roots = p.roots?.length ? p.roots : [{ mult: 1 as const, irrep: 0, nroots: 1 }];
		const first = roots[0];
		// The parser rejects a table whose lowest root is not the singlet ground state (memory-project.md).
		if (first.mult !== 1 || first.irrep !== 0) fail("roots: put the ground state first (mult 1, irrep 0)");
		const seen = new Set<string>();
		for (const r of roots) {
			const key = `${r.mult}/${r.irrep}`;
			if (seen.has(key)) fail(`roots: mult ${r.mult} irrep ${r.irrep} is listed twice; give one block its total nroots`);
			seen.add(key);
		}
		if (p.method === "nevpt2" && roots.length === 1 && first.nroots === 1) {
			notes.push("one root prints no transition energy table; add excited-state roots to report energies");
		}
		const list = (f: (r: RootBlock) => number) => roots.map(f).join(",");
		out.push(
			"%casscf",
			`  nel ${n}`,
			`  norb ${m}`,
			`  mult ${list((r) => r.mult)}`,
			`  irrep ${list((r) => r.irrep)}`,
			`  nroots ${list((r) => r.nroots)}`,
			// ORCA's default weights each block equally, not each root; bweight in the input's block
			// order (ORCA 6.1.1 applies it so) gives every root the same weight, as QUESTDB's references do.
			...(roots.length > 1 ? [`  bweight = ${list((r) => r.nroots)}`] : []),
			"  trafostep ri",
			"  orbstep TRAH",
			`  maxiter ${p.maxiter ?? 150}`,
			...(p.method === "nevpt2" ? ["  PTMethod SC_NEVPT2"] : []),
			"end",
		);
	} else if (p.nel !== undefined || p.norb !== undefined || p.roots !== undefined) {
		fail("rhf takes no active space or roots; use method casscf or nevpt2");
	}

	out.push(`* xyz ${p.charge ?? 0} 1`, ...atoms, "*", "");
	return { input: out.join("\n"), attach, notes };
}
