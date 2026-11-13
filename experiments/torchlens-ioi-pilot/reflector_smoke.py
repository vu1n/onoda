import json
import os
import time

os.environ["HF_HUB_OFFLINE"] = "1"
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

torch.set_num_threads(2)
torch.manual_seed(0)
t = AutoTokenizer.from_pretrained("reflector", local_files_only=True)
m = AutoModelForCausalLM.from_pretrained("reflector", local_files_only=True).eval()
outs = []
for old in [[114, 117, 120], [0, 1, 2]]:
    prompt = (
        "You propose experiments. A candidate is three distinct attention head IDs, "
        f"integers 0 to 143. The current candidate {old} scored poorly. Change at least one ID. "
        "Return only a JSON list of three distinct integers; no explanation."
    )
    inputs = t.apply_chat_template(
        [{"role": "user", "content": prompt}],
        add_generation_prompt=True,
        return_tensors="pt",
        return_dict=False,
    )
    start = time.perf_counter()
    with torch.no_grad():
        out = m.generate(inputs, max_new_tokens=64, do_sample=False, pad_token_id=t.eos_token_id)
    text = t.decode(out[0, inputs.shape[1] :], skip_special_tokens=True)
    print(repr(text), flush=True)
    try:
        vals = json.loads(text.strip().removeprefix("```json").removesuffix("```").strip())
        valid = (
            isinstance(vals, list)
            and len(vals) == 3
            and len(set(vals)) == 3
            and all(isinstance(n, int) and 0 <= n < 144 for n in vals)
            and vals != old
        )
    except (ValueError, TypeError):
        valid = False
    outs.append({"old": old, "text": text, "valid": valid, "seconds": time.perf_counter() - start})
json.dump(outs, open("reflector-smoke.json", "w"), indent=2)
