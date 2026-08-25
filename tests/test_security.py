# -*- coding: utf-8 -*-
from __future__ import annotations

import io
import os
import re
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path

import notification_service
from security_utils import UnsafeArchiveError, load_or_create_secret_key, validate_zip_archive


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SecurityUtilitiesTests(unittest.TestCase):
    def test_generated_secret_is_strong_and_stable(self):
        old_value = os.environ.pop("FLASK_SECRET_KEY", None)
        try:
            with tempfile.TemporaryDirectory(prefix="dar-secret-test-") as directory:
                first = load_or_create_secret_key(Path(directory))
                second = load_or_create_secret_key(Path(directory))
                self.assertEqual(first, second)
                self.assertGreaterEqual(len(first), 32)
        finally:
            if old_value is not None:
                os.environ["FLASK_SECRET_KEY"] = old_value

    def test_zip_guard_accepts_small_archive_and_rejects_high_ratio(self):
        normal = io.BytesIO()
        with zipfile.ZipFile(normal, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", "hello")
        normal.seek(0)
        validate_zip_archive(normal, max_files=10, max_uncompressed_bytes=1024)

        suspicious = io.BytesIO()
        with zipfile.ZipFile(suspicious, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("huge.txt", "A" * 2_000_000)
        suspicious.seek(0)
        with self.assertRaises(UnsafeArchiveError):
            validate_zip_archive(
                suspicious,
                max_files=10,
                max_uncompressed_bytes=3_000_000,
                max_compression_ratio=100,
            )

    def test_email_rate_limit(self):
        conn = sqlite3.connect(":memory:")
        try:
            for _ in range(3):
                notification_service.claim_test_email_slot(conn, 1)
            with self.assertRaises(notification_service.EmailRateLimitError):
                notification_service.claim_test_email_slot(conn, 1)
        finally:
            conn.close()

    def test_every_post_form_has_csrf_field(self):
        form_pattern = re.compile(
            r"<form\b(?=[^>]*method\s*=\s*['\"]post['\"])[^>]*>", re.I
        )
        for template in (PROJECT_ROOT / "templates").glob("*.html"):
            source = template.read_text(encoding="utf-8-sig")
            for match in form_pattern.finditer(source):
                nearby = source[match.start():match.start() + 300]
                self.assertRegex(
                    nearby,
                    r"_csrf\.html|name\s*=\s*['\"]_csrf_token",
                    f"Нет CSRF-поля после формы в {template.name}",
                )


class FlaskSecurityFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_cwd = Path.cwd()
        cls.runtime_dir = tempfile.TemporaryDirectory(prefix="dar-security-test-")
        os.chdir(cls.runtime_dir.name)
        os.environ["FLASK_SECRET_KEY"] = "test-only-" + "x" * 64
        import app as application_module

        cls.module = application_module

    @classmethod
    def tearDownClass(cls):
        os.chdir(cls.original_cwd)
        os.environ.pop("FLASK_SECRET_KEY", None)
        cls.runtime_dir.cleanup()

    def test_csrf_and_security_headers(self):
        client = self.module.app.test_client()
        response = client.get("/login")
        html = response.get_data(as_text=True)
        token = re.search(r'name="_csrf_token" value="([^"]+)"', html).group(1)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("HttpOnly", response.headers.get("Set-Cookie", ""))
        self.assertIn("SameSite=Lax", response.headers.get("Set-Cookie", ""))

        rejected = client.post("/login", data={"username": "x", "password": "x"})
        accepted = client.post(
            "/login",
            data={"username": "x", "password": "x", "_csrf_token": token},
        )
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(client.get("/logout").status_code, 405)

    def test_configured_secret_is_used(self):
        self.assertEqual(self.module.app.secret_key, "test-only-" + "x" * 64)
        self.assertGreaterEqual(len(self.module.app.secret_key), 32)

    def test_authenticated_pages_render_and_logout_requires_csrf(self):
        client = self.module.app.test_client()
        register_page = client.get("/register")
        token = re.search(
            r'name="_csrf_token" value="([^"]+)"', register_page.get_data(as_text=True)
        ).group(1)
        registered = client.post(
            "/register",
            data={
                "username": "security-test-user",
                "password": "test-password",
                "_csrf_token": token,
            },
        )
        self.assertEqual(registered.status_code, 302)

        pages = (
            "/",
            "/tenders",
            "/priorities",
            "/favorites",
            "/irrelevant",
            "/analytics",
            "/profile",
            "/settings",
            "/tasks",
            "/upload-history",
        )
        for path in pages:
            with self.subTest(path=path):
                self.assertEqual(client.get(path).status_code, 200)

        home = client.get("/").get_data(as_text=True)
        logout_token = re.search(
            r'name="_csrf_token" value="([^"]+)"', home
        ).group(1)
        self.assertEqual(
            client.post("/logout", data={"_csrf_token": logout_token}).status_code,
            302,
        )


if __name__ == "__main__":
    unittest.main()
