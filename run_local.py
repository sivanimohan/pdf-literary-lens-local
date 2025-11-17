#!/usr/bin/env python3
"""
Python replacement for `run_local.sh`.
- Loads environment variables from `.env` (if present)
- Optionally installs system packages on Linux (best-effort)
- Creates/activates a Python venv in `python-server/.venv` and installs requirements
- Optionally builds and starts the Java server with Maven (unless --skip-java)
- Starts the Python FastAPI server using the venv's Python/uvicorn
- Waits for services to become ready and POSTs the provided PDF to `/process-pdf`

Usage:
    python run_local.py [--skip-java] "/path/to/Your PDF.pdf"

Notes:
- This script tries to be cross-platform but system package installation and JDK
  selection are only supported on Linux with apt/update-alternatives.
- It will attempt to stop prior uvicorn processes on POSIX systems before starting
  a new Python server (uses `pkill -f uvicorn` when available).
"""

import argparse
import atexit
import glob
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

try:
    import requests
except Exception:
    requests = None

ROOT = Path(__file__).resolve().parent
PY_SERVER_DIR = ROOT / "python-server"
VENV_DIR = PY_SERVER_DIR / ".venv"
ENV_FILE = ROOT / ".env"
JAVA_LOG = ROOT / "java.log"
PY_LOG = PY_SERVER_DIR / "python.log"

processes = {}


def load_env(path: Path):
    """Load simple KEY=VALUE pairs from a .env file into os.environ."""
    if not path.exists():
        return 0
    with path.open() as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            if "=" not in ln:
                continue
            k, v = ln.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"')
            os.environ.setdefault(k, v)
    return 1


def run_cmd(cmd, cwd=None, check=True, capture_output=False, env=None):
    """Run a command and return CompletedProcess. Prints the command."""
    print("\n$", " ".join(cmd))
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=capture_output, env=env)


def try_system_installs():
    """Attempt to install system packages on Linux (best-effort).
    Only runs on Debian/Ubuntu-like systems with apt. This is optional.
    """
    if platform.system() != "Linux":
        print("System package install skipped: non-Linux platform")
        return
    apt = shutil.which("apt-get") or shutil.which("apt")
    if not apt:
        print("No apt package manager found; skipping system installs")
        return
    sudo = []
    if os.geteuid() != 0 and shutil.which("sudo"):
        sudo = ["sudo"]
    pkgs = [
        "openjdk-17-jdk",
        "maven",
        "python3-venv",
        "python3-pip",
        "poppler-utils",
        "tesseract-ocr",
        "tesseract-ocr-eng",
    ]
    try:
        run_cmd(sudo + [apt, "update"])
        run_cmd(sudo + [apt, "install", "-y"] + pkgs)
    except Exception as e:
        print("System install attempted but failed:", e)


def create_venv_and_install_requirements():
    """Ensure venv exists and install Python requirements into it."""
    if not VENV_DIR.exists():
        print("Creating virtual environment at:", VENV_DIR)
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
    py_exec = VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    pip_exec = VENV_DIR / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
    if not py_exec.exists():
        raise RuntimeError("Failed to create virtualenv python at {}".format(py_exec))
    # Upgrade pip and install requirements
    run_cmd([str(pip_exec), "install", "--upgrade", "pip"])  # no check to fail hard
    reqs = PY_SERVER_DIR / "requirements.txt"
    if reqs.exists():
        run_cmd([str(pip_exec), "install", "-r", str(reqs)])
    else:
        print("No requirements.txt found at", reqs)
    return str(py_exec)


def find_jdk17():
    """Try to find a JDK 17 installation on common paths or via java -version."""
    # First try java -version parsing
    java = shutil.which("java")
    if java:
        try:
            out = subprocess.run([java, "-version"], capture_output=True, text=True)
            ver = out.stderr.splitlines()[0] if out.stderr else out.stdout.splitlines()[0]
            # Example: 'openjdk version "17.0.1"'
            if "17" in ver:
                return java  # java executable is sufficient
        except Exception:
            pass
    # Try common JDK install dirs
    candidates = [
        "/usr/lib/jvm/java-17-openjdk-amd64",
        "/usr/lib/jvm/java-17-openjdk",
        "/usr/lib/jvm/temurin-17-jdk",
    ]
    for c in candidates:
        p = Path(c) / "bin" / "java"
        if p.exists():
            return str(p)
    return None


def build_java_if_needed(skip_java: bool):
    """Build the Java application with Maven unless skipped."""
    if skip_java:
        print("Skipping local Java build (flag set)")
        return None
    mvn = shutil.which("mvn")
    if not mvn:
        print("Maven not found on PATH; cannot build Java locally")
        return None
    jdk = find_jdk17()
    if not jdk:
        print("JDK 17 not found automatically; proceeding but build may fail")
    # Run mvn -DskipTests clean package
    run_cmd([mvn, "-DskipTests", "clean", "package"], cwd=ROOT)
    # Find jar
    jars = glob.glob(str(ROOT / "target" / "*.jar"))
    # Prefer the repackaged spring-boot jar (not .original)
    jar = None
    for j in jars:
        if j.endswith(".jar") and not j.endswith(".jar.original"):
            jar = j
            break
    if not jar and jars:
        jar = jars[0]
    if jar:
        print("Built jar:", jar)
    else:
        print("No jar found in target/")
    return jar


