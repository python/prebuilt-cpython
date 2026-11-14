import json
import os
import pathlib
import py_compile
import subprocess
import sys
import tempfile
import unittest

from relocatable import patch_path, placeholder_prefix


class PatchPathTests(unittest.TestCase):
    def test_python_shebang(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory).resolve()
            names = (
                "with spaces",
                "with\ttab",
                "with ''' quotes",
                'with """ quotes',
                r"with \u backslash",
                "with $HOME and $(false)",
                "with café",
                "x" * (250 - len(os.fsencode(root)) - 1),
            )
            for name in names:
                with self.subTest(name=name):
                    prefix = root / name
                    bin_dir = prefix / "bin"
                    bin_dir.mkdir(parents=True)
                    interpreter = bin_dir / "python3.16"
                    interpreter.symlink_to(sys.executable)
                    script = bin_dir / "pydoc3.16"
                    script.write_text(
                        f"#!{placeholder_prefix()}/bin/python3.16\n"
                        "import json, sys\n"
                        "print(json.dumps([sys.executable, sys.argv[1:]]))\n",
                        encoding="utf-8",
                    )
                    script.chmod(0o755)
                    patch_path(prefix, str(prefix))
                    result = subprocess.run(
                        [script, "argument with spaces", "$literal"],
                        env={**os.environ, "PATH": ""},
                        capture_output=True, text=True, check=True,
                    )
                    executable, arguments = json.loads(result.stdout)
                    self.assertTrue(pathlib.Path(executable).samefile(interpreter))
                    self.assertEqual(arguments, ["argument with spaces", "$literal"])

    def test_prefix_replacement(self):
        prefix = placeholder_prefix().encode()
        target = b"/opt/python"
        padding = b"\0" * (len(prefix) - len(target))
        cases = (
            (prefix + b"/lib", target + b"/lib"),
            (
                b"header\0" + prefix + b"/lib\0tail",
                b"header\0" + target + b"/lib" + padding + b"\0tail",
            ),
            (
                b"\0" + prefix + b"/lib:" + prefix + b"/lib64\0",
                b"\0" + target + b"/lib:" + target + b"/lib64" + padding * 2 + b"\0",
            ),
            (b"\0" + prefix + b"/lib", b"\0" + target + b"/lib" + padding),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            path = root / "file"
            for original, expected in cases:
                with self.subTest(original=original):
                    path.write_bytes(original)
                    patch_path(root, target.decode())
                    self.assertEqual(path.read_bytes(), expected)

    def test_bytecode_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            source = root / "module.py"
            source.write_text("pass\n", encoding="utf-8")
            for suffix in (".pyc", ".pyo"):
                with self.subTest(suffix=suffix):
                    bytecode = root / ("module" + suffix)
                    py_compile.compile(
                        str(source), cfile=str(bytecode),
                        dfile=placeholder_prefix() + "/module.py", doraise=True,
                    )
                    original = bytecode.read_bytes()
                    patch_path(root, "/opt/python")
                    self.assertEqual(bytecode.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
