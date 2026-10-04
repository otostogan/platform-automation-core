import re
import unittest
from pathlib import Path

MAIN = re.compile(r'^if __name__ == "__main__":\n    unittest\.main\(\)\n', re.M)


class TestFilesRunWholeWhenRunDirectlyTest(unittest.TestCase):
    def test_nothing_follows_the_main_block(self) -> None:
        """``unittest.main()`` exits; a class defined after it never runs.

        Discovery imports the module and finds every class, so CI stays
        green while ``python tests/test_x.py`` silently skips what was
        appended at the end of the file.
        """
        offenders = []
        for path in sorted(Path(__file__).parent.glob("test_*.py")):
            text = path.read_text(encoding="utf-8")
            match = MAIN.search(text)
            if match and text[match.end() :].strip():
                offenders.append(path.name)
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
