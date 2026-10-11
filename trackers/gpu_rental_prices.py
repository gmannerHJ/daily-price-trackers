"""Daily snapshot of GPU rental list prices from providers that publish them
through an endpoint meant to be read without an account.

Sources:
  azure  Azure Retail Prices API (documented by Microsoft as unauthenticated)
         GPU virtual machine sizes (NC, ND, NV, NG families), Linux, in REGIONS
  verda  Verda (formerly DataCrunch) public instance-types endpoint

Writes data/gpu_rental_prices/YYYY-MM-DD.jsonl and updates changes.jsonl.
"""
import os
import re
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402

AZURE_URL = "https://prices.azure.com/api/retail/prices"
# A fixed list keeps the daily job to one request per region.
AZURE_REGIONS = (
    "eastus",
    "eastus2",
    "southcentralus",
    "westus2",
    "westus3",
    "northeurope",
    "westeurope",
    "uksouth",
    "swedencentral",
    "southeastasia",
    "japaneast",
    "australiaeast",
)
AZURE_FAMILIES = ("NC", "ND", "NV", "NG")
VERDA_URL = "https://api.verda.com/v1/instance-types"

KEY_FIELDS = ("source", "instance_type", "pricing", "region")
PRICE_FIELDS = ("price_usd_per_hour",)

# Parts of an Azure size name that are not the accelerator name.
_AZURE_NOT_GPU = re.compile(r"^(v\d+|Promo|xl|NDR|flex|noIB)$")


def azure_gpu_model(arm_sku_name):
    """Accelerator token from the size name, e.g. Standard_NC24ads_A100_v4 -> A100.

    Older sizes (for example Standard_NC6s_v3) do not carry it; those give None.
    """
    parts = arm_sku_name.strip().split("_")[2:]
    named = [p for p in parts if p and not _AZURE_NOT_GPU.match(p)]
    return named[0] if named else None


def fetch_azure():
    best = {}
    for region in AZURE_REGIONS:
        families = " or ".join("contains(armSkuName,'Standard_%s')" % f for f in AZURE_FAMILIES)
        flt = (
            "serviceName eq 'Virtual Machines' and priceType eq 'Consumption' "
            "and armRegionName eq '%s' and (%s)" % (region, families)
        )
        url = AZURE_URL + "?" + urllib.parse.urlencode({"$filter": flt})
        pages = 0
        while url and pages < 10:
            page = common.http_get_json(url)
            pages += 1
            for item in page.get("Items") or []:
                sku = (item.get("armSkuName") or "").strip()
                sku_name = item.get("skuName") or ""
                product = item.get("productName") or ""
                price = common.num(item.get("retailPrice"))
                if not sku or price is None or price <= 0:
                    continue
                if item.get("currencyCode") != "USD" or item.get("unitOfMeasure") != "1 Hour":
                    continue
                if item.get("type") != "Consumption":
                    continue
                if "Windows" in product or product.endswith(" Win") or "Low Priority" in sku_name:
                    continue
                if not sku.startswith(tuple("Standard_" + f for f in AZURE_FAMILIES)):
                    continue
                pricing = "interruptible" if "Spot" in sku_name else "on_demand"
                row = {
                    "source": "azure",
                    "provider": "Microsoft Azure",
                    "instance_type": sku,
                    "gpu_model": azure_gpu_model(sku),
                    "gpu_count": None,
                    "vram_gb": None,
                    "price_usd_per_hour": round(price, 6),
                    "pricing": pricing,
                    "region": region,
                }
                key = (sku, pricing, region)
                # Several meters can exist for one size; keep the primary,
                # most recently effective one, then the lowest price.
                rank = (
                    1 if item.get("isPrimaryMeterRegion") else 0,
                    item.get("effectiveStartDate") or "",
                    -price,
                )
                if key not in best or rank > best[key][0]:
                    best[key] = (rank, row)
            url = page.get("NextPageLink")
    return [row for _, row in best.values()]


def fetch_verda():
    data = common.http_get_json(VERDA_URL)
    rows = []
    for item in data:
        gpu = item.get("gpu") if isinstance(item.get("gpu"), dict) else {}
        count = common.num(gpu.get("number_of_gpus"))
        if not count or count < 1:
            continue  # CPU-only instance
        if str(item.get("currency", "")).lower() != "usd":
            continue
        memory = item.get("gpu_memory") if isinstance(item.get("gpu_memory"), dict) else {}
        total_vram = common.num(memory.get("size_in_gigabytes"))
        vram = None if not total_vram else round(total_vram / count, 2)
        if vram is not None and vram == int(vram):
            vram = int(vram)
        for field, pricing in (("price_per_hour", "on_demand"), ("spot_price", "interruptible")):
            price = common.num(item.get(field))
            if price is None or price <= 0:
                continue
            rows.append(
                {
                    "source": "verda",
                    "provider": "Verda",
                    "instance_type": item.get("instance_type"),
                    "gpu_model": item.get("model"),
                    "gpu_count": int(count),
                    "vram_gb": vram,
                    "price_usd_per_hour": round(price, 6),
                    "pricing": pricing,
                    "region": None,
                }
            )
    return rows


def main():
    common.run_tracker(
        "gpu_rental_prices",
        {"azure": fetch_azure, "verda": fetch_verda},
        KEY_FIELDS,
        PRICE_FIELDS,
        sort_key=lambda r: (r["source"], r["instance_type"] or "", r["pricing"], r["region"] or ""),
    )


if __name__ == "__main__":
    main()
