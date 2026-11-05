"""
astrbot_plugin_gemini_null_guard

部分 Gemini 中转（OpenAI 兼容接口）把 tool 消息转成 function_response /
function_call 时，会把数组里的元素变成 null，然后报 400：
  Invalid value at 'contents[N].parts[M].function_response...'
  Invalid value at 'contents[N].parts[M].function_call.args...'
一条坏记录留在上下文里，之后每次请求都会失败。

做法：给 ProviderOpenAIOfficial._finally_convert_payload 套一层，
原逻辑跑完后，对命中关键词的模型：
  - tool 消息：结果里只要有数组或 null，就把原文整段包成 {"result": "..."} 字符串
  - assistant 历史里的 tool_calls：参数里有数组或 null，就包成 {"_raw_args": "..."}
  - 发给模型的工具清单：删掉 Gemini 不认的 schema 字段（default、additionalProperties 等），
    有用的信息挪进 description
只改发出去的请求，不改数据库里存的对话记录，也不改工具本身的定义。
"""

import copy
import json

from astrbot.api import logger
from astrbot.api.star import Context, Star, register
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

_ATTR = "_finally_convert_payload"
_ATTR_TOOLS = "_apply_provider_specific_request_overrides"
_MARK = "_gemini_null_guard_original"

# 运行时设置，由插件配置覆盖
_SETTINGS = {
    "keywords": ["gemini"],
    "match_all": False,
    "verbose": False,
    "clean_tools": True,
}


def _has_list_or_null(v) -> bool:
    if v is None or isinstance(v, list):
        return True
    if isinstance(v, dict):
        return any(_has_list_or_null(x) for x in v.values())
    return False


def _text_parts_to_str(content: list):
    """OpenAI 格式的 tool content 可能是 [{"type":"text","text":"..."}]。
    全是文本段时拼成一段字符串返回，否则返回 None。"""
    texts = []
    for part in content:
        if not (isinstance(part, dict) and part.get("type") == "text"):
            return None
        texts.append(str(part.get("text", "")))
    return "\n".join(texts)


def _clean_tool_content(content):
    """返回改写后的字符串；不需要改时返回 None。

    实测中转会把 function_response 里任何数组的元素都变成 null（连 [1,"a"] 都不行），
    但字符串完整保留。所以只要结果里有数组或 null，就把原文整段当字符串包进
    {"result": "..."}，模型照样能读到全部内容。
    """
    if isinstance(content, list):
        joined = _text_parts_to_str(content)
        if joined is not None:
            content = joined  # 按纯文本继续往下判断

    if isinstance(content, (dict, list)):
        raw = json.dumps(content, ensure_ascii=False)
        parsed = content
    elif isinstance(content, str):
        raw = content
        try:
            parsed = json.loads(content)
        except (json.JSONDecodeError, ValueError):
            return None  # 纯文本由原逻辑包成 {"result": ...}
    else:
        return None

    if isinstance(parsed, dict):
        if set(parsed) == {"result"} and isinstance(parsed["result"], str):
            return None  # 已经包好的
        if not _has_list_or_null(parsed):
            return None  # 普通对象，中转能正常处理
    # 数组、null、带数组的对象、以及数字/布尔这类非对象 JSON，都包成字符串
    return json.dumps({"result": raw}, ensure_ascii=False)


def _clean_call_arguments(arguments):
    """历史里 assistant 的 tool_calls 参数同样会被中转转坏：
    参数里只要有数组（比如 send_message_to_user 的 messages），元素就变成 null。
    这些是已经执行完的历史调用，不会再执行，所以把整段参数原文包成
    {"_raw_args": "..."} 字符串，模型照样看得到当时传了什么。
    返回改写后的字符串；不需要改时返回 None。
    """
    if isinstance(arguments, dict):
        raw = json.dumps(arguments, ensure_ascii=False)
        parsed = arguments
    elif isinstance(arguments, str):
        raw = arguments
        try:
            parsed = json.loads(arguments) if arguments.strip() else {}
        except (json.JSONDecodeError, ValueError):
            return None
    else:
        return None
    if not isinstance(parsed, dict):
        return json.dumps({"_raw_args": raw}, ensure_ascii=False)
    if set(parsed) == {"_raw_args"}:
        return None  # 已经包好的
    if not any(_has_list_or_null(v) for v in parsed.values()):
        return None
    return json.dumps({"_raw_args": raw}, ensure_ascii=False)


