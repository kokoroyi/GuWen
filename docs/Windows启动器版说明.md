# GuWen Windows 启动器版

这个版本包含：

- `GuWen.exe`：Windows 托盘启动器；
- 完整应用源码和 `app/data` 中的查字/曲牌数据；
- `setup.bat`、`start.bat`、`requirements.txt`；
- **不包含** Python 运行时、PaddleOCR 模型、`.gguf` 大模型或本地 LLM 服务。

## 使用

1. 安装 Python **3.11 或 3.12**（安装时勾选 Add Python to PATH）。
2. 解压压缩包到任意目录（建议路径不含特殊符号）。
3. 双击 `setup.bat`，等待依赖安装完成。第一次会下载较多 Python 包。
4. 双击 `GuWen.exe`。托盘图标出现后会自动打开 <http://127.0.0.1:8770/>。
5. 在网页右上角「AI 设置」中填写 OpenAI 兼容接口、API Key、模型和思考强度。

## 托盘菜单

- 打开工作台
- 打开数据目录
- 打开日志
- 安装依赖（首次使用）
- 退出 GuWen：结束由启动器启动的服务进程

## 注意

- 首次 OCR 需要联网下载 PP-OCRv6 模型，之后可离线 OCR。
- AI 句读不是本地模型，正文会发送到你配置的接口。
- 如果 `GuWen.exe` 提示找不到 `.venv`，说明还没运行 `setup.bat`。
- 如果启动失败，右键托盘图标 →「打开日志」，查看 `logs/server.log`。
