"""The package page opens only for a cross-family whitelist format."""
from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from unifier.errors import UnifierError
from unifier.package_offer import (
    DESKTOP_ID,
    admission,
    desktop_entry,
    host_launch_argv,
    parse_choice,
    prefer_mime,
    remove_registration,
    restore_mime,
    select_host_desktop,
    whitelist_formats,
    write_registration,
)
from unifier.shortcuts import cmd_offer, build_context


def facts(distro):
    return {
        "os_release": {"ID": distro, "VERSION_ID": "1"},
        "system": "Linux", "machine": "x86_64", "libc": "glibc",
        "ostree": False, "transactional": False,
    }


class OfferPolicyTests(unittest.TestCase):
    def test_fedora_keeps_rpm_and_offers_deb(self):
        self.assertEqual(whitelist_formats(facts("fedora")), ["deb", "arch"])
        self.assertFalse(admission({"format": "rpm", "candidates": ["fedora44"]}, facts("fedora"))["enter"])
        decision = admission({"format": "deb", "candidates": ["debian13"]}, facts("fedora"))
        self.assertTrue(decision["enter"])
        self.assertEqual(decision["targets"], ["debian13"])

    def test_debian_keeps_deb_and_offers_rpm(self):
        self.assertFalse(admission({"format": "deb", "candidates": ["debian13"]}, facts("ubuntu"))["enter"])
        decision = admission({"format": "rpm", "candidates": ["fedora44"]}, facts("debian"))
        self.assertTrue(decision["enter"])
        self.assertEqual(decision["reason"], "cross-family")

    def test_choice_requires_explicit_answer(self):
        self.assertEqual(parse_choice("y"), "unifier")
        self.assertEqual(parse_choice("N"), "host")
        with self.assertRaises(UnifierError):
            parse_choice("")
        with self.assertRaises(UnifierError):
            parse_choice("maybe")

    def test_registration_does_not_claim_the_host_format(self):
        text, previous = prefer_mime(
            "[Default Applications]\napplication/x-rpm=org.kde.discover.desktop\n",
            "application/vnd.debian.binary-package", DESKTOP_ID)
        self.assertIn("application/vnd.debian.binary-package=" + DESKTOP_ID, text)
        self.assertIn("application/x-rpm=org.kde.discover.desktop", text)
        self.assertIsNone(previous)
        text, previous = prefer_mime(text, "application/x-rpm", "other.desktop")
        self.assertEqual(previous, "org.kde.discover.desktop")
        restored = restore_mime(text, "application/x-rpm", previous)
        self.assertIn("application/x-rpm=org.kde.discover.desktop", restored)
        self.assertNotIn("application/x-rpm=other.desktop", restored)

    def test_handoff_cannot_call_unifier_again(self):
        with self.assertRaises(UnifierError):
            host_launch_argv(DESKTOP_ID, "/tmp/package.deb")
        self.assertEqual(select_host_desktop(DESKTOP_ID, {DESKTOP_ID, "org.kde.ark.desktop"}),
                         "org.kde.ark.desktop")

    def test_setup_records_only_foreign_mimes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            desktop = root / "applications"
            mime = root / "mime"
            desktop.mkdir()
            (desktop / "mimeapps.list").write_text(
                "[Default Applications]\napplication/vnd.debian.binary-package=org.kde.ark.desktop\n",
                encoding="utf-8")
            formats = write_registration(
                root / "state", desktop, mime, facts=facts("fedora"), launcher="/usr/bin/unifier",
                query_default=lambda mime_type: None)
            self.assertEqual(formats, ["deb", "arch"])
            entry = (desktop / DESKTOP_ID).read_text(encoding="utf-8")
            self.assertIn("application/vnd.debian.binary-package;", entry)
            self.assertNotIn("application/x-rpm", entry)
            self.assertIn("Terminal=true", entry)
            self.assertTrue((mime / "packages" / "unifier-archpkg.xml").is_file())
            remove_registration(root / "state", desktop, mime)
            self.assertIn("org.kde.ark.desktop", (desktop / "mimeapps.list").read_text(encoding="utf-8"))
            self.assertNotIn(DESKTOP_ID, (desktop / "mimeapps.list").read_text(encoding="utf-8"))

    def test_dirs_use_process_environment_when_context_has_none(self):
        from unifier import shortcuts
        ctx = build_context(Path("/tmp/unifier-offer-root"))
        self.assertIsNone(ctx.environ)
        applications, _mime = shortcuts._offer_dirs(ctx)
        self.assertEqual(applications.name, "applications")

    def test_command_does_not_prompt_for_host_rpm(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = Path(temporary) / "example.rpm"
            package.write_bytes(b"\xed\xab\xee\xdbrest")
            asked = []
            out = StringIO()
            ctx = build_context(Path(temporary) / "home", stdin_isatty=True,
                                input_fn=lambda _prompt: asked.append(True) or "y",
                                out=out, err=StringIO(),
                                environ={"LC_ALL": "C", "LANG": "C"}, locale_files=())
            seen = {}

            def handoff(_ctx, path, mime):
                seen["path"] = path
                seen["mime"] = mime
                return 0

            code = cmd_offer(ctx, [str(package)], facts=facts("fedora"), handoff=handoff)
            self.assertEqual(code, 0)
            self.assertEqual(asked, [])
            self.assertEqual(seen["mime"], "application/x-rpm")
            self.assertIn("host distribution", out.getvalue())


if __name__ == "__main__":
    unittest.main()
