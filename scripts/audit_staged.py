"""Fail on credential-shaped strings in staged files without printing their values."""

import re
import subprocess

patterns = [
    rb"AIza[0-9A-Za-z_-]{30,}",
    rb"sk-(?:proj-)?[A-Za-z0-9_-]{30,}",
    rb"gh[pousr]_[A-Za-z0-9]{30,}",
    rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
    rb"postgres(?:ql)?(?:\+psycopg)?://[^\s:]+:(?!local-ci-only)[^\s@]{8,}@",
]
paths = subprocess.check_output(["git", "diff", "--cached", "--name-only", "-z"]).split(b"\0")
bad = []
for raw in filter(None, paths):
    path = raw.decode()
    data = subprocess.check_output(["git", "show", f":{path}"])
    if path == ".env" or any(re.search(pattern, data) for pattern in patterns):
        bad.append(path)
if bad:
    raise SystemExit("Credential audit failed in: " + ", ".join(bad))
print(f"Credential audit passed: {len(list(filter(None, paths)))} staged files.")
