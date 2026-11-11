"""Offline loader acceptance using a synthetic GTK ABI, never a real GUI."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from unifier.native_library_launcher import RULES


@unittest.skipUnless(shutil.which("cc"), "a C compiler is needed for the synthetic loader fixture")
class NativeLoaderRuntimeTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.lib = self.root / "libraries"
        self.lib.mkdir()
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.launcher = Path(__file__).resolve().parents[1] / "src/unifier/native_library_launcher.py"
        # Do not inherit host display, bus, preload or Python configuration.
        self.env = {"PATH": os.defpath, "LD_LIBRARY_PATH": str(self.lib),
                    "GTK_USE_PORTAL": "1", "TMPDIR": str(self.cache)}

    def build(self, *, soname="libgtk-3.so.0", omit=None):
        code = "\n".join(f"int {name}(void) {{ return 123; }}" for name in
                         RULES["gtk3-native"]["symbols"] if name != omit)
        code += "\nint unifier_fixture_marker(void) { return 123; }\n"
        source = self.root / "fixture.c"
        source.write_text(code)
        result = subprocess.run([shutil.which("cc"), "-shared", "-fPIC",
                                 f"-Wl,-soname,{soname}", "-o", str(self.lib / "libgtk-3.so.0"),
                                 str(source)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def run_launcher(self, code, *args):
        return subprocess.run([sys.executable, "-I", str(self.launcher), "gtk3-native", "--",
                               sys.executable, "-I", "-c", code, *args],
                              env=self.env, capture_output=True, text=True, timeout=15)

    def test_alias_fixes_real_dynamic_loading_and_preserves_process_argv(self):
        self.build()
        code = '''import ctypes,json,os,sys
lib=ctypes.CDLL("libgtk-3.so")
assert lib.unifier_fixture_marker()==123
print(json.dumps({"value":lib.gtk_file_chooser_native_new(),"argv":sys.argv[1:],
                 "path":os.environ["LD_LIBRARY_PATH"],"pid":os.getpid()}))
'''
        # Reproduce the failure before applying the compatibility launcher.
        before_code = f"import ctypes; ctypes.CDLL({str(self.lib / 'libgtk-3.so')!r})"
        before = subprocess.run([sys.executable, "-I", "-c", before_code], env=self.env,
                                capture_output=True, text=True, timeout=15)
        self.assertNotEqual(before.returncode, 0)
        self.assertIn("libgtk-3.so", before.stderr)
        child = subprocess.Popen([sys.executable, "-I", str(self.launcher), "gtk3-native", "--",
                                  sys.executable, "-I", "-c", code, "中文 空格", ";$(false)"],
                                 env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            output, error = child.communicate(timeout=15)
        except BaseException:
            child.kill()
            child.communicate()
            raise
        self.assertEqual(child.returncode, 0, error)
        result = json.loads(output)
        self.assertEqual(result["value"], 123)
        self.assertEqual(result["pid"], child.pid)  # exec, no supervising wrapper left behind
        self.assertEqual(result["argv"], ["中文 空格", ";$(false)"])
        directory, old = result["path"].split(":", 1)
        self.assertEqual(old, str(self.lib))
        self.assertEqual((Path(directory) / "libgtk-3.so").resolve(), self.lib / "libgtk-3.so.0")
        self.assertEqual(sorted(p.name for p in self.lib.iterdir()), ["libgtk-3.so.0"])

    def test_glib_module_loader_can_resolve_the_same_alias(self):
        self.build()
        # The vendor uses GModule rather than calling dlopen itself.
        code = '''import ctypes
try:
    glib=ctypes.CDLL("libgmodule-2.0.so.0")
except OSError:
    raise SystemExit(77)
glib.g_module_open.argtypes=[ctypes.c_char_p,ctypes.c_int]
glib.g_module_open.restype=ctypes.c_void_p
handle=glib.g_module_open(b"libgtk-3.so",1)
assert handle
symbol=ctypes.c_void_p()
glib.g_module_symbol.argtypes=[ctypes.c_void_p,ctypes.c_char_p,ctypes.POINTER(ctypes.c_void_p)]
assert glib.g_module_symbol(handle,b"unifier_fixture_marker",ctypes.byref(symbol))
assert ctypes.CFUNCTYPE(ctypes.c_int)(symbol.value)()==123
'''
        result = self.run_launcher(code)
        if result.returncode == 77:
            self.skipTest("host GModule unavailable; dlopen acceptance covered separately")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_native_symbol_refuses_to_execute_application(self):
        self.build(omit="gtk_native_dialog_run")
        result = self.run_launcher("print('APPLICATION_STARTED')")
        self.assertEqual(result.returncode, 126)
        self.assertNotIn("APPLICATION_STARTED", result.stdout)
        self.assertIn("gtk_native_dialog_run", result.stderr)
        self.assertFalse(list(self.cache.iterdir()))

    def test_wrong_soname_refuses_to_execute_application(self):
        self.build(soname="libgtk-4.so.1")
        result = self.run_launcher("print('APPLICATION_STARTED')")
        self.assertEqual(result.returncode, 126)
        self.assertNotIn("APPLICATION_STARTED", result.stdout)
        self.assertIn("SONAME", result.stderr)
        self.assertFalse(list(self.cache.iterdir()))
