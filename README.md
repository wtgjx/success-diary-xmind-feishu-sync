# 成功日记 XMind 自动同步飞书

沉浸式在 XMind 写内容，自动同步到飞书，让 AI 可以实时理解你的记录。

## 为什么要做这个

我特别喜欢 XMind 的画布和它的设计。它让人可以沉下心，在一个相对安静的画布里工作、记录、整理想法。

我也喜欢飞书。尤其是飞书开放了飞书 CLI，让文档可以被 AI Agent 完整读取和操作。这意味着，当你的飞书 CLI 已经接入任意 AI 编程工具，比如 Codex、Claude Code、Cursor、Trae 等，AI 就可以实时看到你的内容，并基于这些内容继续帮你分析、整理和行动。

但这两个工具各自好的地方，恰好也是对方没有的地方：

- XMind 很适合沉浸式写作和结构化思考，但不适合把内容持续、稳定地导出给 AI 看。
- 飞书很适合让 AI 读取和处理文档，但功能太多，写日记或沉浸工作时容易分散注意力。

所以我开发了这个脚本。

它的目标很简单：**你只在 XMind 里写内容，脚本自动把内容同步到飞书上。**

这样你可以继续待在 XMind 的画布里思考和记录，同时让飞书成为 AI 能读取的镜像文档。当你的 AI 工具接入了飞书 CLI，它就能实时知道你写了什么，而你不用频繁切换工具。

## 环境要求

- Windows 或 macOS
- Python 3.10 或更高版本
- 已安装并能运行飞书 CLI
- 已准备一个 XMind 文件，例如 `成功日记.xmind`
- 已准备一个飞书文档或知识库页面链接

飞书 CLI 可以通过 AI Agent 辅助安装。官方页面里也提供了类似下面的提示词：

![飞书 CLI 通过 AI Agent 安装](assets/feishu-cli-ai-agent.png)

你可以把这个提示词复制给 Codex、Claude Code、Cursor、Trae 等 AI 助手，让它帮你完成飞书 CLI 安装：

```text
帮我安装飞书 CLI: https://open.feishu.cn/document/no_class/mcp-archive/feishu-cli-installation-guide.md
```

安装完成后，确认下面命令能正常运行：

```powershell
python --version
lark-cli --version
```

## 快速开始

你能找到这个项目，说明你大概率认同这个理念：**让人留在适合思考的工具里，让 AI 去连接和搬运信息。**

所以我假设你已经在使用 Codex、Claude Code、Cursor、Trae 这类 AI 编程工具。最推荐的部署方式不是自己逐条敲命令，而是把下面这段话交给 AI，让它帮你完成部署。

### 让 AI 帮你部署

先把这个仓库下载到本地，或者让 AI 帮你 clone：

```text
请帮我部署这个项目：https://github.com/wtgjx/success-diary-xmind-feishu-sync

我的目标是：
1. 检查本机是否有 Python 3.10+。
2. 检查是否安装并能运行飞书 CLI，也就是 lark-cli。
3. 如果没有飞书 CLI，请按官方文档帮我安装。
4. 引导我提供 XMind 文件路径，例如 C:\path\成功日记.xmind。
5. 引导我提供飞书文档或知识库页面链接。
6. 运行 setup 绑定 XMind 和飞书文档。
7. 运行 doctor --fix 自动修复后台、自启动和飞书 CLI。
8. 最后运行 task-status，确认后台同步进程正在运行。

请不要上传我的 .sync 文件夹、XMind 文件、日志、XML 备份或飞书链接到 GitHub。
```

如果你想自己执行，核心命令是下面两条：

```powershell
python .\xmind_to_feishu.py setup --xmind "C:\path\成功日记.xmind" --doc "你的飞书文档链接"
python .\xmind_to_feishu.py doctor --fix
```

完成后，你只需要继续在 XMind 里写内容并保存。脚本会在后台检测保存动作，并把当前 XMind 的完整内容覆盖同步到飞书文档。

同步逻辑是：**XMind 是唯一源头，飞书是镜像展示。**

也就是说，XMind 里保留的内容会同步到飞书；XMind 里删除的内容，下次同步后也会从飞书消失。不要直接在飞书里手动改这份镜像文档，因为下次保存 XMind 时，飞书内容会被重新覆盖。

程序运行后会生成 `.sync` 文件夹，用来保存本地配置、日志和最近 7 份 XML 备份。这个文件夹是私人数据，不应该提交到 GitHub。
