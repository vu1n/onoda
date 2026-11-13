import hashlib
from pathlib import Path
from urllib.request import urlretrieve

MODELS = {
    "model": (
        "openai-community/gpt2",
        "607a30d783dfa663caf39e06633721c8d4cfcd7e",
        [
            "config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "vocab.json",
            "merges.txt",
            "model.safetensors",
        ],
    ),
    "reflector": (
        "Qwen/Qwen2.5-0.5B-Instruct",
        "7ae557604adf67be50417f59c2c2f167def9a775",
        [
            "config.json",
            "generation_config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "vocab.json",
            "merges.txt",
            "model.safetensors",
        ],
    ),
}
for dst, (repo, rev, files) in MODELS.items():
    Path(dst).mkdir(exist_ok=True)
    for f in files:
        p = Path(dst) / f
        if not p.exists():
            urlretrieve(f"https://huggingface.co/{repo}/resolve/{rev}/{f}", p)
for line in Path("weight-sha256.txt").read_text().splitlines():
    expected, file = line.split()
    actual = hashlib.file_digest(open(file, "rb"), "sha256").hexdigest()
    assert actual == expected, (file, actual)
print("Weights verified.")
