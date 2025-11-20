import re
import json
from pathlib import Path

log_path = Path("java.log")
if not log_path.exists():
    print("java.log not found")
    raise SystemExit(1)

log = log_path.read_text(encoding="utf-8", errors="replace")

# Try several markers we observed in the logs
patterns = [r"Java headings raw response: (\[.*?\])",
            r"Java headings for matching: (\[.*?\])",
            r"Java headings raw response: (\[.*\])",
            r"Java headings for matching: (\[.*\])"]
block = None
for pat in patterns:
    m = re.search(pat, log, flags=re.DOTALL)
    if m:
        block = m.group(1)
        break

if not block:
    print("No Java headings block found in java.log")
    raise SystemExit(1)

s = block

# Normalize Java map-style entries to JSON-like list of objects
# Replace title=VALUE with "title": "VALUE"
# Replace pageNumber=NUM with "pageNumber": NUM etc.

# Ensure braces are JSON-friendly
s = s.replace("\n", " ")
# Quote title values (heuristic): title=... (stops at , pageNumber or , level or })
s = re.sub(r"title=([^,\}]+)(?=,\s*pageNumber|,\s*level|\})",
           lambda mo: '"title": ' + json.dumps(mo.group(1).strip()), s)
# numbers
s = re.sub(r"pageNumber=([0-9]+)", r'"pageNumber": \1', s)
s = re.sub(r"level=([0-9]+)", r'"level": \1', s)
# Ensure keys are quoted
s = re.sub(r"\b(title|pageNumber|level)\s*:", r'"\1":', s)

# Try to parse
try:
    parsed = json.loads(s)
except Exception:
    # Fallback: extract each {...} entry and parse manually
    entries = re.findall(r"\{([^}]+)\}", s)
    parsed = []
    for ent in entries:
        obj = {}
        parts = re.split(r",\s*(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", ent)
        for part in parts:
            if ':' not in part:
                continue
            k, v = part.split(':', 1)
            k = k.strip().strip('"')
            v = v.strip()
            if v.startswith('"') and v.endswith('"'):
                v = v[1:-1]
            else:
                try:
                    v = int(v)
                except Exception:
                    v = v.strip('"')
            obj[k] = v
        if obj:
            parsed.append(obj)

if not parsed:
    print("Parsed 0 headings")
    raise SystemExit(0)

print(f"Found {len(parsed)} candidate headings; showing top 30:\n")
for i, item in enumerate(parsed[:30], 1):
    title = item.get("title") or item.get('title') or ''
    page = item.get("pageNumber") or item.get('pageNumber') or item.get('page')
    level = item.get("level")
    print(f"{i:2}. (p={page}, lvl={level}) {title}")