# Gemini 函数声明不认、但 JSON Schema 里常见的字段。中转原样转发时会报
# Invalid JSON payload / Unknown name。删掉后把有用的信息挪进 description。
_DROP_KEYS = ("$schema", "$id", "$comment", "additionalProperties", "default", "examples")


def _append_desc(node: dict, text: str) -> None:
    desc = str(node.get("description", "") or "").strip()
    node["description"] = f"{desc}\n{text}".strip() if desc else text


def _clean_schema(node) -> int:
    """原地清理一份 JSON Schema（调用方必须先深拷贝）。返回改动次数。"""
    if isinstance(node, list):
        return sum(_clean_schema(x) for x in node)
    if not isinstance(node, dict):
        return 0
    n = 0
    if "default" in node:
        d = node.pop("default")
        if d not in (None, "", [], {}):
            _append_desc(node, f"默认值: {json.dumps(d, ensure_ascii=False)}")
        n += 1
    if "additionalProperties" in node:
        ap = node.pop("additionalProperties")
        if isinstance(ap, dict) and ap.get("type"):
            _append_desc(node, f"键值对，值的类型为 {ap['type']}")
        n += 1
    if "examples" in node:
        ex = node.pop("examples")
        if ex:
            _append_desc(node, f"示例: {json.dumps(ex, ensure_ascii=False)}")
        n += 1
    for k in ("$schema", "$id", "$comment"):
        if k in node:
            node.pop(k)
            n += 1
    # Gemini 只接受字符串 enum：非字符串类型把可选值挪进描述，类型不变
    if "enum" in node and node.get("type") not in (None, "string"):
        values = node.pop("enum")
        _append_desc(node, "可选值: " + ", ".join(json.dumps(v, ensure_ascii=False) for v in values))
        n += 1
    # Gemini 要求 array 必须带 items
    if node.get("type") == "array" and not isinstance(node.get("items"), dict):
        node["items"] = {"type": "string"}
        n += 1
    for key, value in node.items():
        if key == "properties" and isinstance(value, dict):
            for sub in value.values():
                n += _clean_schema(sub)
        elif key in ("items", "anyOf", "oneOf", "allOf", "not"):
            n += _clean_schema(value)
    return n


def _guard_tools(payloads: dict) -> int:
    """清理发给模型的工具清单。tools 里的 parameters 是工具对象本身那份的引用，
    必须深拷贝后再改，否则会改坏工具的真实定义。"""
    tools = payloads.get("tools")
    if not isinstance(tools, list) or not tools:
        return 0
    new_tools = copy.deepcopy(tools)
    n = 0
    for t in new_tools:
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and isinstance(fn.get("parameters"), dict):
            n += _clean_schema(fn["parameters"])
    if n:
        payloads["tools"] = new_tools
    return n


def _should_guard(provider, payloads: dict) -> bool:
    if _SETTINGS["match_all"]:
        return True
    model = str(payloads.get("model", "")).lower()
    try:
        base_url = str(provider.client.base_url).lower()
    except Exception:
        base_url = ""
    for kw in _SETTINGS["keywords"]:
        kw = str(kw).strip().lower()
        if kw and (kw in model or kw in base_url):
            return True
    return False


def _guard_payload(payloads: dict) -> int:
    n = 0
    for message in payloads.get("messages", []) or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if role == "tool":
            new = _clean_tool_content(message.get("content"))
            if new is not None:
                message["content"] = new
                n += 1
        elif role == "assistant":
            for call in message.get("tool_calls") or []:
                fn = call.get("function") if isinstance(call, dict) else None
                if not isinstance(fn, dict):
                    continue
                new = _clean_call_arguments(fn.get("arguments"))
                if new is not None:
                    fn["arguments"] = new
                    n += 1
    return n


