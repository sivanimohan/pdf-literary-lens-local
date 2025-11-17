#!/usr/bin/env bash
set -euo pipefail

# Usage: ./run_local.sh "/path/to/Your PDF.pdf"

SKIP_JAVA_BUILD=${SKIP_JAVA_BUILD:-0}
if [ "${1:-}" = "--skip-java" ]; then
  SKIP_JAVA_BUILD=1
  shift
fi

PDF_PATH="$1"

if [ -z "${PDF_PATH}" ]; then
  echo "Usage: $0 \"/path/to/Your PDF.pdf\""
  exit 1
fi

# If not root, try to use sudo for apt commands
APT_CMD="apt-get"
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
  if command -v sudo >/dev/null 2>&1; then
    SUDO=sudo
    APT_CMD="sudo apt-get"
  else
    echo "This script needs to install system packages. Run as root or install sudo." >&2
    exit 1
  fi
fi

echo "Installing system packages (openjdk-17-jdk, maven, python3-venv, poppler-utils, tesseract)..."
${SUDO} ${APT_CMD} update
${SUDO} ${APT_CMD} install -y openjdk-17-jdk maven python3-venv python3-pip poppler-utils tesseract-ocr tesseract-ocr-eng

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
## If a .env file exists at repo root, load it and export variables (so GEMINI_API_KEY and JAVA_HEADINGS_URL are available)
if [ -f "${ROOT_DIR}/.env" ]; then
  echo "Loading environment variables from ${ROOT_DIR}/.env"
  # shellcheck disable=SC1090
  set -a
  # Use a subshell to source the file safely
  . "${ROOT_DIR}/.env"
  set +a
fi

# Print confirmation that critical env vars were loaded (do not print secrets)
if [ -n "${GEMINI_API_KEY:-}" ]; then
  echo "GEMINI_API_KEY present: yes"
else
  echo "GEMINI_API_KEY present: no"
fi
if [ -n "${JAVA_HEADINGS_URL:-}" ]; then
  echo "JAVA_HEADINGS_URL: ${JAVA_HEADINGS_URL}"
else
  echo "JAVA_HEADINGS_URL: not set"
fi

echo "Setting up Python virtual environment..."
cd "${ROOT_DIR}/python-server"
PY_VENV_DIR=".venv"
if [ ! -d "${PY_VENV_DIR}" ]; then
  python3 -m venv "${PY_VENV_DIR}"
fi
source "${PY_VENV_DIR}/bin/activate"
pip install --upgrade pip
pip install -r requirements.txt

echo "Building Java application with Maven..."
cd "${ROOT_DIR}"

