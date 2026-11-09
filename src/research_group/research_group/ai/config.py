"""只读服务端配置；不接受客户端模型、URL或密钥。"""
import hashlib
import json
from urllib.parse import urlsplit

import frappe

DEFAULTS = {
    "minute_runs": 5, "daily_runs": 50, "input_chars": 12000,
    "context_tokens": 24000, "output_tokens": 2000, "model_calls": 4,
    "tool_calls": 6, "run_seconds": 90, "queue_size": 20, "queue_seconds": 60,
    "user_bytes": 20 * 1024 * 1024, "site_bytes": 200 * 1024 * 1024,
    "memory_count": 200, "user_daily_tokens": 5200000, "site_daily_tokens": 20000000,
}


def _read_configuration():
    # frappe.conf and Frappe's cached site loader may remain stale in a worker.
    from frappe.config import get_site_config
    return get_site_config(cached=False)


def server_configuration():
    try:
        return _read_configuration()
    except Exception:
        frappe.throw("无法读取共享模型服务配置。")


def limits(configuration=None):
    supplied = (configuration if configuration is not None else server_configuration()).get("rg_ai_limits") or {}
    if not isinstance(supplied, dict) or set(supplied) - set(DEFAULTS):
        frappe.throw("AI额度配置无效。")
    result = {}
    for key, default in DEFAULTS.items():
        value = supplied.get(key, default)
        if type(value) is not int or value <= 0 or value > default * 100:
            frappe.throw("AI额度配置无效。")
        result[key] = value
    return result


def mode(configuration=None):
    value = (configuration if configuration is not None else server_configuration()).get("rg_ai_mode", "mock")
    # Legacy stored 'agents' is an explicit compatibility alias, not the engine.
    if value not in {"mock", "disabled", "agents", "langchain"}:
        frappe.throw("AI供应商配置无效。")
    return value


def public_configuration():
    value = mode()
    return {"mode": value, "engine": "LangChain + LangGraph", "scope": "仅本站工作",
            "label": "LangChain + LangGraph · 站内离线模拟 · 不调用真实模型" if value == "mock" else
            ("AI助手已关闭" if value == "disabled" else "LangChain + LangGraph · 共享模型服务 · 仅本站工作"), "limits": limits()}


def configuration_version():
    from research_group.ai.graph import GRAPH_VERSION, SCOPE_VERSION
    # 凭据不进入哈希输入、审计、SDK上下文或运行序列化。
    current = server_configuration()
    endpoint = current.get("rg_ai_base_url") or ""
    allowed = current.get("rg_ai_allowed_base_urls") or []
    if not isinstance(endpoint, str) or not isinstance(allowed, list) or any(not isinstance(url, str) for url in allowed):
        frappe.throw("模型端点配置无效。")
    for value in [endpoint, *allowed]:
        if not value:
            continue
        parsed = urlsplit(value)
        # Reject credential-bearing URLs before including endpoints in a hash.
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            frappe.throw("模型端点配置无效。")
    config = {"engine": "langchain", "graph": GRAPH_VERSION, "scope": SCOPE_VERSION,
              "mode": mode(current), "model": current.get("rg_ai_model", ""),
              "base_url": endpoint, "allowed_base_urls": sorted(set(allowed)),
              "real_enabled": bool(current.get("rg_ai_real_enabled")),
              "protocol": current.get("rg_ai_protocol", "responses"),
              "revision": str(current.get("rg_ai_config_version", "1")), "limits": limits(current)}
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
