# 发布到 GitHub

本项目已经是一个干净的 Git 仓库。发布到 GitHub 时，不要上传外层工作区，也不要上传 `.sync`、`.xmind`、日志或 XML 备份。

## 1. 在 GitHub 创建空仓库

推荐仓库名：

```text
success-diary-xmind-feishu-sync
```

建议选择：

- Visibility: Public
- 不要勾选 Add a README
- 不要勾选 Add .gitignore
- 不要勾选 Choose a license

因为这些文件本地项目里已经有了。

## 2. 推送本地项目

把下面命令里的 `<你的GitHub用户名>` 换成你的用户名：

```powershell
cd "C:\Users\DELL\Documents\公考AI\success-diary-xmind-feishu-sync"
git remote add origin https://github.com/<你的GitHub用户名>/success-diary-xmind-feishu-sync.git
git push -u origin main
```

如果你创建仓库时用了别的名称，把 URL 中的仓库名也一起改掉。

## 3. 发布前确认

推送前建议检查：

```powershell
git status --short
python -m unittest discover
```

确认没有这些文件：

- `.sync/`
- `*.xmind`
- 真实飞书链接
- 日志文件
- XML 备份