# Ensure a JDK 17 is active. If current `java` is older, try to locate an installed JDK17 and switch to it.
JAVA_MAJOR=$(java -version 2>&1 | awk -F[\"._] 'NR==1{print $2}') || true
JDK17_DIR=""
if [ -z "${JAVA_MAJOR}" ] || [ "${JAVA_MAJOR}" -lt 17 ]; then
  echo "Current java major version is ${JAVA_MAJOR:-unknown} (<17). Attempting to locate JDK 17 on the system..."
  # Try common installation paths
  JDK17_DIR=""
  for candidate in /usr/lib/jvm/*jdk*17* /usr/lib/jvm/*-17* /usr/lib/jvm/*temurin-17*; do
    if [ -x "${candidate}/bin/java" ] 2>/dev/null; then
      JDK17_DIR="${candidate}"
      break
    fi
  done

  if [ -z "${JDK17_DIR}" ]; then
    echo "No JDK 17 found in /usr/lib/jvm. Attempting to install openjdk-17-jdk via apt..."
    ${SUDO} ${APT_CMD} update
    ${SUDO} ${APT_CMD} install -y openjdk-17-jdk
    for candidate in /usr/lib/jvm/*jdk*17* /usr/lib/jvm/*-17* /usr/lib/jvm/*temurin-17*; do
      if [ -x "${candidate}/bin/java" ] 2>/dev/null; then
        JDK17_DIR="${candidate}"
        break
      fi
    done
  fi

  if [ -n "${JDK17_DIR}" ]; then
    echo "Found JDK17 at ${JDK17_DIR}. Registering with update-alternatives and switching system java/javac..."
    ${SUDO} update-alternatives --install /usr/bin/java java "${JDK17_DIR}/bin/java" 2 || true
    ${SUDO} update-alternatives --install /usr/bin/javac javac "${JDK17_DIR}/bin/javac" 2 || true
    ${SUDO} update-alternatives --set java "${JDK17_DIR}/bin/java" || true
    ${SUDO} update-alternatives --set javac "${JDK17_DIR}/bin/javac" || true
    # Re-evaluate
    JAVA_MAJOR=$(java -version 2>&1 | awk -F[\"._] 'NR==1{print $2}') || true
    echo "After switching, java major version is ${JAVA_MAJOR}"
  else
    echo "Failed to locate or install JDK 17. Will attempt to build but may fail." >&2
  fi
    # Ensure this script and child processes use the found JDK17 by setting JAVA_HOME and PATH
    export JAVA_HOME="${JDK17_DIR}"
    export PATH="${JAVA_HOME}/bin:${PATH}"
fi

# If we found JDK17_DIR, prefer it; otherwise if current java is 17+ we can build too
if [ -n "${JDK17_DIR}" ]; then
  echo "Building Java application with Maven using JDK at ${JDK17_DIR}..."
  export JAVA_HOME="${JDK17_DIR}"
  export PATH="${JAVA_HOME}/bin:${PATH}"
  mvn -DskipTests clean package
else
  # Re-evaluate java major and if it's >=17 use it; else abort
  JAVA_MAJOR=$(java -version 2>&1 | awk -F[\"._] 'NR==1{print $2}') || true
  if [ -n "${JAVA_MAJOR}" ] && [ "${JAVA_MAJOR}" -ge 17 ]; then
    echo "Building Java application with Maven (system java ${JAVA_MAJOR})..."
    mvn -DskipTests clean package
  else
    echo "Java 17 not available; cannot build local Java server. Aborting local all-in-one run." >&2
    exit 1
  fi
fi

if [ "${SKIP_JAVA_BUILD}" -eq 0 ]; then
  JAR_FILE=$(ls target/*.jar 2>/dev/null | head -n 1 || true)
  if [ -z "${JAR_FILE}" ]; then
    echo "Could not find built jar in target/. Make sure Maven build succeeded." >&2
    exit 1
  fi

  echo "Starting Java server (background): ${JAR_FILE}"
  nohup java -jar "${JAR_FILE}" > java.log 2>&1 &
  JAVA_PID=$!
  echo "Java PID: ${JAVA_PID} (logs: ${ROOT_DIR}/java.log)"
else
  echo "Skipping local Java server run (using remote Java headings service)."
  JAVA_PID=0
fi

echo "Starting Python FastAPI server (background)"
cd "${ROOT_DIR}/python-server"
## Ensure any previous python server is stopped so new env vars take effect
pkill -f 'uvicorn' || true
nohup ${PY_VENV_DIR}/bin/uvicorn main:app --host 0.0.0.0 --port 8000 > python.log 2>&1 &
PY_PID=$!
echo "Python PID: ${PY_PID} (logs: ${ROOT_DIR}/python-server/python.log)"

# Cleanup trap to stop background servers on script exit
trap 'echo "Stopping background servers..."; [ -n "${JAVA_PID:-}" ] && kill ${JAVA_PID} 2>/dev/null || true; [ -n "${PY_PID:-}" ] && kill ${PY_PID} 2>/dev/null || true' EXIT

# Wait for services to be ready
echo "Waiting for Java (port 8080) and Python (port 8000) to be available..."
retry=0
max_retries=30
until curl -sSf http://localhost:8080/actuator/health >/dev/null 2>&1 || [ $retry -ge $max_retries ]; do
  sleep 1
  retry=$((retry+1))
done
if [ $retry -ge $max_retries ]; then
  echo "Warning: Java server may not have a /actuator/health endpoint or failed to start within timeout. Check ${ROOT_DIR}/java.log" >&2
fi

retry=0
until curl -sSf http://localhost:8000/docs >/dev/null 2>&1 || [ $retry -ge $max_retries ]; do
  sleep 1
  retry=$((retry+1))
done
if [ $retry -ge $max_retries ]; then
  echo "Warning: Python server /docs not reachable within timeout. Check ${ROOT_DIR}/python-server/python.log" >&2
fi

PDF_BASENAME=$(basename "${PDF_PATH}")
PDF_NAME_NOEXT="${PDF_BASENAME%.*}"
SAFE_NAME="${PDF_NAME_NOEXT// /_}"
OUT_FILE="${ROOT_DIR}/${SAFE_NAME}.json"
echo "Sending PDF to local Python /process-pdf endpoint and saving to ${OUT_FILE}..."
curl -s -o "${OUT_FILE}" -F "file=@${PDF_PATH}" http://localhost:8000/process-pdf

if [ $? -eq 0 ]; then
  echo "Saved JSON to ${OUT_FILE}"
else
  echo "Request failed. Inspect logs: ${ROOT_DIR}/python-server/python.log and ${ROOT_DIR}/java.log" >&2
fi

echo "Servers running. Java PID=${JAVA_PID}, Python PID=${PY_PID}. To stop them: kill ${JAVA_PID} ${PY_PID}"
