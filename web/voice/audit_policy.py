"""Read-only AST-based classifier regression. Never imports the runtime or runs shell.
Usage: python -B voice/audit_policy.py /absolute/path/to/tasks/supervisor.py
Exit 1 means at least one mutating command was classified approval-free.
"""
import ast
import json
from pathlib import Path
import shlex
import sys


def review(path):
    module = ast.parse(Path(path).read_text())
    classifier = next(node for node in module.body
                      if isinstance(node, ast.FunctionDef) and node.name == 'requires_approval')
    namespace = {'shlex': shlex, 'Path': Path, 'Any': object}
    exec(compile(ast.Module(body=[classifier], type_ignores=[]), str(path), 'exec'), namespace)
    cases = [
        ('disk mutation', 'diskutil eraseDisk APFS AUDIT /dev/audit-placeholder'),
        ('git mutation', 'git branch -D audit-placeholder'),
        ('substituted executable', '/tmp/audit-placeholder/df -h'),
    ]
    return [{'case': name, 'approval_required': bool(namespace['requires_approval'](
        'shell_exec', {'command': command}, True))} for name, command in cases]


if __name__ == '__main__':
    rows = review(sys.argv[1])
    print(json.dumps({'kind': 'classification only; no command execution', 'cases': rows}, indent=2))
    sys.exit(0 if all(row['approval_required'] for row in rows) else 1)
