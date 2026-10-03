"""Inventory QUESTDB's multireference data: data/cas/json tables and the SC-NEVPT2 columns.

Usage: uv run python cas_inventory.py /path/to/QUESTDB

Writes cas_inventory.json. Values are copied as stored (eV); nothing is converted.
Footnotes are read from the .docx tables because the repo's JSON conversion drops them.
"""

import collections
import json
import re
import subprocess
import sys
import zipfile
from decimal import Decimal
from pathlib import Path

src = Path(sys.argv[1])
here = Path(__file__).parent
SUBSETS = ("MAIN", "RAD", "TM", "CHROM", "BIO")
# cas table stem -> data/json stem, where the names differ by more than case/punctuation.
ALIASES = {
    "c2": "Carbon_dimer",
    "c3": "Carbon_trimer",
    "c2h": "Ethynyl",
    "criegee": "Criegee_Intermediate",
    "streptocyanine": "Streptocyanine-C1",
}


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9+]", "", name.lower())


def footnotes(docx: Path) -> tuple[str, list[str]]:
    """(table caption, paragraphs after the table) from the docx body, tags stripped."""
    xml = zipfile.ZipFile(docx).read("word/document.xml").decode()
    body = re.sub(r"<w:tbl>.*?</w:tbl>", "<TABLE/>", xml, flags=re.S)
    before, _, after = body.partition("<TABLE/>")

    def paragraphs(part: str) -> list[str]:
        out = []
        for p in re.findall(r"<w:p[ >].*?</w:p>", part, flags=re.S):
            text = "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p)).strip()
            if text:
                out.append(text)
        return out

    return " ".join(paragraphs(before)), paragraphs(after)


def agreement(data_rows: list[dict], cas_rows: list[dict]) -> str:
    """How the data file's SC-NEVPT2 column relates to the cas table's (sorted lists)."""
    data = sorted(Decimal(str(r["SC-NEVPT2"])) for r in data_rows)
    cas = sorted(
        Decimal(v)
        for row in cas_rows
        for k, v in row.items()
        if k.startswith("SC-NEVPT2") and re.fullmatch(r"-?\d+\.\d+", v or "")
    )
    if len(data) != len(cas):
        return f"different counts (data {len(data)}, cas {len(cas)})"
    if data == cas:
        return "identical"
    if all(abs(a - b) <= Decimal("0.005") for a, b in zip(data, cas, strict=True)):
        return "data file holds the cas value rounded to 2 decimals"
    return "different values"


data_files = {}
for subset in SUBSETS:
    for path in sorted((src / "data/json" / subset).glob("*.json")):
        data_files[norm(path.stem)] = (subset, path, json.loads(path.read_text()))
geoms = sorted(p.stem for p in (src / "geometries/xyz").glob("*.xyz"))

entries = []
for path in sorted((src / "data/cas/json").glob("*_mrpt.json")):
    stem = path.stem.removesuffix("_mrpt")
    key = norm(ALIASES.get(stem, stem))
    table = json.loads(path.read_text())
    caption, notes = footnotes(src / "data/cas/docx" / f"{stem}_mrpt.docx")
    entry = {
        "cas_file": f"data/cas/json/{path.name}",
        "caption": caption,
        "headers": table["headers"],
        "states": len(table["data"]),
        "rows": table["data"],
        "footnotes": notes,
        "data_file": None,
        "geometry_files": [
            f"geometries/xyz/{g}.xyz"
            for g in geoms
            if norm(g) == key or (norm(g).startswith(key) and norm(g)[len(key) :].isdigit())
        ],
    }
    if key in data_files:
        subset, dpath, rows = data_files[key]
        with_nev = [r for r in rows if r.get("SC-NEVPT2") is not None]
        entry.update(
            data_file=f"data/json/{subset}/{dpath.name}",
            subset=subset,
            heavy_atoms=rows[0].get("Size"),
            transitions=len(rows),
            transitions_with_sc_nevpt2=len(with_nev),
            sc_nevpt2_agreement=agreement(with_nev, table["data"]),
        )
    entries.append(entry)

nev_molecules = {
    subset: sorted(
        path.stem
        for s, path, rows in data_files.values()
        if s == subset and any(r.get("SC-NEVPT2") is not None for r in rows)
    )
    for subset in SUBSETS
}
commit = subprocess.check_output(["git", "-C", str(src), "rev-parse", "HEAD"], text=True).strip()
out = {
    "source": {
        "repository": "https://github.com/pfloos/QUESTDB",
        "commit": commit,
        "license": "CC-BY-SA-4.0",
    },
    "molecules_with_sc_nevpt2_in_data_json": nev_molecules,
    "cas_tables": entries,
}
(here / "cas_inventory.json").write_text(json.dumps(out, indent=1, ensure_ascii=False) + "\n")

matched = [e for e in entries if e["data_file"]]
stems = lambda es: [Path(e["cas_file"]).stem.removesuffix("_mrpt") for e in es]  # noqa: E731
print(f"cas tables: {len(entries)}, states: {sum(e['states'] for e in entries)}")
print(f"matched to a data file: {len(matched)}")
print("unmatched:", stems(e for e in entries if not e["data_file"]))
print("no geometry:", stems(e for e in entries if not e["geometry_files"]))
print(collections.Counter(e["sc_nevpt2_agreement"].split(" (")[0] for e in matched))
for e in matched:
    if e["sc_nevpt2_agreement"].startswith("different"):
        print("  ", Path(e["cas_file"]).stem, e["sc_nevpt2_agreement"])
print("with SC-NEVPT2 in data json:", {k: len(v) for k, v in nev_molecules.items()})
cas_names = {Path(e["data_file"]).stem for e in matched}
all_nev = {m for v in nev_molecules.values() for m in v}
print("SC-NEVPT2 in data json but no cas table:", sorted(all_nev - cas_names))
print("cas table but no SC-NEVPT2 in data json:", sorted(cas_names - all_nev))
cols = collections.Counter(
    re.sub(r" \(.*", "", h) for e in entries for h in e["headers"] if "NEVPT2" in h or "CAS" in h
)
print(cols)
