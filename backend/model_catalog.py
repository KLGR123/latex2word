from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, List, Tuple


DEFAULT_MARKUP_MULTIPLIER = 2.2
DEFAULT_MINIMUM_PRICE = 2.0


PROVIDER_CATALOG: Dict[str, Dict[str, Any]] = {
    "deepseek": {
        "label": "DeepSeek",
        "api_key_env": "DEEPSEEK_API_KEY",
        "base_url": "https://api.deepseek.com",
        "currency": "CNY",
        "pricing_as_of": "2026-04-24",
        "pricing_note": "参考官方公开 API 定价，生产环境请以后端账单配置为准。",
        "models": [
            {
                "id": "deepseek-chat",
                "label": "DeepSeek Chat",
                "description": "通用对话与翻译，默认推荐。",
                "context_window": 128000,
                "input_price_per_mtok": 2.0,
                "output_price_per_mtok": 8.0,
                "input_cache_hit_price_per_mtok": 0.5,
                "chars_per_token": 1.67,
                "default_output_ratio": 1.08,
                "markup_multiplier": 2.2,
                "minimum_price": 2.0,
            },
            {
                "id": "deepseek-reasoner",
                "label": "DeepSeek Reasoner",
                "description": "推理更强，但输出更贵，适合复杂结构修复。",
                "context_window": 64000,
                "input_price_per_mtok": 4.0,
                "output_price_per_mtok": 16.0,
                "input_cache_hit_price_per_mtok": 1.0,
                "chars_per_token": 1.67,
                "default_output_ratio": 1.15,
                "markup_multiplier": 2.0,
                "minimum_price": 3.0,
            },
        ],
    },
    "kimi": {
        "label": "Kimi",
        "api_key_env": "MOONSHOT_API_KEY",
        "base_url": "https://api.moonshot.cn/v1",
        "currency": "CNY",
        "pricing_as_of": "2026-04-24",
        "pricing_note": "参考官方公开 API 定价，生产环境请以后端账单配置为准。",
        "models": [
            {
                "id": "kimi-k2.5",
                "label": "Kimi K2.5",
                "description": "当前更强的通用模型，适合高质量翻译。",
                "context_window": 256000,
                "input_price_per_mtok": 4.0,
                "output_price_per_mtok": 21.0,
                "chars_per_token": 1.75,
                "default_output_ratio": 1.08,
                "markup_multiplier": 1.8,
                "minimum_price": 3.0,
            },
            {
                "id": "kimi-k2-0905-preview",
                "label": "Kimi K2 0905 Preview",
                "description": "代码和长上下文能力更强，适合复杂文档。",
                "context_window": 256000,
                "input_price_per_mtok": 4.0,
                "output_price_per_mtok": 16.0,
                "chars_per_token": 1.75,
                "default_output_ratio": 1.12,
                "markup_multiplier": 1.8,
                "minimum_price": 3.0,
            },
            {
                "id": "moonshot-v1-32k",
                "label": "Moonshot V1 32K",
                "description": "经典长上下文模型，适合常规论文翻译。",
                "context_window": 32768,
                "input_price_per_mtok": 5.0,
                "output_price_per_mtok": 20.0,
                "chars_per_token": 1.75,
                "default_output_ratio": 1.08,
                "markup_multiplier": 1.9,
                "minimum_price": 2.5,
            },
            {
                "id": "moonshot-v1-128k",
                "label": "Moonshot V1 128K",
                "description": "更长上下文，适合超长论文或合并项目。",
                "context_window": 131072,
                "input_price_per_mtok": 10.0,
                "output_price_per_mtok": 30.0,
                "chars_per_token": 1.75,
                "default_output_ratio": 1.1,
                "markup_multiplier": 1.7,
                "minimum_price": 4.0,
            },
        ],
    },
    "openai": {
        "label": "OpenAI",
        "api_key_env": "OPENAI_API_KEY",
        "base_url": None,
        "currency": "USD",
        "pricing_as_of": "2026-04-24",
        "pricing_note": "保留兼容；当前产品策略优先 DeepSeek/Kimi。",
        "models": [
            {
                "id": "gpt-5.4-mini-2026-03-17",
                "label": "GPT-5.4 Mini",
                "description": "历史默认选项，保留兼容。",
                "context_window": 128000,
                "input_price_per_mtok": 0.0,
                "output_price_per_mtok": 0.0,
                "chars_per_token": 1.6,
                "default_output_ratio": 1.08,
                "markup_multiplier": DEFAULT_MARKUP_MULTIPLIER,
                "minimum_price": DEFAULT_MINIMUM_PRICE,
            },
        ],
    },
    "anthropic": {
        "label": "Anthropic",
        "api_key_env": "ANTHROPIC_API_KEY",
        "base_url": None,
        "currency": "USD",
        "pricing_as_of": "2026-04-24",
        "pricing_note": "保留兼容；当前产品策略优先 DeepSeek/Kimi。",
        "models": [
            {
                "id": "claude-sonnet-4-5",
                "label": "Claude Sonnet",
                "description": "历史兼容占位，便于后续恢复支持。",
                "context_window": 200000,
                "input_price_per_mtok": 0.0,
                "output_price_per_mtok": 0.0,
                "chars_per_token": 1.6,
                "default_output_ratio": 1.08,
                "markup_multiplier": DEFAULT_MARKUP_MULTIPLIER,
                "minimum_price": DEFAULT_MINIMUM_PRICE,
            },
        ],
    },
}


DEFAULT_PROVIDER = "deepseek"
DEFAULT_MODEL = "deepseek-chat"


def get_provider_catalog() -> Dict[str, Dict[str, Any]]:
    return deepcopy(PROVIDER_CATALOG)


def list_provider_names() -> List[str]:
    return list(PROVIDER_CATALOG.keys())


def get_provider_defaults(provider: str) -> Tuple[str, str | None]:
    info = PROVIDER_CATALOG[provider]
    return str(info["api_key_env"]), info.get("base_url")


def find_model(provider: str, model: str) -> Dict[str, Any] | None:
    provider_info = PROVIDER_CATALOG.get(provider)
    if not provider_info:
        return None
    for item in provider_info.get("models", []):
        if item.get("id") == model:
            return deepcopy(item)
    return None


def validate_provider_and_model(provider: str, model: str) -> Dict[str, str]:
    normalized_provider = provider.strip().lower()
    normalized_model = model.strip()
    if normalized_provider not in PROVIDER_CATALOG:
        allowed = ", ".join(list_provider_names())
        raise ValueError(f"暂不支持该 provider。可选项：{allowed}")
    if not normalized_model:
        raise ValueError("model 不能为空。")
    if find_model(normalized_provider, normalized_model) is None:
        allowed_models = ", ".join(
            item["id"] for item in PROVIDER_CATALOG[normalized_provider]["models"]
        )
        raise ValueError(
            f"{normalized_provider} 不支持 model={normalized_model}。"
            f"可选项：{allowed_models}"
        )
    return {"provider": normalized_provider, "model": normalized_model}


def get_catalog_payload() -> Dict[str, Any]:
    providers: List[Dict[str, Any]] = []
    for provider, info in PROVIDER_CATALOG.items():
        providers.append({
            "id": provider,
            "label": info["label"],
            "api_key_env": info["api_key_env"],
            "base_url": info.get("base_url"),
            "currency": info["currency"],
            "pricing_as_of": info["pricing_as_of"],
            "pricing_note": info["pricing_note"],
            "models": deepcopy(info["models"]),
        })
    return {
        "defaults": {"provider": DEFAULT_PROVIDER, "model": DEFAULT_MODEL},
        "providers": providers,
    }
