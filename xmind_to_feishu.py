#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
成功日记 XMind -> 飞书自动同步器。

常用命令：
  python xmind_to_feishu.py setup --xmind "C:\\path\\成功日记.xmind"
  python xmind_to_feishu.py install-task --run-now
  python xmind_to_feishu.py doctor --fix
  python xmind_to_feishu.py sync --dry-run
  python xmind_to_feishu.py export -o 成功日记.xml
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any
import zipfile


APP_NAME = "xmind_to_feishu"
DEFAULT_TITLE = "成功日记"
DEFAULT_PARENT_POSITION = "my_library"
DEFAULT_POLL_SECONDS = 2.0
DEFAULT_DEBOUNCE_SECONDS = 4.0
SCRIPT_DIR = Path(__file__).resolve().parent
SYNC_DIR = SCRIPT_DIR / ".sync"
CONFIG_PATH = SYNC_DIR / "xmind_to_feishu_config.json"
LATEST_XML_PATH = SYNC_DIR / "成功日记.latest.xml"
BACKUP_DIR = SYNC_DIR / "backups"
LOG_PATH = SYNC_DIR / "xmind_to_feishu.log"
SUPERVISE_LOCK_PATH = SYNC_DIR / "xmind_to_feishu.supervise.lock"
DEFAULT_TASK_NAME = "SuccessDiaryXMindFeishuSync"
STARTUP_LAUNCHER_NAME = "SuccessDiaryXMindFeishuSync.vbs"
RUN_REGISTRY_VALUE_NAME = DEFAULT_TASK_NAME
LARK_MAX_ATTEMPTS = 3
LARK_RETRY_SECONDS = 3
XML_BACKUP_RETENTION = 7
MIN_LARK_CLI_VERSION = "1.0.47"
MAX_STATUS_MESSAGE_LENGTH = 1200


class SyncError(Exception):
    """User-facing sync failure."""


