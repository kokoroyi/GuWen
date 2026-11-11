# Windows 托盘启动器

`GuWen.exe` 是源码版的 Windows 托盘启动器，不包含 Python、OCR 模型或任何本地大模型运行时。

## 行为

- 双击启动本项目本地服务，并打开 `http://127.0.0.1:8770/`；
- 常驻系统托盘，右键菜单：
  - **打开工作台**
  - **打开数据目录**
  - **打开日志**
  - **安装依赖（首次使用）**：运行根目录的 `setup.bat`
  - **退出 GuWen**：结束由它启动的服务进程树
- 单实例：再次双击只打开已有页面；
- 已有服务在运行时，只打开浏览器，退出托盘不会结束别人的服务；
- 找不到 `.venv\Scripts\python.exe` 时会提示先运行 `setup.bat`；
- 不启动任何本地 LLM，AI 句读通过网页中配置的 OpenAI 兼容接口调用。

## 首次使用

1. 双击 `setup.bat` 安装 Python 依赖（会创建 `.venv`）。
2. 双击 `GuWen.exe`。
3. 在网页「AI 设置」中填写 OpenAI 兼容接口、模型和思考强度。

## 构建

需要 Windows 自带的 .NET Framework 4.x `csc.exe`：

```bat
build.bat
```

或手动：

```bat
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe ^
  /nologo /target:winexe /optimize+ ^
  /win32icon:..\app\static\guwen.ico ^
  /r:System.dll /r:System.Windows.Forms.dll /r:System.Drawing.dll ^
  /out:GuWen.exe TrayLauncher.cs
```

产物 `launcher\GuWen.exe` 直接放在项目根目录使用。
