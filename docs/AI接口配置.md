# AI 接口配置

## 1. 这个版本为什么没有本地模型

源码版不再内置 llama.cpp、`.gguf` 模型或便携 Python。AI 句读改为标准 OpenAI 兼容 HTTP 客户端：

```text
浏览器（设置页）
  → 本机 FastAPI（/api/llm/config、/api/llm/test、/api/llm/models）
  → POST {base_url}/chat/completions
  → 你选择的云端或自建模型服务
```

应用只负责发请求，不负责安装、启动、下载或停止模型服务。指向 Ollama / vLLM / LM Studio 时，需要你自己先把这些服务跑起来。

## 2. 打开设置

- 首页右上角：**AI 设置**
- 打开一本书后：顶栏 **设置**

保存后可以立即点 **测试连接**；测试会发一次很小的 chat 请求。

## 3. 字段说明

### 接口地址（Base URL）

填到 `/v1` 为止：

```text
https://api.openai.com/v1
https://api.deepseek.com/v1
https://dashscope.aliyuncs.com/compatible-mode/v1
http://127.0.0.1:11434/v1
```

也可以直接粘贴完整地址：

```text
https://api.openai.com/v1/chat/completions
```

保存时会自动去掉 `/chat/completions` 或 `/models` 后缀，再拼接客户端自己的路径。

> 目前只支持 `POST /chat/completions` 这种 Chat Completions 接口；不调用 OpenAI Responses API。

### API Key

- 标准写法：`Authorization: Bearer <key>`
- 无鉴权的本地 OpenAI 兼容服务可以留空。
- 已保存后输入框留空表示**保持原密钥不变**，不会清除；需要清除时勾选 **清除已保存的密钥**。
- 密钥保存在 `app/data/settings.json`，接口读取时只返回掩码，不会把明文回传给浏览器。
- 这是单机工具，设置文件没有加密；不要把 `settings.json` 提交到 Git。

### 模型

- 点 **获取模型** 调 `GET /models`，把返回的 `data[].id` 填进候选列表。
- 接口没有实现 `/models`（404/405）时，直接手填模型 ID 即可。
- 模型名以供应商控制台为准，不要凭猜测填写。

### 思考强度（reasoning_effort）

按 OpenAI 标准字段发送，默认不发送：

| 选项 | 请求体 |
|---|---|
| 不发送（默认） | 不带该字段 |
| none | `"reasoning_effort": "none"` |
| minimal | `"reasoning_effort": "minimal"` |
| low | `"reasoning_effort": "low"` |
| medium | `"reasoning_effort": "medium"` |
| high | `"reasoning_effort": "high"` |

注意：

- 不是所有模型都接受这些值。模型报 400 且错误提到 `reasoning_effort` 时，改成“不发送”。
- 有的供应商不使用这个字段，而使用 `thinking` / `enable_thinking` 等私有参数；请写到 **额外参数 JSON**。
- 强度越高通常越慢、越贵，但古籍句读是“保字 + 加标点”的任务，不一定需要最强推理。

### 采样温度

- 留空：不发送 `temperature`，由服务端默认值决定。**推理模型优先留空。**
- `0`：最稳定，适合要求逐字保留的句读任务。
- 0–2 之间的小数：越大越发散。

### 请求超时

默认 600 秒。整本句读会按结构块多次请求，单块通常远小于超时；如果网络慢或模型很慢，可以调大。

### 额外参数 JSON

会合并到请求体中，适合供应商私有字段：

```json
{
  "thinking": {"type": "disabled"}
}
```

```json
{
  "enable_thinking": false
}
```

```json
{
  "top_p": 0.9,
  "frequency_penalty": 0
}
```

约束：

- 必须是 JSON 对象 `{...}`。
- 不能覆盖 `model`、`messages`、`stream`；这三项由客户端控制。
- 会和温度、思考强度、`max_tokens` 等字段合并；同名时客户端生成的字段优先。

## 4. 请求体长什么样

客户端实际发送类似：

```json
{
  "model": "deepseek-chat",
  "messages": [
    {"role": "system", "content": "你是古籍戏曲整理助手……"},
    {"role": "user", "content": "为下面这段古文加标点，只输出结果：……"}
  ],
  "stream": false,
  "max_tokens": 1234,
  "temperature": 0,
  "reasoning_effort": "medium",
  "stop": ["（", "(注", "\n注", "注："]
}
```

说明：

- `max_tokens` 按当前块长度动态计算，不是整本上限。
- 句读提示词要求逐字保留原文、只输出加标点的文本；返回后会做保字、字符过滤和回退校验。
- 如果模型把思考过程写进正文或返回 `content: null`，会被判定为无效输出；请换非思考模型或调整供应商参数。

## 5. 常见错误

### 401 / 403

密钥错误、过期、没有模型权限，或接口要求别的鉴权头。先确认控制台复制的 Key 和 Base URL 属于同一个服务。

### 404

Base URL 多了或少了路径。常见错误：

```text
https://api.deepseek.com          ← 少了 /v1
https://api.openai.com/v1/v1      ← 重复 /v1
```

保存时客户端会去掉 `/chat/completions`、`/models` 后缀，但不会猜 `/v1`，请按供应商文档填写。

### 400 参数不支持

按顺序处理：

1. 思考强度改“不发送”；
2. 温度留空；
3. 把供应商要求的思考开关写进额外参数；
4. 如果错误明确要求 `max_completion_tokens`，客户端会自动用该字段重试一次（仅限此类 400）。

### 连接失败 / timeout

- 检查地址、网络、代理和防火墙。
- 本地 Ollama 等要确认服务已启动并监听正确端口。
- 云端接口偶发 429/5xx 时客户端会自动重试两次；仍然失败请稍后再试。

### `/models` 返回 404

很多兼容服务不实现模型列表。直接手填控制台里的模型 ID 即可，不影响句读。

### 模型返回空正文

思考模型可能只返回了 `reasoning_content` 或 `content` 为空。换模型，或在额外参数里关闭思考。客户端不会把思考内容当作标点结果。

## 6. 密钥安全

- 密钥只存在本机 `app/data/settings.json`，不会写入书稿或导出文件。
- 设置读取接口只返回 `has_api_key` 和末四位提示，不返回明文。
- 浏览器与本地服务之间是 `127.0.0.1` HTTP，不会把密钥发到第三方；只有你配置的 AI 接口会收到请求和正文。
- 若怀疑泄露，在供应商控制台吊销并重新生成，然后在设置中替换。
