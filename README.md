# astrbot_plugin_gemini_null_guard

修复部分 Gemini 中转（OpenAI 兼容接口）在工具调用时报 400 的问题。

## 症状

用 Gemini 模型调用工具后，开始报错，而且之后每次请求都失败：

```
Invalid value at 'contents[N].parts[M].function_response...'
Invalid value at 'contents[N].parts[M].function_call.args...'
```

还有一种是工具定义本身带了 Gemini 不认的字段，报：

```
Invalid JSON payload received. Unknown name "additionalProperties" ...
Invalid JSON payload received. Unknown name "default" ...
```

第一种的原因是有的中转把 OpenAI 格式转成 Gemini 原生格式时，会把数组里的元素变成 null，Gemini 不接受。
坏掉的那条工具记录留在上下文里，所以后面每一轮都会带着它出错。

## 做法

在 AstrBot 组装好请求、发出去之前，对命中的模型：

- 工具结果里有数组或 null：把原文整段包成 `{"result": "原文"}` 字符串
- 历史里已执行过的工具调用，参数里有数组或 null：包成 `{"_raw_args": "原文"}` 字符串

- 发给模型的工具清单：删掉 `default`、`additionalProperties`、`$schema`、`examples`、非字符串类型的 `enum`，默认值、可选值这类信息挪进 `description`；`array` 缺 `items` 时补上 `{"type": "string"}`

工具结果和历史参数内容一个字都不丢，模型照样读得到。只改发出去的请求，不改数据库里存的对话记录，停用插件就完全恢复原样。

## 配置

| 项 | 默认 | 说明 |
|---|---|---|
| 生效关键词 | `gemini` | 模型名或 base_url 里包含任意一个就生效。中转给模型起了别名时，把别名或中转域名加进来 |
| 对所有模型生效 | 关 | 打开后忽略关键词 |
| 清理工具清单 | 开 | 处理上面说的第二种报错 |
| 详细日志 | 关 | 每次命中都写日志，用来确认插件在工作 |

## 怎么确认它在工作

控制台日志里搜 `gemini_null_guard`：

- `已启用，生效范围：...`：插件装上了
- `wrapped=N`：这次请求处理了 N 条工具记录
- `schema_fixed=N`：这次请求的工具清单改了 N 处

## 已知限制

- 清理工具清单只改发出去的那份副本，工具本身的定义、执行时收到的参数都不受影响。删掉的 `default` 由工具自己的代码兜底，跟 Gemini 原生接口的处理方式一致。
- 模型自己写错参数（类型不对、漏了必填项）不在处理范围内，那是新的调用，改了反而可能执行错。

- 只处理走 AstrBot OpenAI 兼容接口（`ProviderOpenAIOfficial` 及其子类）的请求，Gemini 原生接口不经过这里，也不需要。
- 模型回看历史时会看到 `_raw_args` 这种格式，少数情况下可能模仿着在新调用里也这么写。遇到工具调用参数异常，可以先往这上面想。
- 插件是给 AstrBot 内部方法打补丁实现的，AstrBot 大版本更新改了这个方法名时需要跟着改。