class SingleInstanceLock:
    """Keep one background supervisor alive per workspace."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle: Any = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self.handle.seek(0)
                if not self.handle.read(1):
                    self.handle.write(b"\0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            return False

        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()).encode("ascii"))
        self.handle.flush()
        return True

    def release(self) -> None:
        if not self.handle:
            return
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def setup_logging(verbose: bool = False) -> logging.Logger:
    SYNC_DIR.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(APP_NAME)
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    console = logging.StreamHandler()
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
    logger.addHandler(console)

    file_handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(file_handler)

    return logger


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        raise SyncError(f"未找到配置文件：{path}。请先运行 setup。")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SyncError(f"配置文件 JSON 损坏：{path} ({exc})") from exc


def save_config(config: dict[str, Any], path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def format_time(timestamp: Any) -> str:
    if timestamp in (None, ""):
        return "无"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(timestamp)))
    except (TypeError, ValueError, OSError):
        return str(timestamp)


def trim_status_message(message: str) -> str:
    message = message.strip()
    if len(message) <= MAX_STATUS_MESSAGE_LENGTH:
        return message
    return message[:MAX_STATUS_MESSAGE_LENGTH] + "...(已截断)"


def record_error_state(config: dict[str, Any], message: str, *, increment_failures: bool = True, path: Path = CONFIG_PATH) -> None:
    config["last_error_at"] = time.time()
    config["last_error_message"] = trim_status_message(message)
    if increment_failures:
        try:
            failures = int(config.get("consecutive_failures") or 0)
        except (TypeError, ValueError):
            failures = 0
        config["consecutive_failures"] = failures + 1
    save_config(config, path)


def record_sync_success(config: dict[str, Any], xmind_path: str | Path, stats: dict[str, Any], *, path: Path = CONFIG_PATH) -> None:
    config["last_synced_at"] = time.time()
    config["last_success_stats"] = stats
    config["last_error_at"] = None
    config["last_error_message"] = None
    config["consecutive_failures"] = 0
    try:
        config["last_synced_mtime"] = Path(xmind_path).stat().st_mtime
    except OSError:
        pass
    save_config(config, path)


def normalize_doc_token_or_url(value: str | None) -> str | None:
    if not value:
        return None
    value = value.strip().strip("\"'")
    if not value:
        return None
    match = re.search(r"/docx/([^/?#]+)", value)
    if match:
        return match.group(1).strip("\"'")
    return value


def require_config_value(config: dict[str, Any], key: str) -> Any:
    value = config.get(key)
    if value in (None, ""):
        raise SyncError(f"配置缺少 {key}，请重新运行 setup。")
    return value


def write_setup_config(args: argparse.Namespace, logger: logging.Logger) -> None:
    xmind_path = Path(args.xmind).expanduser()
    if not xmind_path.exists():
        raise SyncError(f"XMind 文件不存在：{xmind_path}")
    if xmind_path.suffix.lower() != ".xmind":
        raise SyncError(f"文件扩展名不是 .xmind：{xmind_path}")

    config = {
        "xmind_path": str(xmind_path.resolve()),
        "doc": normalize_doc_token_or_url(args.doc),
        "title": args.title or DEFAULT_TITLE,
        "parent_position": args.parent_position or DEFAULT_PARENT_POSITION,
        "parent_token": args.parent_token,
        "poll_seconds": float(args.poll_seconds),
        "debounce_seconds": float(args.debounce_seconds),
        "as_identity": args.as_identity,
        "last_synced_mtime": None,
        "last_doc_url": None,
        "last_success_stats": None,
        "last_error_at": None,
        "last_error_message": None,
        "consecutive_failures": 0,
        "last_lark_cli_version": None,
        "autostart_repaired_at": None,
    }
    save_config(config)
    logger.info("配置已写入：%s", CONFIG_PATH)
    logger.info("XMind：%s", config["xmind_path"])
    if config["doc"]:
        logger.info("已绑定飞书文档：%s", config["doc"])
    else:
        logger.info("尚未绑定飞书文档；首次 sync/watch 会自动创建。")


def escape_xml_text(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\n", "<br/>")
    )


def topic_title(topic: dict[str, Any], default: str) -> str:
    title = str(topic.get("title") or "").strip()
    return title or default


def topic_label_xml(topic: dict[str, Any], default: str) -> str:
    title = escape_xml_text(topic_title(topic, default))
    if topic.get("is_summary"):
        return f"<b>概括：</b>{title}"
    if topic.get("is_detached"):
        return f"<b>自由主题：</b>{title}"
    return title


class XMindParser:
    def __init__(self, xmind_path: str | Path) -> None:
        self.xmind_path = Path(xmind_path)
        self.root_topic: dict[str, Any] | None = None
        self.total_nodes = 0
        self.max_depth = 0
        self.detached_nodes = 0
        self.summary_nodes = 0
        self._seen_ids: set[str] = set()

    def parse(self) -> dict[str, Any]:
        if not self.xmind_path.exists():
            raise SyncError(f"XMind 文件不存在：{self.xmind_path}")
        if self.xmind_path.suffix.lower() != ".xmind":
            raise SyncError(f"文件扩展名不是 .xmind：{self.xmind_path}")

        try:
            with zipfile.ZipFile(self.xmind_path, "r") as zf:
                names = set(zf.namelist())
                if "content.json" not in names:
                    if "content.xml" in names:
                        raise SyncError("检测到旧版 XMind content.xml，请先在 XMind 中另存为新版 .xmind。")
                    raise SyncError("无法识别的 XMind 文件：缺少 content.json。")
                with zf.open("content.json") as handle:
                    content = json.load(handle)
        except zipfile.BadZipFile as exc:
            raise SyncError(f"文件损坏或不是有效 XMind/ZIP：{self.xmind_path}") from exc
        except json.JSONDecodeError as exc:
            raise SyncError(f"content.json 解析失败：{exc}") from exc

        if not isinstance(content, list) or not content:
            raise SyncError("content.json 内容为空或格式不正确。")

        root = None
        for sheet in content:
            if isinstance(sheet, dict) and isinstance(sheet.get("rootTopic"), dict):
                root = sheet["rootTopic"]
                break
        if root is None:
            raise SyncError("content.json 中没有 rootTopic。")

        extracted = self._extract_topic(root, level=0)
        if extracted is None:
            raise SyncError("根主题解析失败。")
        self.root_topic = extracted
        return extracted

    def _extract_topic(self, topic: dict[str, Any], level: int, marker: str | None = None) -> dict[str, Any] | None:
        topic_id = topic.get("id")
        if topic_id:
            topic_id = str(topic_id)
            if topic_id in self._seen_ids:
                return None
            self._seen_ids.add(topic_id)

        self.total_nodes += 1
        self.max_depth = max(self.max_depth, level + 1)
        data = {
            "id": topic_id,
            "title": str(topic.get("title") or ""),
            "children": [],
            "is_detached": marker == "detached",
            "is_summary": marker == "summary",
        }

        children = topic.get("children") or {}
        if not isinstance(children, dict):
            return data

        for child in children.get("attached") or []:
            if isinstance(child, dict):
                child_data = self._extract_topic(child, level + 1)
                if child_data:
                    data["children"].append(child_data)

        for child in children.get("detached") or []:
            if isinstance(child, dict):
                self.detached_nodes += 1
                child_data = self._extract_topic(child, level + 1, "detached")
                if child_data:
                    data["children"].append(child_data)

        for child in children.get("summary") or []:
            if isinstance(child, dict):
                self.summary_nodes += 1
                child_data = self._extract_topic(child, level + 1, "summary")
                if child_data:
                    data["children"].append(child_data)

        return data

    def stats(self) -> dict[str, Any]:
        return {
            "total_nodes": self.total_nodes,
            "max_depth": self.max_depth,
            "detached_nodes": self.detached_nodes,
            "summary_nodes": self.summary_nodes,
        }


class SuccessDiaryXmlConverter:
    def __init__(self, default_title: str = DEFAULT_TITLE) -> None:
        self.default_title = default_title

    def to_xml(self, root: dict[str, Any]) -> str:
        title = escape_xml_text(topic_title(root, self.default_title))
        parts = [f"<title>{title}</title>"]
        for date_topic in root.get("children", []):
            self._date_to_xml(date_topic, parts)
        return "\n".join(parts) + "\n"

    def _date_to_xml(self, topic: dict[str, Any], parts: list[str]) -> None:
        title = topic_label_xml(topic, "未命名日期")
        parts.append(f"<h1>{title}</h1>")
        children = topic.get("children", [])
        regular_children = [child for child in children if not child.get("is_summary")]
        summary_children = [child for child in children if child.get("is_summary")]
        if regular_children:
            parts.append(self._children_to_list(regular_children, ordered=True, depth=1))
        for summary in summary_children:
            parts.append(self._summary_to_xml(summary))

    def _summary_to_xml(self, topic: dict[str, Any]) -> str:
        label = topic_label_xml(topic, "未命名概括")
        children = topic.get("children", [])
        if not children:
            return f"<blockquote><p>{label}</p></blockquote>"
        lines = [f"<blockquote><p>{label}</p>"]
        lines.append(self._children_to_list(children, ordered=False, depth=2))
        lines.append("</blockquote>")
        return "\n".join(lines)

    def _children_to_list(self, children: list[dict[str, Any]], ordered: bool, depth: int) -> str:
        tag = "ol" if ordered else "ul"
        lines = [f"<{tag}>"]
        for child in children:
            lines.extend(self._topic_to_li(child, depth))
        lines.append(f"</{tag}>")
        return "\n".join(lines)

    def _topic_to_li(self, topic: dict[str, Any], depth: int) -> list[str]:
        seq_attr = ' seq="auto"' if depth == 1 else ""
        label = topic_label_xml(topic, "未命名事项")
        children = topic.get("children", [])
        if not children:
            return [f"<li{seq_attr}>{label}</li>"]

        lines = [f"<li{seq_attr}>{label}"]
        lines.append(self._children_to_list(children, ordered=False, depth=depth + 1))
        lines.append("</li>")
        return lines


def render_xmind_to_xml(xmind_path: str | Path, title: str = DEFAULT_TITLE) -> tuple[str, dict[str, Any]]:
    parser = XMindParser(xmind_path)
    root = parser.parse()
    xml = SuccessDiaryXmlConverter(title).to_xml(root)
    return xml, parser.stats()


def write_latest_xml(xml: str) -> None:
    SYNC_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_XML_PATH.write_text(xml, encoding="utf-8")


def write_timestamped_xml_backup(
    xml: str,
    *,
    backup_dir: Path = BACKUP_DIR,
    retain: int = XML_BACKUP_RETENTION,
    timestamp: str | None = None,
) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = timestamp or time.strftime("%Y%m%d-%H%M%S")
    backup_path = backup_dir / f"成功日记.{stamp}.xml"
    index = 1
    while backup_path.exists():
        backup_path = backup_dir / f"成功日记.{stamp}-{index}.xml"
        index += 1
    backup_path.write_text(xml, encoding="utf-8")

    backups = sorted(backup_dir.glob("成功日记.*.xml"), key=lambda path: (path.stat().st_mtime, path.name))
    for old_backup in backups[:-retain]:
        try:
            old_backup.unlink()
        except OSError:
            pass
    return backup_path


def count_xml_backups(backup_dir: Path = BACKUP_DIR) -> int:
    if not backup_dir.exists():
        return 0
    return len(list(backup_dir.glob("成功日记.*.xml")))


def classify_lark_error(result: subprocess.CompletedProcess[str]) -> str:
    output = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()
    lower = output.lower()
    if "permission" in lower or "scope" in lower or "forbidden" in lower:
        return "飞书权限不足，请根据 lark-cli 提示补充授权或应用 scope。\n" + output
    if "auth" in lower or "login" in lower or "unauthorized" in lower:
        return "飞书未登录或授权已失效，请先运行 lark-cli auth login。\n" + output
    if "not found" in lower or "invalid doc" in lower or "doc" in lower and "token" in lower:
        return "飞书文档 token/url 可能失效，请重新 setup --doc。\n" + output
    if "network" in lower or "timeout" in lower:
        return "网络或飞书接口请求失败，请稍后重试。\n" + output
    return output or f"lark-cli 退出码：{result.returncode} ({hex(result.returncode)})"


def resolve_lark_cli() -> str:
    candidates = ["lark-cli.cmd", "lark-cli.exe", "lark-cli"] if os.name == "nt" else ["lark-cli"]
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise SyncError("找不到 lark-cli，请先安装并加入 PATH。")


def parse_version_tuple(version: str) -> tuple[int, ...]:
    match = re.search(r"(\d+(?:\.\d+)+)", version)
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def version_less_than(current: str, minimum: str) -> bool:
    current_parts = parse_version_tuple(current)
    minimum_parts = parse_version_tuple(minimum)
    if not current_parts or not minimum_parts:
        return False
    length = max(len(current_parts), len(minimum_parts))
    current_parts = current_parts + (0,) * (length - len(current_parts))
    minimum_parts = minimum_parts + (0,) * (length - len(minimum_parts))
    return current_parts < minimum_parts


def get_lark_cli_version() -> str:
    result = subprocess.run(
        [resolve_lark_cli(), "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        raise SyncError("无法读取 lark-cli 版本：\n" + output)
    match = re.search(r"(\d+(?:\.\d+)+)", output)
    return match.group(1) if match else output


def ensure_lark_cli_current(config: dict[str, Any], logger: logging.Logger, *, fix: bool) -> bool:
    version = get_lark_cli_version()
    config["last_lark_cli_version"] = version
    save_config(config)
    if not version_less_than(version, MIN_LARK_CLI_VERSION):
        logger.info("lark-cli 版本正常：%s", version)
        return True

    logger.warning("lark-cli 版本偏旧：%s，目标最低版本：%s", version, MIN_LARK_CLI_VERSION)
    if not fix:
        return False

    logger.info("正在自动执行 lark-cli update...")
    result = subprocess.run(
        [resolve_lark_cli(), "update"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    config["last_lark_cli_update_attempt_at"] = time.time()
    if result.returncode != 0:
        message = "lark-cli update 失败：\n" + (result.stdout or result.stderr or "").strip()
        record_error_state(config, message, increment_failures=False)
        logger.warning("%s", trim_status_message(message))
        return False

    try:
        version = get_lark_cli_version()
    except SyncError:
        version = "unknown"
    config["last_lark_cli_version"] = version
    config["last_lark_cli_updated_at"] = time.time()
    save_config(config)
    logger.info("lark-cli 更新检查完成，当前版本：%s", version)
    return True


def resolve_task_python() -> str:
    if os.name == "nt":
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if pythonw.exists():
            return str(pythonw)
    return sys.executable


def build_watch_task_command() -> str:
    return subprocess.list2cmdline(
        [
            resolve_task_python(),
            str(SCRIPT_DIR / "xmind_to_feishu.py"),
            "supervise",
        ]
    )


def startup_launcher_path() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise SyncError("无法定位 Windows 启动文件夹：缺少 APPDATA 环境变量。")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_LAUNCHER_NAME


def build_startup_launcher_text() -> str:
    command = build_watch_task_command().replace('"', '""')
    return (
        'Set WshShell = CreateObject("WScript.Shell")\n'
        f'WshShell.CurrentDirectory = "{str(SCRIPT_DIR).replace(chr(34), chr(34) * 2)}"\n'
        f'WshShell.Run "{command}", 0, False\n'
    )


def write_startup_launcher() -> Path:
    launcher = startup_launcher_path()
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(build_startup_launcher_text(), encoding="utf-16")
    return launcher


def is_startup_launcher_current() -> bool:
    try:
        launcher = startup_launcher_path()
    except SyncError:
        return False
    if not launcher.exists():
        return False
    expected = build_startup_launcher_text().strip()
    for encoding in ("utf-16", "utf-8-sig", "mbcs"):
        try:
            return launcher.read_text(encoding=encoding).strip() == expected
        except (UnicodeError, OSError, LookupError):
            continue
    return False


def write_run_registry_entry() -> None:
    if os.name != "nt":
        return
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_REGISTRY_VALUE_NAME, 0, winreg.REG_SZ, build_watch_task_command())


def delete_run_registry_entry() -> None:
    if os.name != "nt":
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_REGISTRY_VALUE_NAME)
    except FileNotFoundError:
        return


def read_run_registry_entry() -> str | None:
    if os.name != "nt":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            value, _value_type = winreg.QueryValueEx(key, RUN_REGISTRY_VALUE_NAME)
    except FileNotFoundError:
        return None
    return str(value)


def is_run_registry_current() -> bool:
    return read_run_registry_entry() == build_watch_task_command()


def ensure_autostart_config(config: dict[str, Any], logger: logging.Logger, *, fix: bool) -> dict[str, bool]:
    status = {
        "startup_launcher_current": is_startup_launcher_current(),
        "run_registry_current": is_run_registry_current(),
    }
    if fix and (not status["startup_launcher_current"] or not status["run_registry_current"]):
        launcher = write_startup_launcher()
        write_run_registry_entry()
        config["autostart_repaired_at"] = time.time()
        save_config(config)
        status["startup_launcher_current"] = is_startup_launcher_current()
        status["run_registry_current"] = is_run_registry_current()
        logger.info("已自动修复自启动：%s", launcher)
    return status


def start_background_watch() -> None:
    command = [
        resolve_task_python(),
        str(SCRIPT_DIR / "xmind_to_feishu.py"),
        "supervise",
    ]
    kwargs: dict[str, Any] = {"cwd": SCRIPT_DIR}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **kwargs)


def list_running_sync_processes() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        return []
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { ($_.Name -like 'python*') -and ($_.CommandLine -like '*xmind_to_feishu.py*') } | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    result = subprocess.run(
        [powershell, "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        processes = [data]
    elif isinstance(data, list):
        processes = [item for item in data if isinstance(item, dict)]
    else:
        processes = []
    current_pid = os.getpid()
    running = []
    for process in processes:
        command_line = str(process.get("CommandLine") or "")
        if int(process.get("ProcessId") or 0) == current_pid:
            continue
        if " supervise" not in command_line and " watch" not in command_line:
            continue
        running.append(process)
    return running


def run_schtasks(args: list[str]) -> subprocess.CompletedProcess[str]:
    if os.name != "nt":
        raise SyncError("计划任务安装只支持 Windows。")
    schtasks = shutil.which("schtasks.exe") or shutil.which("schtasks")
    if not schtasks:
        raise SyncError("找不到 schtasks.exe，无法安装 Windows 计划任务。")
    return subprocess.run([schtasks, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")


def extract_doc_from_create_output(stdout: str) -> tuple[str | None, str | None]:
    doc_token = None
    doc_url = None
    for text in reversed(stdout.splitlines()):
        text = text.strip()
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        document = ((data.get("data") or {}).get("document") or {})
        doc_url = document.get("url") or doc_url
        doc_token = document.get("document_id") or doc_token
        if doc_token or doc_url:
            break

    if not doc_token:
        match = re.search(r"/docx/([^/?#\s]+)", stdout)
        if match:
            doc_token = match.group(1)
    if not doc_url:
        match = re.search(r"https?://\S+/docx/[^\s\"']+", stdout)
        if match:
            doc_url = match.group(0)
    if doc_url:
        doc_token = normalize_doc_token_or_url(doc_url) or doc_token
    doc_token = normalize_doc_token_or_url(doc_token)
    return doc_token, doc_url


class LarkDocSyncer:
    def __init__(self, config: dict[str, Any], logger: logging.Logger) -> None:
        self.config = config
        self.logger = logger

    def sync(self, dry_run: bool = False) -> None:
        xmind_path = require_config_value(self.config, "xmind_path")
        try:
            title = self.config.get("title") or DEFAULT_TITLE
            xml, stats = render_xmind_to_xml(xmind_path, title)
            write_latest_xml(xml)
            if not dry_run:
                backup_path = write_timestamped_xml_backup(xml)
                self.logger.info("历史 XML 备份：%s", backup_path)

            self.logger.info(
                "解析完成：%s 个节点，最大 %s 层，自由主题 %s，概要 %s",
                stats["total_nodes"],
                stats["max_depth"],
                stats["detached_nodes"],
                stats["summary_nodes"],
            )
            self.logger.info("XML 备份：%s", LATEST_XML_PATH)

            doc = normalize_doc_token_or_url(self.config.get("doc"))
            if doc:
                self._update_doc(doc, xml, dry_run=dry_run)
            else:
                doc_token, doc_url = self._create_doc(xml, dry_run=dry_run)
                if not dry_run and doc_token:
                    self.config["doc"] = doc_token
                    self.config["last_doc_url"] = doc_url
                    save_config(self.config)
                    self.logger.info("首次创建完成，已保存飞书文档 token：%s", doc_token)

            if not dry_run:
                record_sync_success(self.config, xmind_path, stats)
        except SyncError as exc:
            if not dry_run:
                record_error_state(self.config, str(exc))
            raise

    def _base_cmd(self, action: str) -> list[str]:
        cmd = ["lark-cli", "docs", action, "--api-version", "v2", "--doc-format", "xml"]
        as_identity = self.config.get("as_identity")
        if as_identity:
            cmd.extend(["--as", as_identity])
        return cmd

    def _run_lark(self, cmd: list[str], xml: str, dry_run: bool) -> subprocess.CompletedProcess[str]:
        if dry_run:
            cmd = [*cmd, "--dry-run"]

        SYNC_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", suffix=".xml", prefix="_lark_content_", dir=SYNC_DIR, encoding="utf-8", delete=False) as tmp:
            tmp.write(xml)
            tmp_path = Path(tmp.name)
        try:
            cmd = [*cmd]
            cmd[0] = resolve_lark_cli()
            content_path = os.path.relpath(tmp_path, SCRIPT_DIR)
            cmd = [*cmd, "--content", f"@{content_path}"]
            self.logger.debug("执行：%s", " ".join(cmd))
            result: subprocess.CompletedProcess[str] | None = None
            for attempt in range(1, LARK_MAX_ATTEMPTS + 1):
                result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", cwd=SCRIPT_DIR)
                if result.returncode == 0:
                    return result
                if attempt < LARK_MAX_ATTEMPTS and not (result.stdout or result.stderr):
                    self.logger.warning(
                        "lark-cli 异常退出 %s (%s)，%s 秒后重试第 %s 次。",
                        result.returncode,
                        hex(result.returncode),
                        LARK_RETRY_SECONDS,
                        attempt + 1,
                    )
                    time.sleep(LARK_RETRY_SECONDS)
                    continue
                return result
            assert result is not None
            return result
        finally:
            try:
                tmp_path.unlink()
            except OSError:
                pass

    def _create_doc(self, xml: str, dry_run: bool) -> tuple[str | None, str | None]:
        cmd = self._base_cmd("+create")
        parent_token = self.config.get("parent_token")
        if parent_token:
            cmd.extend(["--parent-token", str(parent_token)])
        else:
            parent_position = self.config.get("parent_position") or DEFAULT_PARENT_POSITION
            cmd.extend(["--parent-position", str(parent_position)])

        self.logger.info("飞书文档未绑定，准备自动创建。")
        result = self._run_lark(cmd, xml, dry_run)
        if result.returncode != 0:
            raise SyncError("创建飞书文档失败：\n" + classify_lark_error(result))
        if result.stdout.strip():
            self.logger.debug(result.stdout.strip())
        if dry_run:
            self.logger.info("dry-run 完成：未真实创建飞书文档。")
            return None, None
        doc_token, doc_url = extract_doc_from_create_output(result.stdout)
        if not doc_token:
            raise SyncError("飞书文档已创建，但未能从 lark-cli 输出中识别 document_id。请查看日志。")
        return doc_token, doc_url

    def _update_doc(self, doc: str, xml: str, dry_run: bool) -> None:
        cmd = self._base_cmd("+update")
        cmd.extend(["--doc", doc, "--command", "overwrite"])
        self.logger.info("准备覆盖同步飞书文档：%s", doc)
        result = self._run_lark(cmd, xml, dry_run)
        if result.returncode != 0:
            raise SyncError("覆盖飞书文档失败：\n" + classify_lark_error(result))
        if dry_run:
            self.logger.info("dry-run 完成：未真实覆盖飞书文档。")
        else:
            self.logger.info("飞书文档已更新。")


def run_export(args: argparse.Namespace, logger: logging.Logger) -> None:
    config = load_config()
    xmind_path = args.xmind or require_config_value(config, "xmind_path")
    title = args.title or config.get("title") or DEFAULT_TITLE
    xml, stats = render_xmind_to_xml(xmind_path, title)

    output_path = Path(args.output)
    if output_path.suffix.lower() != ".xml":
        raise SyncError("export 只输出 XML，请使用 .xml 扩展名。")
    output_path.write_text(xml, encoding="utf-8")
    logger.info("已导出 XML：%s", output_path)
    logger.info("节点：%s，最大层级：%s", stats["total_nodes"], stats["max_depth"])


def run_sync(args: argparse.Namespace, logger: logging.Logger) -> None:
    config = load_config()
    LarkDocSyncer(config, logger).sync(dry_run=args.dry_run)


def run_watch(args: argparse.Namespace, logger: logging.Logger) -> None:
    config = load_config()
    xmind_path = Path(require_config_value(config, "xmind_path"))
    poll_seconds = float(args.poll_seconds or config.get("poll_seconds") or DEFAULT_POLL_SECONDS)
    debounce_seconds = float(args.debounce_seconds or config.get("debounce_seconds") or DEFAULT_DEBOUNCE_SECONDS)

    logger.info("开始监听：%s", xmind_path)
    logger.info("保存后约 %.1f 秒自动同步，按 Ctrl+C 停止。", debounce_seconds)

    last_seen_mtime = None
    pending_mtime = None
    pending_since = None

    while True:
        try:
            if not xmind_path.exists():
                logger.error("XMind 文件不存在：%s", xmind_path)
                time.sleep(poll_seconds)
                continue

            current_mtime = xmind_path.stat().st_mtime
            if last_seen_mtime is None:
                last_seen_mtime = current_mtime
                if args.sync_on_start:
                    try:
                        LarkDocSyncer(config, logger).sync(dry_run=args.dry_run)
                    except SyncError as exc:
                        logger.error("%s", exc)
                time.sleep(poll_seconds)
                continue

            if current_mtime != last_seen_mtime:
                last_seen_mtime = current_mtime
                pending_mtime = current_mtime
                pending_since = time.time()
                logger.info("检测到 XMind 保存，等待文件稳定...")

            if pending_mtime is not None and pending_since is not None:
                if time.time() - pending_since >= debounce_seconds:
                    try:
                        LarkDocSyncer(config, logger).sync(dry_run=args.dry_run)
                    except SyncError as exc:
                        logger.error("%s", exc)
                    pending_mtime = None
                    pending_since = None

            time.sleep(poll_seconds)
        except KeyboardInterrupt:
            logger.info("监听已停止。")
            return
        except Exception as exc:
            logger.exception("监听循环异常：%s", exc)
            time.sleep(poll_seconds)


def run_supervise(args: argparse.Namespace, logger: logging.Logger) -> None:
    lock = SingleInstanceLock(SUPERVISE_LOCK_PATH)
    if not lock.acquire():
        logger.info("已有后台监督进程在运行，本次启动自动退出。锁文件：%s", SUPERVISE_LOCK_PATH)
        return

    logger.info("后台监督进程已启动。")
    try:
        config = load_config()
        ensure_autostart_config(config, logger, fix=True)
        ensure_lark_cli_current(config, logger, fix=True)
    except SyncError as exc:
        logger.error("后台自检未完全通过，但监听会继续尝试：%s", exc)

    try:
        while True:
            watch_args = argparse.Namespace(
                poll_seconds=args.poll_seconds,
                debounce_seconds=args.debounce_seconds,
                sync_on_start=True,
                dry_run=False,
            )
            try:
                run_watch(watch_args, logger)
            except Exception as exc:
                logger.exception("监听意外退出，10 秒后自动重启：%s", exc)
            time.sleep(10)
    finally:
        lock.release()


def run_install_task(args: argparse.Namespace, logger: logging.Logger) -> None:
    config = load_config()
    task_name = args.task_name or DEFAULT_TASK_NAME
    task_command = build_watch_task_command()
    result = run_schtasks(["/Create", "/TN", task_name, "/SC", "ONLOGON", "/TR", task_command, "/F"])
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or "").strip()
        logger.warning("安装 Windows 计划任务失败，改用当前用户自启动双保险方案：%s", reason)
    else:
        logger.info("已安装后台自动同步任务：%s", task_name)
        logger.info("任务命令：%s", task_command)
    ensure_autostart_config(config, logger, fix=True)

    if args.run_now:
        if result.returncode == 0:
            run_result = run_schtasks(["/Run", "/TN", task_name])
            if run_result.returncode != 0:
                logger.warning("计划任务已安装，但立即启动失败，改用直接后台启动：%s", (run_result.stderr or run_result.stdout).strip())
                start_background_watch()
            else:
                logger.info("后台任务已立即启动。以后登录 Windows 后会自动启动。")
        else:
            start_background_watch()
            logger.info("后台监听已立即启动。以后登录 Windows 后会自动启动。")


def run_uninstall_task(args: argparse.Namespace, logger: logging.Logger) -> None:
    task_name = args.task_name or DEFAULT_TASK_NAME
    result = run_schtasks(["/Delete", "/TN", task_name, "/F"])
    if result.returncode == 0:
        logger.info("已删除 Windows 计划任务：%s", task_name)
    else:
        logger.info("未删除 Windows 计划任务：%s", (result.stderr or result.stdout).strip())

    launcher = startup_launcher_path()
    if launcher.exists():
        launcher.unlink()
        logger.info("已删除当前用户启动项：%s", launcher)
    delete_run_registry_entry()
    logger.info("已删除注册表自启动项：%s", RUN_REGISTRY_VALUE_NAME)


def run_start_task(args: argparse.Namespace, logger: logging.Logger) -> None:
    task_name = args.task_name or DEFAULT_TASK_NAME
    result = run_schtasks(["/Run", "/TN", task_name])
    if result.returncode != 0:
        logger.warning("启动 Windows 计划任务失败，改用直接后台启动：%s", (result.stderr or result.stdout).strip())
        start_background_watch()
        logger.info("后台监听已启动。")
    else:
        logger.info("后台任务已启动：%s", task_name)


def run_doctor(args: argparse.Namespace, logger: logging.Logger) -> None:
    config = load_config()
    healthy = True
    logger.info("开始自动同步器体检%s。", "并修复" if args.fix else "")

    try:
        xmind_path = require_config_value(config, "xmind_path")
        _xml, stats = render_xmind_to_xml(xmind_path, config.get("title") or DEFAULT_TITLE)
        logger.info(
            "XMind 正常：%s；%s 个节点，最大 %s 层，概要 %s",
            xmind_path,
            stats["total_nodes"],
            stats["max_depth"],
            stats["summary_nodes"],
        )
    except SyncError as exc:
        healthy = False
        logger.error("XMind 检查失败：%s", exc)
        if args.fix:
            record_error_state(config, f"doctor XMind 检查失败：{exc}", increment_failures=False)

    try:
        autostart = ensure_autostart_config(config, logger, fix=args.fix)
        logger.info(
            "自启动状态：启动文件夹 %s，注册表 Run %s",
            "正常" if autostart["startup_launcher_current"] else "异常",
            "正常" if autostart["run_registry_current"] else "异常",
        )
        healthy = healthy and autostart["startup_launcher_current"] and autostart["run_registry_current"]
    except SyncError as exc:
        healthy = False
        logger.error("自启动检查失败：%s", exc)

    try:
        lark_ok = ensure_lark_cli_current(config, logger, fix=args.fix)
        healthy = healthy and lark_ok
    except SyncError as exc:
        healthy = False
        logger.error("lark-cli 检查失败：%s", exc)
        if args.fix:
            record_error_state(config, f"doctor lark-cli 检查失败：{exc}", increment_failures=False)

    logger.info("历史 XML 备份数量：%s/%s", count_xml_backups(), XML_BACKUP_RETENTION)
    if config.get("last_error_message"):
        logger.info("最近一次错误：%s，%s", format_time(config.get("last_error_at")), config.get("last_error_message"))
    logger.info("连续失败次数：%s", config.get("consecutive_failures") or 0)

    if args.fix:
        try:
            LarkDocSyncer(config, logger).sync(dry_run=True)
            logger.info("飞书 dry-run 验证通过。")
        except SyncError as exc:
            healthy = False
            logger.error("飞书 dry-run 验证失败：%s", exc)
            record_error_state(config, f"doctor 飞书 dry-run 验证失败：{exc}", increment_failures=False)

    processes = list_running_sync_processes()
    if not processes and args.fix:
        start_background_watch()
        logger.info("未检测到后台同步进程，已自动启动。")
        time.sleep(3)
        processes = list_running_sync_processes()

    if processes:
        for process in processes:
            logger.info("后台同步进程运行中：PID %s，%s", process.get("ProcessId"), process.get("CommandLine"))
    else:
        healthy = False
        logger.warning("未检测到正在运行的后台同步进程。")

    if healthy:
        logger.info("doctor 完成：核心项目正常。")
    else:
        logger.warning("doctor 完成：仍有项目需要关注。")


def run_task_status(args: argparse.Namespace, logger: logging.Logger) -> None:
    task_name = args.task_name or DEFAULT_TASK_NAME
    result = run_schtasks(["/Query", "/TN", task_name, "/V", "/FO", "LIST"])
    if result.returncode == 0:
        logger.info("\n%s", result.stdout.strip())
    else:
        logger.info("未找到可查询的 Windows 计划任务：%s", (result.stderr or result.stdout).strip())

    launcher = startup_launcher_path()
    if launcher.exists():
        logger.info("当前用户启动项存在：%s", launcher)
    else:
        logger.info("当前用户启动项不存在：%s", launcher)

    run_entry = read_run_registry_entry()
    if run_entry:
        logger.info("注册表自启动项存在：%s", run_entry)
    else:
        logger.info("注册表自启动项不存在。")

    processes = list_running_sync_processes()
    if processes:
        for process in processes:
            logger.info("后台同步进程运行中：PID %s，%s", process.get("ProcessId"), process.get("CommandLine"))
    else:
        logger.info("未检测到正在运行的后台同步进程。")

    try:
        config = load_config()
    except SyncError:
        return
    logger.info("XMind 文件：%s", config.get("xmind_path"))
    xmind_path = Path(str(config.get("xmind_path") or ""))
    if xmind_path.exists():
        logger.info("当前 XMind 修改时间：%s", format_time(xmind_path.stat().st_mtime))
    else:
        logger.warning("当前 XMind 文件不存在。")
    logger.info("飞书文档：%s", config.get("doc") or "未绑定，首次同步会自动创建")
    last_synced_at = config.get("last_synced_at")
    if last_synced_at:
        logger.info("上次成功同步时间：%s", format_time(last_synced_at))
    last_mtime = config.get("last_synced_mtime")
    if last_mtime:
        logger.info("上次同步对应的 XMind 修改时间：%s", format_time(last_mtime))
        if xmind_path.exists() and xmind_path.stat().st_mtime > float(last_mtime) + 0.001:
            logger.warning("XMind 比上次同步记录更新，后台可能还在同步或刚才失败。")
    else:
        logger.info("还没有记录成功同步时间。")
    stats = config.get("last_success_stats") or {}
    if stats:
        logger.info(
            "最近成功解析：%s 个节点，最大 %s 层，自由主题 %s，概要 %s",
            stats.get("total_nodes", "?"),
            stats.get("max_depth", "?"),
            stats.get("detached_nodes", "?"),
            stats.get("summary_nodes", "?"),
        )
    logger.info("连续失败次数：%s", config.get("consecutive_failures") or 0)
    if config.get("last_error_message"):
        logger.info("最近一次错误：%s，%s", format_time(config.get("last_error_at")), config.get("last_error_message"))
    logger.info("lark-cli 最近记录版本：%s", config.get("last_lark_cli_version") or "未知")
    logger.info("最近自启动修复时间：%s", format_time(config.get("autostart_repaired_at")))
    logger.info("历史 XML 备份数量：%s/%s", count_xml_backups(), XML_BACKUP_RETENTION)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="成功日记 XMind 自动同步飞书",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            '  python xmind_to_feishu.py setup --xmind "C:\\path\\成功日记.xmind"\n'
            "  python xmind_to_feishu.py install-task --run-now\n"
            "  python xmind_to_feishu.py doctor --fix\n"
            "  python xmind_to_feishu.py sync --dry-run\n"
            "  python xmind_to_feishu.py export -o 成功日记.xml\n"
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出详细日志")
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup_parser = subparsers.add_parser("setup", help="初始化同步配置")
    setup_parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="输出详细日志")
    setup_parser.add_argument("--xmind", required=True, help="成功日记 .xmind 文件路径")
    setup_parser.add_argument("--doc", help="已有飞书文档 token 或 URL")
    setup_parser.add_argument("--title", default=DEFAULT_TITLE, help="飞书文档标题")
    setup_parser.add_argument("--parent-position", default=DEFAULT_PARENT_POSITION, help="首次创建的飞书位置")
    setup_parser.add_argument("--parent-token", help="首次创建的飞书父文件夹或 wiki 节点 token")
    setup_parser.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS, help="watch 轮询间隔")
    setup_parser.add_argument("--debounce-seconds", type=float, default=DEFAULT_DEBOUNCE_SECONDS, help="保存防抖秒数")
    setup_parser.add_argument("--as", dest="as_identity", choices=["user", "bot"], help="传给 lark-cli 的身份")
    setup_parser.set_defaults(func=write_setup_config)

    watch_parser = subparsers.add_parser("watch", help="常驻监听 XMind 保存并自动同步")
    watch_parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="输出详细日志")
    watch_parser.add_argument("--poll-seconds", type=float, help="临时覆盖轮询间隔")
    watch_parser.add_argument("--debounce-seconds", type=float, help="临时覆盖保存防抖秒数")
    watch_parser.add_argument("--sync-on-start", action="store_true", help="启动 watch 时先同步一次")
    watch_parser.add_argument("--dry-run", action="store_true", help="只预演 lark-cli 请求，不写入飞书")
    watch_parser.set_defaults(func=run_watch)

    supervise_parser = subparsers.add_parser("supervise", help="后台监督监听，意外退出后自动重启")
    supervise_parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="输出详细日志")
    supervise_parser.add_argument("--poll-seconds", type=float, help="临时覆盖轮询间隔")
    supervise_parser.add_argument("--debounce-seconds", type=float, help="临时覆盖保存防抖秒数")
    supervise_parser.set_defaults(func=run_supervise)

    sync_parser = subparsers.add_parser("sync", help="立即同步一次")
    sync_parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="输出详细日志")
    sync_parser.add_argument("--dry-run", action="store_true", help="只预演 lark-cli 请求，不写入飞书")
    sync_parser.set_defaults(func=run_sync)

    export_parser = subparsers.add_parser("export", help="只导出 XML，不调用飞书")
    export_parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="输出详细日志")
    export_parser.add_argument("-o", "--output", required=True, help="XML 输出路径")
    export_parser.add_argument("--xmind", help="临时指定 XMind 路径，不修改配置")
    export_parser.add_argument("--title", help="临时指定文档标题")
    export_parser.set_defaults(func=run_export)

    install_task_parser = subparsers.add_parser("install-task", help="安装 Windows 登录后自动后台同步")
    install_task_parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="输出详细日志")
    install_task_parser.add_argument("--task-name", default=DEFAULT_TASK_NAME, help="Windows 计划任务名称")
    install_task_parser.add_argument("--run-now", action="store_true", help="安装后立即后台启动")
    install_task_parser.set_defaults(func=run_install_task)

    uninstall_task_parser = subparsers.add_parser("uninstall-task", help="删除 Windows 后台同步任务")
    uninstall_task_parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="输出详细日志")
    uninstall_task_parser.add_argument("--task-name", default=DEFAULT_TASK_NAME, help="Windows 计划任务名称")
    uninstall_task_parser.set_defaults(func=run_uninstall_task)

    start_task_parser = subparsers.add_parser("start-task", help="立即启动 Windows 后台同步任务")
    start_task_parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="输出详细日志")
    start_task_parser.add_argument("--task-name", default=DEFAULT_TASK_NAME, help="Windows 计划任务名称")
    start_task_parser.set_defaults(func=run_start_task)

    doctor_parser = subparsers.add_parser("doctor", help="检查并可自动修复后台同步环境")
    doctor_parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="输出详细日志")
    doctor_parser.add_argument("--fix", action="store_true", help="自动修复自启动、后台进程和 lark-cli")
    doctor_parser.set_defaults(func=run_doctor)

    status_parser = subparsers.add_parser("task-status", help="查看 Windows 后台同步任务状态")
    status_parser.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="输出详细日志")
    status_parser.add_argument("--task-name", default=DEFAULT_TASK_NAME, help="Windows 计划任务名称")
    status_parser.set_defaults(func=run_task_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    logger = setup_logging(args.verbose)
    try:
        args.func(args, logger)
        return 0
    except SyncError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
