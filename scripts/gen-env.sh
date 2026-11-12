#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_EXAMPLE="$REPO_ROOT/.env.example"

OUTPUT_PATH="$REPO_ROOT/.env"
FORCE="false"

usage() {
    cat <<'EOF'
Usage: scripts/gen-env.sh [OPTIONS]

Generate a local .env configuration from .env.example with fresh secrets.

Options:
  --output PATH    Output path for the generated file (default: ./.env at repo root)
  --force          Overwrite destination file if it already exists
  -h, --help       Show this help message and exit
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --output)
            if [[ $# -lt 2 ]]; then
                echo "Error: --output requires a file path argument." >&2
                exit 1
            fi
            OUTPUT_PATH="$2"
            shift 2
            ;;
        --force)
            FORCE="true"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Error: Unknown argument: $1" >&2
            usage >&2
            exit 1
            ;;
    esac
done

if ! command -v uv >/dev/null 2>&1; then
    cat >&2 <<'EOT'
Error: 'uv' is not installed or not in PATH.

Docker-only fallback. Run this command; it prints three lines for your .env plus the admin password:

docker run --rm -i python:3.14-slim sh -c 'pip install -q --disable-pip-version-check argon2-cffi >/dev/null 2>&1; exec python -' <<'PY'
import secrets
from argon2 import PasswordHasher
pw = secrets.token_urlsafe(16)
print("POSTGRES_PASSWORD=" + secrets.token_hex(16))
print("JWT_SECRET=" + secrets.token_hex(32))
print("ADMIN_PASSWORD_HASH='" + PasswordHasher().hash(pw) + "'")
print("# admin password (save it now): " + pw)
PY

Then copy .env.example to .env and replace its POSTGRES_PASSWORD, JWT_SECRET and ADMIN_PASSWORD_HASH lines with the first three output lines. Keep the hash single-quoted.
EOT
    exit 1
fi

if [[ ! -f "$ENV_EXAMPLE" ]]; then
    echo "Error: Template file '$ENV_EXAMPLE' not found." >&2
    exit 1
fi

export GEN_ENV_TEMPLATE="$ENV_EXAMPLE"
export GEN_ENV_OUTPUT="$OUTPUT_PATH"
export GEN_ENV_FORCE="$FORCE"

uv run --no-project --with argon2-cffi python - <<'PY'
import os
import pathlib
import re
import secrets
import sys
from argon2 import PasswordHasher

template_path = os.environ["GEN_ENV_TEMPLATE"]
output_path = os.environ["GEN_ENV_OUTPUT"]
force = os.environ.get("GEN_ENV_FORCE") == "true"

if os.path.exists(output_path) and not force:
    sys.stderr.write(f"Error: Output file '{output_path}' already exists. Use --force to overwrite.\n")
    sys.exit(1)

template_text = pathlib.Path(template_path).read_text(encoding="utf-8")
lines = template_text.splitlines(keepends=True)

found_keys = set()
for line in lines:
    m = re.match(r"^([A-Z_]+)=", line)
    if m:
        found_keys.add(m.group(1))

required = {"POSTGRES_PASSWORD", "JWT_SECRET", "ADMIN_PASSWORD_HASH"}
if not required.issubset(found_keys):
    missing = required - found_keys
    sys.stderr.write(f"Error: Template is missing required keys: {sorted(missing)}\n")
    sys.exit(1)

postgres_password = secrets.token_hex(16)
jwt_secret = secrets.token_hex(32)
admin_password = secrets.token_urlsafe(16)

hasher = PasswordHasher()
admin_password_hash = hasher.hash(admin_password)

new_lines = []
for line in lines:
    if re.match(r"^POSTGRES_PASSWORD=", line):
        new_lines.append(f"POSTGRES_PASSWORD={postgres_password}\n")
    elif re.match(r"^JWT_SECRET=", line):
        new_lines.append(f"JWT_SECRET={jwt_secret}\n")
    elif re.match(r"^ADMIN_PASSWORD_HASH=", line):
        new_lines.append(f"ADMIN_PASSWORD_HASH='{admin_password_hash}'\n")
    else:
        new_lines.append(line)

new_content = "".join(new_lines)

flags = os.O_WRONLY | os.O_CREAT | (os.O_TRUNC if force else os.O_EXCL)
try:
    fd = os.open(output_path, flags, 0o600)
except OSError as e:
    sys.stderr.write(f"Error: cannot write '{output_path}': {e.strerror}\n")
    sys.exit(1)

os.fchmod(fd, 0o600)
with open(fd, "w", encoding="utf-8") as f:
    f.write(new_content)

admin_user_m = re.search(r"^ADMIN_USERNAME=(.+)$", template_text, re.M)
admin_user = admin_user_m.group(1).strip() if admin_user_m else "admin"

print(f"Admin username: {admin_user}")
print(f"Admin password: {admin_password}")
PY
