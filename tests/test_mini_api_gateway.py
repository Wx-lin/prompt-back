import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1] / "deploy/mini-api-gateway"
spec = importlib.util.spec_from_file_location("gateway", BASE / "release.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


class GatewayTests(unittest.TestCase):
    def test_only_selected_server_changes_and_roundtrip(self):
        original = """# example { comment }
server { listen 80; server_name other.com; }
server {
 listen 8443 ssl;
 server_name aigc-prompt.com;
 location / { proxy_pass http://frontend:3000; }
}
server { listen 8443; server_name 62.234.69.93; }
"""
        candidate = gateway.merge(original)
        self.assertIn("server { listen 80; server_name other.com; }", candidate)
        self.assertIn("server { listen 8443; server_name 62.234.69.93; }", candidate)
        self.assertEqual(gateway.remove(candidate), original)
        with self.assertRaises(ValueError):
            gateway.merge(candidate)

    def test_reject_ambiguous_or_existing(self):
        for original in [
            "server { server_name other.com; }",
            "server { server_name aigc-prompt.com; }" * 2,
            "server { server_name aigc-prompt.com; location /mini-api/ {} }",
            "server { server_name aigc-prompt.com; # unbalanced\n",
        ]:
            with self.assertRaises(ValueError):
                gateway.merge(original)

    def test_allowlist_browse_only(self):
        config = (BASE / "http.conf").read_text()
        self.assertIn("default 0;", config)
        self.assertNotIn("POST", config)
        self.assertNotIn("/auth/", config)
        self.assertNotIn("/admin/", config)

    def test_install_preserves_inode_and_rejects_concurrent_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "nginx.conf"
            target.write_bytes(b"original")
            target.chmod(0o640)
            inode = target.stat().st_ino
            with (
                patch.object(gateway, "TARGET", target),
                patch.object(gateway, "verify_mount"),
                patch.object(gateway, "nginx"),
                patch.object(gateway, "reload_nginx"),
            ):
                gateway.install(b"candidate", b"original", gateway.digest(b"original"))
                self.assertEqual(target.read_bytes(), b"candidate")
                self.assertEqual(target.stat().st_ino, inode)
                self.assertEqual(target.stat().st_mode & 0o777, 0o640)
                with self.assertRaises(ValueError):
                    gateway.install(b"other", b"original", gateway.digest(b"original"))
                self.assertEqual(target.read_bytes(), b"candidate")

    def test_failed_nginx_check_restores_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "nginx.conf"
            target.write_bytes(b"original")
            with (
                patch.object(gateway, "TARGET", target),
                patch.object(gateway, "verify_mount"),
                patch.object(gateway, "nginx", side_effect=[RuntimeError("invalid config"), None]),
                patch.object(gateway, "reload_nginx") as reload,
            ):
                with self.assertRaises(RuntimeError):
                    gateway.install(b"invalid", b"original", gateway.digest(b"original"))
                self.assertEqual(target.read_bytes(), b"original")
                reload.assert_called_once()

    def test_nginx_timeout_restores_original(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "nginx.conf"
            target.write_bytes(b"original")
            error = gateway.subprocess.TimeoutExpired(cmd="nginx -t", timeout=25)
            with (
                patch.object(gateway, "TARGET", target),
                patch.object(gateway, "verify_mount"),
                patch.object(gateway, "nginx", side_effect=[error, None]),
                patch.object(gateway, "reload_nginx") as reload,
            ):
                with self.assertRaises(gateway.subprocess.TimeoutExpired):
                    gateway.install(b"candidate", b"original", gateway.digest(b"original"))
                self.assertEqual(target.read_bytes(), b"original")
                reload.assert_called_once()

    def test_nginx_commands_have_bounded_timeout(self):
        with patch.object(gateway.subprocess, "run") as run:
            gateway.nginx("-t")
            gateway.reload_nginx()
            self.assertEqual(run.call_count, 2)
            self.assertTrue(all(call.kwargs["timeout"] == 25 for call in run.call_args_list))

    def test_bind_mount_hash_is_checked(self):
        with patch.object(gateway.subprocess, "run") as run:
            run.return_value.stdout = gateway.digest(b"old") + "  /etc/nginx/conf.d/default.conf\n"
            gateway.verify_mount(gateway.digest(b"old"))
            with self.assertRaisesRegex(ValueError, "bind mount differs"):
                gateway.verify_mount(gateway.digest(b"new"))
            self.assertEqual(run.call_args.kwargs["timeout"], 25)

    def test_stale_mount_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "nginx.conf"
            target.write_bytes(b"original")
            with (
                patch.object(gateway, "TARGET", target),
                patch.object(gateway, "verify_mount", side_effect=ValueError("stale mount")),
                patch.object(gateway, "write_in_place") as write,
            ):
                with self.assertRaisesRegex(ValueError, "stale mount"):
                    gateway.install(b"candidate", b"original", gateway.digest(b"original"))
                write.assert_not_called()
