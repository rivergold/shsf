"""Install locked embedding dependencies with CPU-only torch on a Linux server.

Run from any directory: python3 scripts/setup_cpu.py
Requires uv. Keeps uv.lock unchanged; standard uv sync --extra embedding will
replace CPU torch with the default CUDA distribution on Linux.
"""
from pathlib import Path
import re
import subprocess
import tempfile


def main():
    root = Path(__file__).resolve().parents[1]
    def run(*args, **kwargs):
        return subprocess.run(args, cwd=root, check=True, **kwargs)

    run('uv', 'sync', '--frozen', '--python', '3.12')
    locked = run('uv', 'export', '--frozen', '--extra', 'embedding',
                 '--no-hashes', '--no-emit-project', '--no-annotate',
                 '--no-header', '--format', 'requirements-txt',
                 capture_output=True, text=True).stdout
    torch = re.search(r'^torch==([^\s;]+)', locked, re.MULTILINE)
    if torch is None:
        raise RuntimeError('torch version missing from uv.lock export')
    requirements = '\n'.join(line for line in locked.splitlines()
                             if not line.startswith(('torch==', 'triton==', 'nvidia-', 'cuda-')))
    with tempfile.TemporaryDirectory(prefix='shsf-cpu-') as temporary:
        path = Path(temporary) / 'requirements.txt'
        path.write_text(requirements + '\n')
        run('uv', 'pip', 'install', '--python', '.venv/bin/python', '--no-deps', '-r', str(path))
    run('uv', 'pip', 'install', '--python', '.venv/bin/python',
        'torch==' + torch.group(1) + '+cpu', '--index-url', 'https://download.pytorch.org/whl/cpu')
    run('uv', 'pip', 'check', '--python', '.venv/bin/python')


if __name__ == '__main__':
    main()
