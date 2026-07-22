#!/bin/bash
set -e

# Find uv executable
UV_CMD="uv"
if ! command -v uv &> /dev/null; then
    if [ -f "/Users/ycaffdevice/.local/bin/uv" ]; then
        UV_CMD="/Users/ycaffdevice/.local/bin/uv"
    else
        echo "Error: uv command not found."
        exit 1
    fi
fi

# 1. Create the mock platform directory and module
mkdir -p /Users/ycaffdevice/dev/agenticAi/.mock_platform
cat << 'EOF' > /Users/ycaffdevice/dev/agenticAi/.mock_platform/platform.py
import sys
import os
import importlib

# Remove mock directory from sys.path to prevent infinite recursion
mock_dir = os.path.dirname(__file__)
if mock_dir in sys.path:
    sys.path.remove(mock_dir)

# Import the real standard library platform module
mock_platform = sys.modules.pop('platform', None)
real_platform = importlib.import_module('platform')
if mock_platform is not None:
    sys.modules['platform'] = mock_platform

# Copy all attributes from real platform to globals
globals().update(real_platform.__dict__)

# Override mac_ver to return a valid mac version
def mac_ver(release='', versioninfo=('', '', ''), machine=''):
    return ('14.0', ('', '', ''), 'arm64')
EOF

# 2. Create the mock python wrapper
cat << 'EOF' > /Users/ycaffdevice/dev/agenticAi/.mock_platform/python3.12
#!/bin/bash
args=()
for arg in "$@"; do
    if [ "$arg" != "-I" ]; then
        args+=("$arg")
    fi
done
export PYTHONPATH="/Users/ycaffdevice/dev/agenticAi/.mock_platform"
exec /opt/homebrew/bin/python3.12 "${args[@]}"
EOF
chmod +x /Users/ycaffdevice/dev/agenticAi/.mock_platform/python3.12

# 3. Create the virtual environment using the mock wrapper
echo "==> Recreating virtual environment using Homebrew Python 3.12..."
$UV_CMD venv --python /Users/ycaffdevice/dev/agenticAi/.mock_platform/python3.12 --clear

# 4. Replace the venv symlinks with our wrapper script
echo "==> Installing wrapper scripts inside .venv..."
rm -f .venv/bin/python .venv/bin/python3 .venv/bin/python3.12
cat << 'EOF' > .venv/bin/python
#!/bin/bash
args=()
for arg in "$@"; do
    if [ "$arg" != "-I" ]; then
        args+=("$arg")
    fi
done
export PYTHONPATH="/Users/ycaffdevice/dev/agenticAi/.mock_platform"
exec /opt/homebrew/bin/python3.12 "${args[@]}"
EOF
chmod +x .venv/bin/python
ln -sf python .venv/bin/python3
ln -sf python .venv/bin/python3.12

# 5. Synchronize dependencies using uv sync
echo "==> Synchronizing dependencies..."
$UV_CMD sync

# 6. Register Jupyter Kernel
echo "==> Registering Jupyter kernel..."
./.venv/bin/python -m ipykernel install --user --name=agenticAi --display-name "Python (agenticAi .venv)"

echo "==> Virtual environment and kernel successfully recreated!"
