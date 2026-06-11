# 成功日记 XMind 自动同步飞书

把 `成功日记.xmind` 当作唯一写作源，后台监听 XMind 保存，并自动覆盖同步到同一个飞书文档。

这个工具适合这样的使用方式：你只在 XMind 写成功日记，飞书负责展示和分享。同步逻辑是“整份 XMind 重新生成飞书文档”，不是每次追加一份新内容。

## 功能

- XMind 保存后自动同步飞书
- 开机登录后自动后台运行
- 飞书文档使用覆盖同步，避免重复追加
- 支持 XMind 日期、事项、深层事项、概要、自由主题
- 最近 7 份 XML 本地备份，便于误操作后排查
- `doctor --fix` 一键检查并自动修复后台、自启动和 `lark-cli`
- 不上传 `.sync`、日志、备份、XMind 原文件等私人数据

## 环境要求

- Windows
- Python 3.10 或更高版本
- 已安装并能运行 `lark-cli`
- 已准备一个 XMind 文件，例如 `成功日记.xmind`
- 已准备一个飞书文档或知识库页面链接

先确认这两个命令能运行：

```powershell
python --version
lark-cli --version
```

## 快速开始

下载项目后，在项目目录打开 PowerShell。

绑定自己的 XMind 和飞书文档：

```powershell
python .\xmind_to_feishu.py setup --xmind "C:\path\成功日记.xmind" --doc "你的飞书文档链接"
```

自动修复环境并启动后台：

```powershell
python .\xmind_to_feishu.py doctor --fix
```

以后只需要打开 XMind 写内容并保存。程序会在后台检测保存动作，并自动同步到飞书。

## 常用命令

查看当前状态：

```powershell
python .\xmind_to_feishu.py task-status
```

立即同步一次：

```powershell
python .\xmind_to_feishu.py sync
```

只预演，不写入飞书：

```powershell
python .\xmind_to_feishu.py sync --dry-run
```

安装并立即启动后台：

```powershell
python .\xmind_to_feishu.py install-task --run-now
```

停止自动同步：

```powershell
python .\xmind_to_feishu.py uninstall-task
```

## 给普通用户的脚本

`scripts` 目录里有几个 Windows 批处理脚本：

- `setup-and-fix.bat`：首次配置并自动修复后台
- `status.bat`：查看状态
- `sync-now.bat`：立即同步
- `uninstall.bat`：关闭自动同步

普通用户可以双击这些脚本使用。

## 同步规则

XMind 是唯一源头，飞书是镜像展示。

- XMind 中保留的内容，会出现在飞书里
- XMind 中删除的内容，下次同步后也会从飞书消失
- 直接在飞书里手动修改的内容，下次 XMind 保存后会被覆盖

这样做可以避免每次保存都重复追加一份内容。

## 本地数据

程序运行后会在项目目录生成 `.sync` 文件夹，里面包含：

- 同步配置
- 同步日志
- 最近生成的 XML
- 最近 7 份 XML 备份

`.sync` 是私人运行数据，不应该提交到 GitHub。

## 开发测试

运行测试：

```powershell
python -m unittest discover
```

语法检查：

```powershell
python -m py_compile .\xmind_to_feishu.py .\test_xmind_to_feishu.py
```

## 安全提醒

不要把这些内容上传到公开仓库：

- `.sync/`
- 你的 `.xmind` 文件
- 飞书文档链接或 token
- 日志和 XML 备份
- 任何真实日记内容

仓库只应该包含工具代码、测试和说明文档。
