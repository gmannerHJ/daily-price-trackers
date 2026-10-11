"""Daily snapshot of language-model API list prices.

Sources (both MIT-licensed, both published for programmatic use):
  litellm     https://github.com/BerriAI/litellm  model_prices_and_context_window.json
  models.dev  https://models.dev/api.json         (https://github.com/anomalyco/models.dev)

Writes data/llm_prices/YYYY-MM-DD.jsonl and updates data/llm_prices/changes.jsonl.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

LITELLM_URL = "https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json"
MODELS_DEV_URL = "https://models.dev/api.json"

KEY_FIELDS = ("source", "provider", "model_id")
PRICE_FIELDS = ("prompt_usd_per_mtok", "completion_usd_per_mtok")


def per_million(per_token):
    value = common.num(per_token)
    return None if value is None else round(value * 1e6, 6)


def whole(value):
    value = common.num(value)
    return None if value is None else int(value)


def fetch_litellm():
    data = common.http_get_json(LITELLM_URL)
    rows = []
    for model_id, spec in data.items():
        if model_id == "sample_spec" or not isinstance(spec, dict):
            continue
        prompt = per_million(spec.get("input_cost_per_token"))
        completion = per_million(spec.get("output_cost_per_token"))
        if prompt is None and completion is None:
            continue  # priced per image, per second or per call, not per token
        provider = spec.get("litellm_provider")
        if not isinstance(provider, str):
            continue
        context = whole(spec.get("max_input_tokens"))
        if context is None:
            context = whole(spec.get("max_tokens"))
        mode = spec.get("mode")
        rows.append(
            {
                "source": "litellm",
                "provider": provider,
                "model_id": model_id,
                "context_length": context,
                "prompt_usd_per_mtok": prompt,
                "completion_usd_per_mtok": completion,
                "modality": mode if isinstance(mode, str) else None,
            }
        )
    return rows


def fetch_models_dev():
    data = common.http_get_json(MODELS_DEV_URL)
    rows = []
    for provider_id, provider in data.items():
        models = provider.get("models") if isinstance(provider, dict) else None
        if not isinstance(models, dict):
            continue
        for model_id, spec in models.items():
            if not isinstance(spec, dict):
                continue
            cost = spec.get("cost") if isinstance(spec.get("cost"), dict) else {}
            prompt = common.num(cost.get("input"))
            completion = common.num(cost.get("output"))
            if prompt is None and completion is None:
                continue
            limit = spec.get("limit") if isinstance(spec.get("limit"), dict) else {}
            modalities = spec.get("modalities") if isinstance(spec.get("modalities"), dict) else {}
            inputs = [m for m in modalities.get("input") or [] if isinstance(m, str)]
            outputs = [m for m in modalities.get("output") or [] if isinstance(m, str)]
            modality = "+".join(inputs) + "->" + "+".join(outputs) if inputs or outputs else None
            rows.append(
                {
                    "source": "models.dev",
                    "provider": provider_id,
                    "model_id": model_id,
                    "context_length": whole(limit.get("context")),
                    "prompt_usd_per_mtok": None if prompt is None else round(prompt, 6),
                    "completion_usd_per_mtok": None if completion is None else round(completion, 6),
                    "modality": modality,
                }
            )
    return rows


def main():
    common.run_tracker(
        "llm_prices",
        {"litellm": fetch_litellm, "models.dev": fetch_models_dev},
        KEY_FIELDS,
        PRICE_FIELDS,
        sort_key=lambda r: (r["source"], r["provider"], r["model_id"]),
    )


if __name__ == "__main__":
    main()
