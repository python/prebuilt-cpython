import pathlib
import py_compile
import tempfile
import unittest

from relocatable import patch_path, placeholder_prefix


class PatchPathTests(unittest.TestCase):
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
