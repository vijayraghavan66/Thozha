"""Security tests for the development workspace boundary (containment, symlinks, execution limits, audit)."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from jarvis_link_host import dev_workspace as dw


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        base = Path(self._tmp.name).resolve()
        self.outside = base / "outside"
        self.outside.mkdir()
        (self.outside / "secret.txt").write_text("outside contents")
        self.root = base / "workspace"
        self.root.mkdir()
        self.ws = dw.Workspace.open(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    # --- traversal, absolute and malformed names ------------------------------------------------

    def test_rejects_traversal_and_absolute_and_malformed_names(self):
        bad = ["../outside/secret.txt", "a/../../outside/secret.txt", "./x.py", "/etc/passwd", "~/x.py",
               "a//b.py", "a\\b.py", "x\x00.py", "", "a/./b.py", "a/" + "b" * 300, ".hidden/..", "bad name.py"]
        for name in bad:
            with self.subTest(name=name):
                with self.assertRaises(dw.WorkspaceError):
                    self.ws.resolve(name)
                with self.assertRaises(dw.WorkspaceError):
                    self.ws.write_text(name, "x")

    def test_plain_nested_names_resolve_inside_the_root(self):
        path = self.ws.resolve("pkg/module_1.py", for_write=True)
        self.assertTrue(path.is_relative_to(self.root))
        self.assertEqual(path, self.root / "pkg" / "module_1.py")

    # --- symlinks at every level -----------------------------------------------------------------

    def test_symlink_file_pointing_outside_is_refused_for_read_and_write(self):
        link = self.root / "link.txt"
        os.symlink(self.outside / "secret.txt", link)
        with self.assertRaises(dw.WorkspaceError):
            self.ws.read_text("link.txt")
        with self.assertRaises(dw.WorkspaceError):
            self.ws.write_text("link.txt", "overwrite")
        self.assertEqual((self.outside / "secret.txt").read_text(), "outside contents")

    def test_dangling_symlink_is_refused(self):
        os.symlink(self.outside / "missing.txt", self.root / "dangling.txt")
        with self.assertRaises(dw.WorkspaceError):
            self.ws.write_text("dangling.txt", "create-through-link")
        self.assertFalse((self.outside / "missing.txt").exists())

    def test_symlinked_directory_in_the_path_is_refused(self):
        os.symlink(self.outside, self.root / "escape")
        with self.assertRaises(dw.WorkspaceError):
            self.ws.write_text("escape/new.py", "print('x')")
        self.assertFalse((self.outside / "new.py").exists())

    def test_symlink_inside_the_workspace_is_still_refused(self):
        (self.root / "real.txt").write_text("inside")
        os.symlink(self.root / "real.txt", self.root / "alias.txt")
        with self.assertRaises(dw.WorkspaceError):
            self.ws.read_text("alias.txt")

    def test_symlinked_workspace_root_is_refused(self):
        link_root = self.root.parent / "linked-root"
        os.symlink(self.root, link_root)
        with self.assertRaises(dw.WorkspaceError):
            dw.Workspace.open(link_root)

    def test_symlinks_in_workspace_root_ancestors_are_refused(self):
        components = ["Documents", "Jarvis-Lab", "agent-evaluation", "taskflow"]
        for link_component in components:
            with self.subTest(component=link_component):
                case = self.root.parent / f"ancestor-{link_component}"
                home = case / "home"
                target = case / "target"
                target.joinpath(*components).mkdir(parents=True)
                home.mkdir(parents=True)

                current = home
                target_parent = target
                for component in components:
                    if component == link_component:
                        os.symlink(
                            target_parent / component,
                            current / component,
                            target_is_directory=True,
                        )
                        break
                    current = current / component
                    current.mkdir()
                    target_parent = target_parent / component

                candidate = home.joinpath(*components)
                with self.assertRaises(dw.WorkspaceError):
                    dw.Workspace.open(candidate)

    def test_listing_never_reveals_or_follows_symlinks(self):
        (self.root / "visible.py").write_text("x")
        os.symlink(self.outside / "secret.txt", self.root / "hidden-link")
        self.assertEqual(self.ws.list_dir("."), ["visible.py"])

    # --- file operations -------------------------------------------------------------------------

    def test_write_then_read_roundtrip_creates_nested_directories(self):
        receipt = self.ws.write_text("src/app/main.py", "print('hello')\n")
        self.assertEqual(receipt["path"], "src/app/main.py")
        self.assertEqual(self.ws.read_text("src/app/main.py"), "print('hello')\n")
        self.assertEqual(receipt["bytes"], len("print('hello')\n".encode()))

    def test_oversized_and_nul_content_is_refused(self):
        with self.assertRaises(dw.WorkspaceError):
            self.ws.write_text("big.txt", "a" * (dw.MAX_FILE_BYTES + 1))
        with self.assertRaises(dw.WorkspaceError):
            self.ws.write_text("nul.txt", "a\x00b")

    def test_write_is_atomic_and_leaves_no_temporary_files(self):
        self.ws.write_text("note.txt", "first")
        self.ws.write_text("note.txt", "second")
        self.assertEqual(self.ws.read_text("note.txt"), "second")
        self.assertEqual([p.name for p in self.root.iterdir()], ["note.txt"])

    def test_directory_cannot_be_written_as_a_file(self):
        (self.root / "pkg").mkdir()
        with self.assertRaises(dw.WorkspaceError):
            self.ws.write_text("pkg", "x")

    def test_open_refuses_relative_or_missing_roots(self):
        with self.assertRaises(dw.WorkspaceError):
            dw.Workspace.open("relative/path")
        with self.assertRaises(dw.WorkspaceError):
            dw.Workspace.open(self.root / "does-not-exist")

    # --- execution -------------------------------------------------------------------------------

    def _write_suite(self, passing: bool):
        (self.root / "tests").mkdir(exist_ok=True)
        (self.root / "app.py").write_text("def value():\n    return 2\n")
        expected = 2 if passing else 3
        (self.root / "tests" / "test_app.py").write_text(
            "import unittest\nfrom app import value\n\n"
            "class T(unittest.TestCase):\n"
            "    def test_value(self):\n"
            f"        self.assertEqual(value(), {expected})\n"
            "    def test_truth(self):\n"
            "        self.assertTrue(True)\n"
        )

    def test_run_tests_reports_a_passing_suite(self):
        self._write_suite(passing=True)
        result = self.ws.run_tests()
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["tests_ran"], 2)
        self.assertFalse(result["timed_out"])

    def test_run_tests_reports_failures_honestly(self):
        self._write_suite(passing=False)
        result = self.ws.run_tests()
        self.assertFalse(result["passed"])
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["tests_ran"], 2)

    def test_run_tests_requires_a_tests_directory(self):
        with self.assertRaises(dw.WorkspaceError):
            self.ws.run_tests()

    def test_run_tests_takes_no_caller_supplied_command(self):
        import inspect
        self.assertEqual(list(inspect.signature(dw.Workspace.run_tests).parameters), ["self"])
        self.assertEqual(dw.TEST_COMMAND[1:3], ("-m", "unittest"))

    def test_timeout_kills_the_process_group(self):
        result = dw.run_bounded([sys.executable, "-c", "import time; time.sleep(30)"], cwd=self.root, timeout_s=1.0)
        self.assertTrue(result["timed_out"])
        self.assertIsNone(result["returncode"])
        self.assertLess(result["duration_s"], 10)

    def test_resource_limits_and_minimal_environment_are_applied(self):
        result = dw.run_bounded(
            [sys.executable, "-c", "import os,resource;print(resource.getrlimit(resource.RLIMIT_CPU)[0]);"
                              "print(sorted(k for k in os.environ if k not in {'PATH','LANG','PYTHONDONTWRITEBYTECODE','PYTHONNOUSERSITE','HOME','__CF_USER_TEXT_ENCODING'}))"],
            cwd=self.root, timeout_s=10.0)
        self.assertIn(str(dw.CPU_LIMIT_S), result["output"])
        # The child may see only variables from the minimal set (macOS may add its own platform variable).
        import ast
        seen = ast.literal_eval(result["output"].strip().splitlines()[-1])
        self.assertTrue(set(seen) <= {"__CF_USER_TEXT_ENCODING"}, seen)

    def test_commands_cannot_be_empty_or_contain_nul(self):
        with self.assertRaises(dw.WorkspaceError):
            dw.run_bounded([], cwd=self.root, timeout_s=1)
        with self.assertRaises(dw.WorkspaceError):
            dw.run_bounded([sys.executable, "x\x00y"], cwd=self.root, timeout_s=1)

    def test_unittest_summary_parsing(self):
        ok = dw.parse_unittest_summary("...\nRan 4 tests in 0.01s\n\nOK\n")
        self.assertEqual((ok["tests_ran"], ok["passed"]), (4, True))
        fail = dw.parse_unittest_summary("Ran 3 tests\n\nFAILED (failures=1, errors=2)\n")
        self.assertEqual((fail["failures"], fail["errors"], fail["passed"]), (1, 2, False))
        self.assertFalse(dw.parse_unittest_summary("nothing ran")["passed"])

    # --- audit ----------------------------------------------------------------------------------

    def test_audit_log_stores_hashes_not_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = dw.AuditLog(Path(tmp) / "audit.jsonl")
            log.record("write", path="a.py", sha256="ab" * 32, content="SECRET BODY", token="t0ken")
            line = json.loads((Path(tmp) / "audit.jsonl").read_text().strip())
            self.assertEqual(line["event"], "write")
            self.assertNotIn("content", line)
            self.assertNotIn("token", line)
            self.assertNotIn("SECRET", (Path(tmp) / "audit.jsonl").read_text())


if __name__ == "__main__":
    unittest.main()
