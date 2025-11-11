# Local run instructions

This repository contains a Java backend (Spring Boot) and a Python FastAPI server that together process PDFs and extract a table of contents using image-based analysis.

Files added for local runs:
- `run_local.sh` - installer + runner script. Usage: `./run_local.sh "/path/to/Your PDF.pdf"`
- `.env.example` - copy to `.env` and fill `GEMINI_API_KEY` (and optionally `JAVA_HEADINGS_URL`).

Quick start
1. Copy and edit environment variables:

```bash
cp .env.example .env
# Edit .env and set GEMINI_API_KEY
```

2. Make the run script executable and run it with the PDF path:

```bash
chmod +x run_local.sh
./run_local.sh "/full/path/to/Your PDF.pdf"
```

What the script does
- Installs system packages (openjdk-17-jdk, maven, python3-venv, poppler-utils, tesseract).
- Sets up a Python virtual environment and installs Python requirements in `python-server`.
- Builds the Java app with Maven (produces a jar in `target/`).
- Starts the Java server on port 8080 and the Python server on port 8000 in the background.
- Calls `http://localhost:8000/process-pdf` with the provided PDF and saves the resulting JSON as `processed_YYYYMMDD_HHMMSS.json`.

Notes and troubleshooting
- `GEMINI_API_KEY` is required for the image-based TOC extraction. Without it the Python server will skip Gemini calls and return limited output.
- The Python code expects the Java headings endpoint at `JAVA_HEADINGS_URL` (default `http://localhost:8080/get/pdf-info/detect-chapter-headings`). You can change this in `.env`.
- If the Java app uses Spring Boot actuator health (default `/actuator/health`) the script will wait until it becomes healthy. If your Spring Boot app doesn’t expose actuator, you may see a timeout warning but the script will still attempt to continue.

Stopping servers
The script prints PIDs of the Java and Python processes it started. Stop them with:

```bash
kill <JAVA_PID> <PY_PID>
```

If you want the servers to run under systemd or as Docker containers for production, consider creating proper service units or Docker Compose files.