def start_java_server(jar_path: str):
    if not jar_path:
        return None
    f = open(JAVA_LOG, "a")
    print("Starting Java server (background):", jar_path)
    p = subprocess.Popen(["java", "-jar", jar_path], stdout=f, stderr=subprocess.STDOUT)
    processes['java'] = (p, f)
    print("Java PID:", p.pid)
    return p


def stop_java_server():
    val = processes.get('java')
    if not val:
        return
    p, f = val
    try:
        p.terminate()
        p.wait(timeout=5)
    except Exception:
        p.kill()
    finally:
        try:
            f.close()
        except Exception:
            pass


def start_python_server(venv_python: str):
    # Try to kill existing uvicorn on POSIX
    if os.name != 'nt' and shutil.which('pkill'):
        try:
            subprocess.run(['pkill', '-f', 'uvicorn'], check=False)
        except Exception:
            pass
    # Start uvicorn using venv python
    py = venv_python
    cmd = [py, "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
    f = open(PY_LOG, "a")
    p = subprocess.Popen(cmd, cwd=str(PY_SERVER_DIR), stdout=f, stderr=subprocess.STDOUT, env=os.environ.copy())
    processes['python'] = (p, f)
    print("Python PID:", p.pid)
    return p


def stop_python_server():
    val = processes.get('python')
    if not val:
        return
    p, f = val
    try:
        p.terminate()
        p.wait(timeout=5)
    except Exception:
        p.kill()
    finally:
        try:
            f.close()
        except Exception:
            pass


def wait_for_url(url, timeout=30):
    if requests is None:
        print("requests library not available; cannot perform HTTP readiness checks")
        return False
    start = time.time()
    while time.time() - start < timeout:
        try:
            r = requests.get(url, timeout=3)
            if r.status_code in (200, 302, 301, 401):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False


def post_pdf_and_save(pdf_path: Path, out_file: Path):
    if requests is None:
        print("requests not installed in this Python. Attempting to use curl subprocess as fallback.")
        curl = shutil.which('curl')
        if not curl:
            raise RuntimeError('requests not available and curl not found; cannot post PDF')
        subprocess.check_call([curl, '-s', '-o', str(out_file), '-F', f'file=@{str(pdf_path)}', 'http://localhost:8000/process-pdf'])
        return
    with pdf_path.open('rb') as fh:
        files = {'file': fh}
        r = requests.post('http://localhost:8000/process-pdf', files=files, timeout=300)
        try:
            r.raise_for_status()
        except Exception:
            print('Request failed:', r.status_code, r.text[:1000])
            raise
        out_file.write_text(r.text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-java', action='store_true', help='Skip building/starting local Java server')
    parser.add_argument('pdf', help='Path to PDF file')
    args = parser.parse_args()

    skip_java = args.skip_java
    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        print('PDF not found at', pdf_path)
        sys.exit(1)

    # Load .env early
    if ENV_FILE.exists():
        print('Loading environment variables from', ENV_FILE)
        load_env(ENV_FILE)
    else:
        print('.env not found at', ENV_FILE)

    # Print masked confirmations
    print('GEMINI_API_KEY present:', 'yes' if os.environ.get('GEMINI_API_KEY') else 'no')
    print('JAVA_HEADINGS_URL:', os.environ.get('JAVA_HEADINGS_URL', 'not set'))

    # Try system installs if on linux
    try_system_installs()

    # Create venv and install requirements
    venv_python = create_venv_and_install_requirements()

    # Build Java if needed
    jar = None
    if not skip_java:
        jar = build_java_if_needed(skip_java=skip_java)

    # Start Java server if jar present
    java_proc = None
    if jar and not skip_java:
        java_proc = start_java_server(jar)

    # Ensure cleanup
    atexit.register(lambda: (stop_python_server(), stop_java_server()))

    # Start Python server
    py_proc = start_python_server(venv_python)

    # Wait for readiness
    print('Waiting for Java (port 8080) and Python (port 8000) to be available...')
    java_ok = True
    if jar and not skip_java:
        java_ok = wait_for_url('http://localhost:8080/actuator/health', timeout=30)
        if not java_ok:
            print('Warning: Java server may not have /actuator/health or failed to start in time')
    py_ok = wait_for_url('http://localhost:8000/docs', timeout=30)
    if not py_ok:
        print('Warning: Python server /docs not reachable within timeout. Check python-server/python.log')

    # Post PDF
    out_file = ROOT / (pdf_path.stem.replace(' ', '_') + '.json')
    print('Sending PDF to local Python /process-pdf endpoint and saving to', out_file)
    try:
        post_pdf_and_save(pdf_path, out_file)
        print('Saved JSON to', out_file)
    except Exception as e:
        print('Request failed. Inspect logs: python-server/python.log and java.log')
        print('Error:', e)

    print('Servers running. Java PID={0}, Python PID={1}. To stop them: kill {0} {1}'.format(
        processes.get('java', (None,))[0].pid if processes.get('java') else 0,
        processes.get('python', (None,))[0].pid if processes.get('python') else 0
    ))


if __name__ == '__main__':
    main()
