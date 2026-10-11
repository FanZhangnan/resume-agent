"""固定供应商目录：公网只能选择已审核的ID，不能改变Key的目的地址。"""

import json
import time

DEFOU = "defou"
NIM = "nvidia-nim"
NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
NIM_MODEL_OPTIONS = ({
    "id": "nvidia/nemotron-3.5-lightning-30b-a3b", "label": "Nemotron 3.5 Lightning",
    "tier": "byok", "tier_label": "自带 NIM Key",
    "status": "configured", "status_label": "已验证",
    "reasoning_levels": ("",), "default_reasoning": "",
    "supports_temperature": True, "supports_streaming": True,
    "token_parameter": "max_tokens",
    "extra_body": {"chat_template_kwargs": {"enable_thinking": False}},
},)


class ProviderError(ValueError):
    def __init__(self, code, message, status=400):
        self.code = code
        self.status = status
        super().__init__(message)


def normalize_provider(value=None):
    provider = str(value or DEFOU).strip()
    if provider not in (DEFOU, NIM):
        raise ProviderError("unsupported_provider", "请选择已支持的模型服务商")
    return provider


def model_options(provider=None):
    import config
    return config.MODEL_OPTIONS if normalize_provider(provider) == DEFOU else NIM_MODEL_OPTIONS


def resolve_model(provider=None, model=None, reasoning=None):
    import config
    provider = normalize_provider(provider)
    if provider == DEFOU:
        return config.validate_model_reasoning(model, reasoning)
    options = {item["id"]: item for item in NIM_MODEL_OPTIONS}
    model = str(model or NIM_MODEL_OPTIONS[0]["id"]).strip()
    if model not in options:
        raise ProviderError("unsupported_model", "所选模型不属于 NVIDIA NIM 的已验证模型")
    if reasoning not in (None, ""):
        raise ProviderError("unsupported_reasoning", "此 NIM 模型不使用 high/xhigh 参数")
    return model, ""


def base_url(provider=None):
    import config
    return config.API_BASE_URL if normalize_provider(provider) == DEFOU else NIM_BASE_URL


def validate_key_binding(provider, key):
    provider = normalize_provider(provider)
    key = str(key or "").strip()
    if provider == NIM and not key:
        raise ProviderError("provider_key_required", "NVIDIA NIM 需要填写自己的 API Key，不使用站点额度")
    if provider == DEFOU and key.startswith("nvapi-"):
        raise ProviderError("provider_key_mismatch", "这是 NVIDIA NIM Key，请先选择 NVIDIA NIM 服务商")
    return key


def public_catalog():
    import config
    return [{
        "id": provider,
        "label": config.PROVIDER_NAME if provider == DEFOU else "NVIDIA NIM",
        "requires_key": provider == NIM,
        "description": "可使用站点额度或得否 Key" if provider == DEFOU else "官方 NVIDIA 接口；仅使用你自己的 NIM Key",
        "default_model": config.DEFAULT_MODEL if provider == DEFOU else NIM_MODEL_OPTIONS[0]["id"],
        "model_options": [{k: list(v) if isinstance(v, tuple) else v for k, v in option.items()
                           if k in ("id", "label", "tier_label", "status_label", "reasoning_levels", "default_reasoning")}
                          for option in model_options(provider)],
    } for provider in (DEFOU, NIM)]


def check_connection(provider, model, reasoning, key):
    """只检查调用者的Key；不回退到站点Key，不返回供应商原始错误体。"""
    from llm_client import LLMClient
    provider = normalize_provider(provider)
    model, reasoning = resolve_model(provider, model, reasoning)
    key = validate_key_binding(provider, key)
    if not key:
        raise ProviderError("provider_key_required", "检测连接需要填写自己的 API Key")
    try:
        client = LLMClient(model=model, reasoning=reasoning, api_key=key, mock=False, provider=provider)
        client.streaming = False
        result = client.chat(
            [{"role": "user", "content": '只输出JSON对象：{"status":"ok"}'}],
            temperature=0, max_tokens=256, operation="provider_check",
            logical_deadline=time.monotonic() + 20,
        )
        content = str(getattr(result, "content", "") or "").strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        if json.loads(content).get("status") != "ok":
            raise ValueError
    except ProviderError:
        raise
    except Exception as error:
        status = getattr(error, "status_code", None)
        if status == 401:
            failure = ("provider_auth_failed", "所选服务商拒绝了此 API Key", 401)
        elif status == 403:
            failure = ("provider_access_denied", "此 Key 没有调用所选模型的权限", 403)
        elif status in (404, 410):
            failure = ("provider_model_unavailable", "所选模型已停用或当前不可调用", 422)
        elif status == 429:
            failure = ("provider_rate_limited", "服务商当前限流，请稍后重试", 429)
        elif isinstance(error, TimeoutError) or getattr(error, "is_timeout", False) or "Timeout" in type(error).__name__ or status in (408, 504):
            failure = ("provider_timeout", "服务商连接或响应超时，请稍后重试（检测上限20秒）", 504)
        elif status in (400, 422):
            failure = ("provider_parameters_rejected", "所选模型不接受当前请求参数", 422)
        elif isinstance(error, (ValueError, TypeError, AttributeError)):
            failure = ("provider_response_invalid", "模型未返回可用JSON，暂不能用于本分析流程", 422)
        else:
            failure = ("provider_unavailable", "服务商暂时无法完成检测，请稍后重试", 502)
        raise ProviderError(*failure) from None
    return {"ok": True, "provider": provider, "model": model,
            "message": "所选模型已返回可用 JSON；长任务仍可能遇到限流或超时"}
