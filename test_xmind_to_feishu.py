import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from xmind_to_feishu import (
    SingleInstanceLock,
    SyncError,
    build_startup_launcher_text,
    build_watch_task_command,
    record_error_state,
    record_sync_success,
    render_xmind_to_xml,
    version_less_than,
    write_timestamped_xml_backup,
    XMindParser,
)


def make_xmind(path: Path, content):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("content.json", json.dumps(content, ensure_ascii=False))


class XMindToFeishuTests(unittest.TestCase):
    def test_success_diary_xml_escapes_and_keeps_deep_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            xmind_path = Path(tmp) / "成功日记.xmind"
            make_xmind(
                xmind_path,
                [
                    {
                        "rootTopic": {
                            "id": "root",
                            "title": "成功日记",
                            "children": {
                                "attached": [
                                    {
                                        "id": "date-1",
                                        "title": "3月24",
                                        "children": {
                                            "attached": [
                                                {
                                                    "id": "item-1",
                                                    "title": "A & B < C\n下一行",
                                                },
                                                {
                                                    "id": "item-2",
                                                    "title": "",
                                                    "children": {
                                                        "attached": [
                                                            {"id": "child-1", "title": "深层事项"}
                                                        ]
                                                    },
                                                },
                                            ],
                                            "detached": [
                                                {"id": "free-1", "title": "自由记录"}
                                            ],
                                            "summary": [
                                                {"id": "sum-1", "title": "当天总结"}
                                            ],
                                        },
                                    }
                                ]
                            },
                        }
                    }
                ],
            )

            xml, stats = render_xmind_to_xml(xmind_path)

        self.assertIn("<title>成功日记</title>", xml)
        self.assertIn("<h1>3月24</h1>", xml)
        self.assertIn("<ol>", xml)
        self.assertIn('<li seq="auto">A &amp; B &lt; C<br/>下一行</li>', xml)
        self.assertIn('<li seq="auto">未命名事项', xml)
        self.assertIn("<li>深层事项</li>", xml)
        self.assertIn('<li seq="auto"><b>自由主题：</b>自由记录</li>', xml)
        self.assertIn('<blockquote><p><b>概括：</b>当天总结</p></blockquote>', xml)
        self.assertNotIn("&nbsp;", xml)
        self.assertNotIn("2.1.", xml)
        self.assertEqual(stats["total_nodes"], 7)
        self.assertEqual(stats["detached_nodes"], 1)
        self.assertEqual(stats["summary_nodes"], 1)

    def test_duplicate_topic_ids_are_not_rendered_twice(self):
        duplicate = {"id": "same", "title": "只出现一次"}
        with tempfile.TemporaryDirectory() as tmp:
            xmind_path = Path(tmp) / "成功日记.xmind"
            make_xmind(
                xmind_path,
                [
                    {
                        "rootTopic": {
                            "id": "root",
                            "title": "成功日记",
                            "children": {
                                "attached": [
                                    {
                                        "id": "date-1",
                                        "title": "3月25",
                                        "children": {
                                            "attached": [duplicate],
                                            "summary": [duplicate],
                                        },
                                    }
                                ]
                            },
                        }
                    }
                ],
            )

            xml, _stats = render_xmind_to_xml(xmind_path)

        self.assertEqual(xml.count("只出现一次"), 1)

    def test_old_xmind_format_reports_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            xmind_path = Path(tmp) / "旧版.xmind"
            with zipfile.ZipFile(xmind_path, "w") as zf:
                zf.writestr("content.xml", "<xmap-content/>")

            with self.assertRaises(SyncError) as ctx:
                XMindParser(xmind_path).parse()

        self.assertIn("旧版 XMind", str(ctx.exception))

    def test_bad_zip_reports_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            xmind_path = Path(tmp) / "损坏.xmind"
            xmind_path.write_text("not zip", encoding="utf-8")

            with self.assertRaises(SyncError) as ctx:
                XMindParser(xmind_path).parse()

        self.assertIn("文件损坏", str(ctx.exception))

    def test_watch_task_command_targets_script_and_watch_mode(self):
        command = build_watch_task_command()

        self.assertIn("xmind_to_feishu.py", command)
        self.assertIn("supervise", command)

    def test_startup_launcher_text_uses_hidden_supervise_command(self):
        text = build_startup_launcher_text()

        self.assertIn('Set WshShell = CreateObject("WScript.Shell")', text)
        self.assertIn("WshShell.CurrentDirectory", text)
        self.assertIn("xmind_to_feishu.py", text)
        self.assertIn("supervise", text)

    def test_single_instance_lock_blocks_second_holder(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock_path = Path(tmp) / "supervise.lock"
            first = SingleInstanceLock(lock_path)
            second = SingleInstanceLock(lock_path)
            try:
                self.assertTrue(first.acquire())
                self.assertFalse(second.acquire())
            finally:
                second.release()
                first.release()

    def test_sync_error_state_and_success_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "config.json"
            xmind_path = Path(tmp) / "成功日记.xmind"
            make_xmind(
                xmind_path,
                [{"rootTopic": {"id": "root", "title": "成功日记", "children": {"attached": []}}}],
            )
            config = {"consecutive_failures": 1}

            record_error_state(config, "飞书失败", path=state_path)

            self.assertEqual(config["consecutive_failures"], 2)
            self.assertEqual(config["last_error_message"], "飞书失败")

            stats = {"total_nodes": 1, "max_depth": 1, "detached_nodes": 0, "summary_nodes": 0}
            record_sync_success(config, xmind_path, stats, path=state_path)

            self.assertEqual(config["consecutive_failures"], 0)
            self.assertIsNone(config["last_error_message"])
            self.assertEqual(config["last_success_stats"], stats)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["last_success_stats"], stats)

    def test_timestamped_backup_keeps_recent_seven(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup_dir = Path(tmp) / "backups"
            for index in range(10):
                write_timestamped_xml_backup(
                    f"<title>{index}</title>",
                    backup_dir=backup_dir,
                    retain=7,
                    timestamp=f"20260605-00000{index}",
                )

            names = sorted(path.name for path in backup_dir.glob("*.xml"))

        self.assertEqual(len(names), 7)
        self.assertNotIn("成功日记.20260605-000000.xml", names)
        self.assertIn("成功日记.20260605-000009.xml", names)

    def test_lark_version_comparison(self):
        self.assertTrue(version_less_than("1.0.44", "1.0.47"))
        self.assertFalse(version_less_than("1.0.47", "1.0.47"))
        self.assertFalse(version_less_than("1.0.48", "1.0.47"))


if __name__ == "__main__":
    unittest.main()