def _install():
    """挂两处：
    1. _finally_convert_payload：messages 组装完之后，处理工具结果和历史调用参数
    2. _apply_provider_specific_request_overrides：_query / _query_stream 把 tools
       塞进请求之后、真正发出之前都会调用它，在这里清理工具清单
       （_finally_convert_payload 跑的时候 tools 还没放进去）
    """
    cur = getattr(ProviderOpenAIOfficial, _ATTR)
    if getattr(cur, _MARK, None) is None:
        original = cur

        def patched(self, payloads: dict) -> None:
            original(self, payloads)
            try:
                if not _should_guard(self, payloads):
                    return
                n = _guard_payload(payloads)
                if n or _SETTINGS["verbose"]:
                    logger.info(
                        f"[gemini_null_guard] model={payloads.get('model')} wrapped={n}"
                    )
            except Exception as e:  # 兜底：清理失败也不影响正常请求
                logger.warning(f"[gemini_null_guard] 清理失败，已跳过: {e}")

        setattr(patched, _MARK, original)
        setattr(ProviderOpenAIOfficial, _ATTR, patched)

    cur2 = getattr(ProviderOpenAIOfficial, _ATTR_TOOLS, None)
    if cur2 is not None and getattr(cur2, _MARK, None) is None:
        original2 = cur2

        def patched_tools(self, payloads, extra_body, *args, **kwargs):
            try:
                if _SETTINGS["clean_tools"] and _should_guard(self, payloads):
                    s = _guard_tools(payloads)
                    if s or _SETTINGS["verbose"]:
                        logger.info(
                            f"[gemini_null_guard] model={payloads.get('model')} schema_fixed={s}"
                        )
            except Exception as e:
                logger.warning(f"[gemini_null_guard] 工具清单清理失败，已跳过: {e}")
            return original2(self, payloads, extra_body, *args, **kwargs)

        setattr(patched_tools, _MARK, original2)
        setattr(ProviderOpenAIOfficial, _ATTR_TOOLS, patched_tools)
    elif cur2 is None:
        logger.warning(
            f"[gemini_null_guard] 当前 AstrBot 没有 {_ATTR_TOOLS}，工具清单清理不可用，其余功能正常"
        )


def _uninstall():
    for attr in (_ATTR, _ATTR_TOOLS):
        current = getattr(ProviderOpenAIOfficial, attr, None)
        if current is None:
            continue
        original = getattr(current, _MARK, None)
        if original is not None:
            setattr(ProviderOpenAIOfficial, attr, original)
        else:
            # 别的插件在我们之后又包了一层，这时不能直接还原，否则会把它的也拆掉
            logger.warning(f"[gemini_null_guard] {attr} 外层还有其他补丁，跳过卸载，重启后生效")


def _load_settings(config) -> None:
    if not config:
        return
    kws = config.get("model_keywords")
    if isinstance(kws, list):
        _SETTINGS["keywords"] = [str(k) for k in kws if str(k).strip()]
    _SETTINGS["match_all"] = bool(config.get("match_all_models", False))
    _SETTINGS["verbose"] = bool(config.get("verbose_log", False))
    _SETTINGS["clean_tools"] = bool(config.get("clean_tool_schema", True))


@register(
    "astrbot_plugin_gemini_null_guard",
    "xavier",
    "修复 Gemini 中转工具调用 400：包好工具结果/历史参数里的数组和 null，清理工具清单里 Gemini 不认的字段",
    "0.4.0",
)
class GeminiNullGuard(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        _load_settings(config)
        _install()
        target = "所有模型" if _SETTINGS["match_all"] else f"关键词 {_SETTINGS['keywords']}"
        logger.info(f"[gemini_null_guard] 已启用，生效范围：{target}")

    async def terminate(self):
        _uninstall()
