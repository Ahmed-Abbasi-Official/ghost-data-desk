# Usage report

Final full-dataset run that produced `output.csv` (`python code/main.py`, 518 requests).

| Provider | Model | Model calls | Served from cache | Input tokens | Output tokens | Total tokens | Cost (USD) |
|---|---|---|---|---|---|---|---|
| ollama | qwen3.5:9b | 27 | 27 | 45777 | 14652 | 60429 | 0.00 (local) |
| **Overall** | | 27 | 27 | 45777 | 14652 | 60429 | 0.00 |

| Per request | Value |
|---|---|
| Average tokens per request | 116.7 |
| Average model calls per request | 0.052 |
| Actual cost per request | $0.00 (local Ollama model) |
| Estimated cost if the same tokens ran on claude-haiku-4-5 ($1.00 / $5.00 per MTok) | $0.1190 total, $0.000230 per request |

Calls by purpose: classify_request=16, image_extract=11

Token counts are the model's reported prompt/completion counts. Calls served from the on-disk cache
(`code/cache/llm/`) re-use the counts recorded when the call was first made, so the totals describe the
work needed to produce the output from scratch. The deterministic stages (parsing, identity, linking,
state, policy, planning, validation) make no model calls.

Classifier disagreements between rules and the LLM: 9.
