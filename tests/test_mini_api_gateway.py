import contextlib
import importlib.util
import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1] / "deploy/mini-api-gateway"
spec = importlib.util.spec_from_file_location("gateway", BASE / "release.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


BROWSE_HTTP = (
    "# Include in nginx http {} scope. WeChat-only mini app allowlist.\nmap "
    '"$request_method:$uri" $mini_api_allowed {\n    default 0;\n    "~^(GET)'
    ':/mini-api/api/prompts$" 1;\n    "~^(GET):/mini-api/api/prompts/search$'
    '" 1;\n    "~^(GET):/mini-api/api/prompts/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-'
    '[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$" 1;\n    "~^(GET):/mini-'
    "api/api/prompts/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-"
    'F]{4}-[0-9a-fA-F]{12}/similar$" 1;\n    "~^(GET):/mini-api/api/(categor'
    'ies|models)$" 1;\n}\n'
)

# Frozen fragments independently hashed from deployed commit 6f3bb67827367c5864b8ecb723464cc7570d34ce.
AUTH_HTTP = BROWSE_HTTP[:-2] + (
    '    "~^(POST):/mini-api/api/auth/wechat$" 1;\n'
    '    "~^(GET):/mini-api/api/auth/me$" 1;\n'
    '    "~^(GET):/mini-api/api/users/me/collects$" 1;\n'
    '    "~^(POST|DELETE):/mini-api/api/prompts/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-'
    '[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/(like|collect)$" 1;\n}\n'
)
AUTH_SERVER = """# Include in the existing HTTPS server {} which can resolve api:8000.
# Public host/CDN must route /mini-api/ to this server without stripping the path.
location = /mini-api { return 404; }
location ^~ /mini-api/ {
    if ($mini_api_allowed = 0) { return 404; }
    client_max_body_size 128k;
    rewrite ^/mini-api(/api/.*)$ $1 break;
    proxy_pass http://api:8000;
    proxy_http_version 1.1;
    proxy_set_header Host $http_host;
    proxy_set_header Authorization $http_authorization;
    proxy_set_header X-Forwarded-Host $http_host;
    proxy_set_header X-Forwarded-Proto https;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 25s;
    proxy_hide_header Set-Cookie;
    add_header X-Content-Type-Options nosniff always;
    add_header Cache-Control "no-store" always;
}
"""


def deployed_config(http=AUTH_HTTP):
    original = "server { server_name aigc-prompt.com; location / { proxy_pass http://frontend; } }\n"
    end = gateway.domain_block(original)[1]
    return original, (
        gateway.HTTP_BEGIN
        + http
        + gateway.HTTP_END
        + original[:end]
        + gateway.SERVER_BEGIN
        + AUTH_SERVER
        + gateway.SERVER_END
        + original[end:]
    )


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

    def test_allowlist_real_frontend_routes_and_denials(self):
        patterns = re.findall(r'"~(.*?)" 1;', (BASE / "http.conf").read_text())

        def allowed(method, path):
            return any(re.fullmatch(pattern, method + ":/mini-api" + path) for pattern in patterns)

        uuid = "12345678-abcd-1234-9876-123456789abc"
        for method, path in [
            ("POST", "/api/auth/wechat"),
            ("GET", "/api/auth/me"),
            ("GET", "/api/users/me/collects"),
            ("POST", f"/api/prompts/{uuid}/like"),
            ("DELETE", f"/api/prompts/{uuid}/collect"),
        ]:
            self.assertTrue(allowed(method, path), (method, path))
        for method, path in [
            ("GET", "/api/auth/wechat"),
            ("POST", "/api/auth/me"),
            ("POST", "/api/auth/send-code"),
            ("POST", "/api/submissions"),
            ("GET", "/api/admin/prompts"),
            ("GET", "/api/prompts/me/collections"),
            ("POST", "/api/prompts/not-uuid/like"),
            ("GET", "/api/users/me"),
        ]:
            self.assertFalse(allowed(method, path), (method, path))

    def test_upgrade_known_browse_blocks_and_preserve_unrelated_config(self):
        original = "server { server_name aigc-prompt.com; location / { proxy_pass http://frontend; } }\n"
        end = gateway.domain_block(original)[1]
        previous = (
            gateway.HTTP_BEGIN
            + BROWSE_HTTP
            + gateway.HTTP_END
            + original[:end]
            + gateway.SERVER_BEGIN
            + AUTH_SERVER
            + gateway.SERVER_END
            + original[end:]
        )
        candidate = gateway.merge(previous)
        self.assertEqual(gateway.remove(candidate), original)
        self.assertIn("/api/auth/wechat", candidate)
        with self.assertRaises(ValueError):
            gateway.merge(previous.replace("default 0;", "default 1;"))
        with self.assertRaises(ValueError):
            gateway.merge(previous + gateway.HTTP_BEGIN + BROWSE_HTTP + gateway.HTTP_END)
        with self.assertRaises(ValueError):
            moved = previous.replace(gateway.HTTP_BEGIN + BROWSE_HTTP + gateway.HTTP_END, "")
            gateway.merge(moved + gateway.HTTP_BEGIN + BROWSE_HTTP + gateway.HTTP_END)

    def test_upgrade_deployed_auth_to_profile_and_reject_repeat_or_changed_blocks(self):
        self.assertEqual(
            gateway.digest(AUTH_HTTP.encode()),
            "cec0167efdc846f22b080f3dc943f67379485f8c2605de66ba3c661ddf6ade3c",
        )
        self.assertEqual(
            gateway.digest(AUTH_SERVER.encode()),
            "6045aca9faa99451ec5500f03dfb6d353a533e475be0e28b02857f011882d452",
        )
        original, previous = deployed_config()
        candidate = gateway.merge(previous)
        self.assertEqual(gateway.remove(candidate), original)
        self.assertIn("/mini-api/api/auth/me/profile", candidate)
        for invalid in [
            candidate,
            previous.replace("default 0;", "default 1;", 1),
            previous + gateway.HTTP_BEGIN + AUTH_HTTP + gateway.HTTP_END,
            previous.replace(gateway.HTTP_BEGIN + AUTH_HTTP + gateway.HTTP_END, "")
            + gateway.HTTP_BEGIN
            + AUTH_HTTP
            + gateway.HTTP_END,
        ]:
            with self.assertRaises(ValueError):
                gateway.merge(invalid)

    def test_only_canonical_post_profile_gets_larger_body_and_self_contained_proxy_rules(self):
        config = (BASE / "http.conf").read_text()
        profile_pattern = r"~^(POST):/mini-api/api/auth/me/profile$"
        self.assertIn('"' + profile_pattern + '" $mini_api_profile_canonical;', config)
        self.assertIn("map $request_uri $mini_api_profile_canonical {", config)
        raw_pattern = re.search(r'"~(\^/mini-api/api/auth/me/profile.*?)" 1;', config).group(1)
        for path in ["/mini-api/api/auth/me/profile", "/mini-api/api/auth/me/profile?x=1"]:
            self.assertIsNotNone(re.fullmatch(raw_pattern, path), path)
        for path in [
            "/mini-api/api/auth/me/profile/",
            "/mini-api/api/auth/me//profile",
            "/mini-api/api/auth/me/%70rofile",
            "/mini-api/api/auth/me/./profile",
            "/mini-api/api/auth/me/profile/../profile",
            "/mini-api/api/auth/me/profile/extra",
        ]:
            self.assertIsNone(re.fullmatch(raw_pattern, path), path)
        for method in ["GET", "PUT", "PATCH", "DELETE", "OPTIONS"]:
            self.assertIsNone(re.fullmatch(profile_pattern[1:], method + ":/mini-api/api/auth/me/profile"))
        server = (BASE / "server.conf").read_text()
        profile_marker = "location = /mini-api/api/auth/me/profile {\n"
        browse_marker = "location ^~ /mini-api/ {\n"
        self.assertLess(server.index(profile_marker), server.index(browse_marker))
        profile = server.split(profile_marker, 1)[1].split("\n}\n", 1)[0]
        browse = server.split(browse_marker, 1)[1].split("\n}\n", 1)[0]
        self.assertEqual(profile.replace("client_max_body_size 3m;", "client_max_body_size 128k;"), browse)
        self.assertEqual(server.count("client_max_body_size 128k;"), 1)
        self.assertEqual(server.count("client_max_body_size 3m;"), 1)
        for directive in [
            "if ($mini_api_allowed = 0) { return 404; }",
            "rewrite ^/mini-api(/api/.*)$ $1 break;",
            "proxy_set_header Authorization $http_authorization;",
            'add_header Cache-Control "no-store" always;',
        ]:
            self.assertEqual(server.count(directive), 2)

    def test_profile_release_and_rollback_restore_actual_auth_config_and_inode(self):
        _, previous = deployed_config()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "releases").mkdir()
            target = root / "nginx.conf"
            target.write_text(previous)
            inode = target.stat().st_ino
            with (
                patch.object(gateway, "ROOT", root),
                patch.object(gateway, "TARGET", target),
                patch.object(gateway, "verify_mount") as mount,
                patch.object(gateway, "nginx"),
                patch.object(gateway, "reload_nginx"),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                gateway.apply(gateway.digest(previous.encode()), "a" * 40)
                candidate = target.read_bytes()
                backup = next((root / "releases").iterdir())
                self.assertEqual((backup / "original.conf").read_text(), previous)
                self.assertEqual(
                    json.loads((backup / "manifest.json").read_text())["candidate_sha256"],
                    gateway.digest(candidate),
                )
                gateway.rollback(backup)
                self.assertEqual(target.read_text(), previous)
                self.assertEqual(target.stat().st_ino, inode)
                self.assertEqual(mount.call_count, 6)

    def test_release_cli_keeps_root_and_source_hash_guards(self):
        argv = ["release.py", "apply", "--expected-sha256", "a" * 64, "--git-sha", "b" * 40]
        with patch.object(gateway.os, "geteuid", return_value=1000), patch("sys.argv", argv):
            with self.assertRaisesRegex(ValueError, "requires root"):
                gateway.main()
        with tempfile.TemporaryDirectory() as temporary:
            with (
                patch.object(gateway, "ROOT", Path(temporary)),
                patch.object(gateway.os, "geteuid", return_value=0),
                patch.object(gateway, "apply") as apply,
            ):
                with patch("sys.argv", argv):
                    gateway.main()
                apply.assert_called_once_with("a" * 64, "b" * 40)
                for index in [3, 5]:
                    invalid = [*argv[:index], "invalid", *argv[index + 1 :]]
                    with patch("sys.argv", invalid), self.assertRaisesRegex(ValueError, "Invalid source"):
                        gateway.main()
                self.assertEqual(apply.call_count, 1)

    def test_plan_runs_locally_and_rollback_cli_dispatches_without_network(self):
        _, previous = deployed_config()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.conf"
            output = root / "candidate.conf"
            source.write_text(previous)
            with patch("sys.argv", ["release.py", "plan", str(source), str(output)]):
                gateway.main()
            self.assertIn("/mini-api/api/auth/me/profile", output.read_text())
            with (
                patch.object(gateway, "ROOT", root),
                patch.object(gateway.os, "geteuid", return_value=0),
                patch.object(gateway, "rollback") as rollback,
                patch("sys.argv", ["release.py", "rollback", "reviewed-backup"]),
            ):
                gateway.main()
                rollback.assert_called_once_with("reviewed-backup")

    def test_tampered_profile_rollback_backup_never_writes_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backup = root / "releases/mini-api-review"
            backup.mkdir(parents=True)
            (backup / "manifest.json").write_text(
                json.dumps(
                    {
                        "original_sha256": gateway.digest(b"original"),
                        "candidate_sha256": gateway.digest(b"candidate"),
                    }
                )
            )
            (backup / "original.conf").write_bytes(b"tampered")
            (backup / "candidate.conf").write_bytes(b"candidate")
            with patch.object(gateway, "ROOT", root), patch.object(gateway, "install") as install:
                with self.assertRaisesRegex(ValueError, "integrity check"):
                    gateway.rollback(backup)
                with self.assertRaisesRegex(ValueError, "Invalid backup"):
                    gateway.rollback(root / "unreviewed")
                install.assert_not_called()

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
