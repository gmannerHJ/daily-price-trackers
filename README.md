# Daily price trackers

Two datasets, each recorded once a day so that a history builds up:

- **Language-model API prices** - list price per million tokens, per model and provider.
- **GPU rental prices** - hourly list price of GPU instances, on-demand and interruptible.

A current price can be looked up at any time. What it was last month cannot be
looked up afterwards unless someone wrote it down; that is what this repository does.

The data is collected and the pages are built by an automated pipeline run by one
person with AI agents. Nothing is edited by hand. Check the provider's own price
page before relying on a number.

## Layout

    data/<dataset>/YYYY-MM-DD.jsonl   one snapshot per UTC day (gzip-compressed after 30 days)
    data/<dataset>/changes.jsonl      every price change, new entry and removed entry
    data/<dataset>/runs.jsonl         per day: fetch time, row counts, which sources answered
    docs/                             static site built from data/ (served by GitHub Pages)
    trackers/                         the collectors
    build_pages.py                    the site builder

## Language-model API prices (`data/llm_prices`)

| Field | Meaning |
| --- | --- |
| `source` | `litellm` or `models.dev` |
| `provider` | provider identifier as given by the source |
| `model_id` | model identifier as given by the source |
| `context_length` | context window in tokens (LiteLLM: `max_input_tokens`, else `max_tokens`; models.dev: `limit.context`) |
| `prompt_usd_per_mtok` | input price, US dollars per million tokens |
| `completion_usd_per_mtok` | output price, US dollars per million tokens |
| `modality` | LiteLLM: its `mode` (for example `chat`, `embedding`); models.dev: input and output modalities, for example `text+image->text` |

Only models with a per-token price are included. The two sources overlap and do
not always agree; rows are kept separately per source and are not merged.

Both lists are maintained by their communities and cover resellers and aggregators
(for example OpenRouter and the large cloud platforms) as well as first-party
providers. Those rows come from the two lists; no request is sent to the providers
themselves. Values are passed through as listed, including ones that look
implausible (a price of 0, or a very high price for an unusual unit), and a list
can lag behind a provider's own change.

Sources:

- LiteLLM, `model_prices_and_context_window.json` in <https://github.com/BerriAI/litellm> (MIT licence).
- models.dev, <https://models.dev/api.json>, from <https://github.com/anomalyco/models.dev> (MIT licence).

## GPU rental prices (`data/gpu_rental_prices`)

| Field | Meaning |
| --- | --- |
| `source` | `azure` or `verda` |
| `provider` | provider name |
| `instance_type` | the provider's instance or size name |
| `gpu_model` | GPU name. Azure: taken from the size name when it carries one (for example `A100`), otherwise empty |
| `gpu_count` | number of GPUs. Empty for Azure, whose price API does not give it |
| `vram_gb` | memory per GPU in GB. Empty for Azure, whose price API does not give it |
| `price_usd_per_hour` | list price, US dollars per hour |
| `pricing` | `on_demand` or `interruptible` (Azure Spot, Verda spot) |
| `region` | region when the source gives one |

Sources:

- Microsoft Azure: the [Azure Retail Prices API](https://learn.microsoft.com/en-us/rest/api/cost-management/retail-prices/azure-retail-prices),
  which Microsoft documents as an unauthenticated API for retrieving retail prices programmatically.
  Linux prices for the NC, ND, NV and NG virtual machine families in twelve regions
  (listed in `trackers/gpu_rental_prices.py`); Windows and Low Priority meters are left out.
  One request per region per day.
- Verda (formerly DataCrunch): `https://api.verda.com/v1/instance-types`, which answers
  without an account. One request per day. Prices are not broken down by region.

GPU marketplaces and providers whose terms prohibit automated reading or the
building of price datasets, or whose price endpoint needs an account, are not read.

## Changes

`changes.jsonl` has one line per event, with `date`, `previous_date` (the snapshot
it was compared with) and `type`:

- `price_change` - with `field`, `old` and `new`;
- `new` - an entry that was not in the previous snapshot;
- `removed` - an entry that was in the previous snapshot and is gone.

If a source cannot be fetched on a given day, its rows are absent from that day's
snapshot (never carried over), nothing is recorded as removed, and the next
successful day is compared with the last day the source was present.

## Running it

Python 3.9 or later, standard library only.

    python trackers/llm_prices.py
    python trackers/gpu_rental_prices.py
    python build_pages.py

The site address used for canonical links, `sitemap.xml` and `feed.xml` is derived
from the repository name (`GITHUB_REPOSITORY` inside GitHub Actions, otherwise the
default in `trackers/common.py`); `--base-url` overrides it.

`.github/workflows/daily.yml` runs these once a day and commits when the data changed.
Requests carry a descriptive User-Agent and are limited to one per second per host.

## Licence

Data (`data/` and the CSV, JSON and Atom files under `docs/`): CC BY 4.0, see `LICENSE-DATA`.
Code: MIT, see `LICENSE`. Attribution for the MIT-licensed sources is in `NOTICE`.
