
# --- BEGIN: Integrated image-based TOC extraction logic ---
import re
import os
import requests
import tempfile
import json
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

# Load environment variables from a .env file if present
load_dotenv()
import sys
try:
    # Ensure stdout/stderr use UTF-8 on Windows consoles to avoid UnicodeEncodeError
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
# Remove PyPDF2 import, not needed for new workflow

# Import the new TOC extraction logic
import toc_logic

app = FastAPI()

# Read Gemini API key from env var, fallback to empty string if not set
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Allow overriding the Java headings service URL via env var for local runs
JAVA_HEADINGS_URL = os.environ.get("JAVA_HEADINGS_URL", "http://localhost:8080/get/pdf-info/detect-chapter-headings")

# This is a fallback parser if Gemini returns markdown instead of JSON
def parse_chapter_list(text_response):
    pattern = r"\*\s*Chapter\s*(\d+):\s*(.*?):\s*(\d+)"
    chapters = []
    for match in re.finditer(pattern, text_response):
        chapters.append({
            "chapter_number": int(match.group(1)),
            "chapter_title": match.group(2).strip(),
            "page_number": int(match.group(3))
        })
    return chapters

async def get_toc_from_new_logic(pdf_path: str):
    """
    Wrapper function to call the new image-based TOC extraction logic.
    """
    print("[DEBUG] Starting new image-based TOC extraction from toc_logic.py")
    if not GEMINI_API_KEY:
        print("[DEBUG] GEMINI_API_KEY not set, skipping new TOC logic.")
        return []
    try:
        # Call the async process_pdf function from the new module
        result_json = await toc_logic.process_pdf(pdf_path)
        if result_json and "toc_entries" in result_json:
            print("[DEBUG] Successfully extracted TOC using new image-based logic.")
            return result_json
        else:
            print("[DEBUG] New TOC extraction logic returned no entries.")
            return None
    except Exception as e:
        print(f"[DEBUG] An error occurred while running the new TOC logic: {e}")
        return None


def get_java_headings(pdf_path):
    url = JAVA_HEADINGS_URL
    with open(pdf_path, "rb") as f:
        files = {"file": f}
        try:
            response = requests.post(url, files=files, timeout=180)
            print("[DEBUG] Java headings API status:", response.status_code)
            if response.status_code == 200:
                headings_data = response.json()
                print("[DEBUG] Java headings raw response:", headings_data)
                if isinstance(headings_data, dict) and "headings" in headings_data:
                    return headings_data["headings"]
                return headings_data
        except Exception as e:
            print("[DEBUG] Java headings API exception:", e)
            return {"error": str(e)}
    return []


def match_toc_with_java_headings_gemini(toc, java_headings, book_title):
    url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key=" + GEMINI_API_KEY

    # --- IMPORTANT CHANGE ---
    # Reformat the TOC to remove page numbers and other extra fields
    # before sending it to the final matching prompt.
    print("[DEBUG] Raw TOC passed to final matching step:", toc)
    formatted_toc_for_prompt = [
        {
            "chapter_title": entry.get("chapter_title"),
            "chapter_number": entry.get("chapter_number")
        }
        for entry in toc
    ]
    print("[DEBUG] Formatted TOC for final prompt (should NOT include page_number):", formatted_toc_for_prompt)

    prompt = (
        f"You are an expert data-cleaning and text-matching AI. Your task is to create a final, accurate Table of Contents (TOC) for the book '{book_title}'.\n\n"
        "You will be given two lists:\n"
        "1.  **[TOC LIST]**: The definitive, 100% correct list of chapter titles.\n"
        "2.  **[JAVA HEADINGS LIST]**: A very noisy and unreliable list of text fragments and their page numbers extracted from the book. This list contains many errors, random words, and chapter titles that are split across multiple lines.\n"
        "\nYour mission is to use the noisy [JAVA HEADINGS LIST] ONLY to find the correct starting page number for each real chapter in the [TOC LIST].\n"
        "-----\n"
        "### CRITICAL RULES FOR SUCCESS:\n"
        "**1. Aggressively Ignore Noise:** The [JAVA HEADINGS LIST] is messy. You MUST completely ignore entries that are clearly not chapter titles. These include:\n"
        "* **Single, common words:** Ignore entries like 'the', 'past', 'of', 'a', etc.\n"
        "* **Symbols and Junk:** Ignore entries that are just symbols, punctuation, or malformed text (e.g., '*', '/', '[', ']').\n"
        "* **Generic Capitalized Words:** Ignore standalone, capitalized words that are unlikely to be full chapter titles (e.g., 'LEVEL', 'FUTURE').\n"
        "**2. Reconstruct Fragmented Titles:** This is the most important challenge. A chapter title like 'LSD PSYCHOTHERAPY' might be split in the noisy data like this:\n"
        "{ 'title': 'LSD', 'pageNumber': 262 }\n"
        "{ 'title': 'PSYCHOTHERAPY', 'pageNumber': 262 }\n"
        "* **Your Strategy:** You must look for **consecutive entries** in the [JAVA HEADINGS LIST] that appear on the **same page number**.\n"
        "* When you find such a sequence, combine their 'title' fields. If the combined text matches a chapter from the [TOC LIST], you have found a match.\n"
        "* The correct page number for the chapter is the page number of the **first** entry in that sequence.\n"
        "**3. Use Logical Reasoning to Resolve Ambiguity:**\n"
        "* **Chronological Order is Mandatory:** Chapter page numbers MUST increase sequentially. Chapter 5 cannot start on a page that comes after Chapter 6. Use this to eliminate impossible matches.\n"
        "* **Plausible Chapter Length:** If you are unsure between two possible page numbers for a chapter, consider the page numbers of the chapters before and after it. If one choice makes the chapter only one or two pages long while all other chapters are 20 pages long, it is almost certainly the wrong choice. Select the page number that results in a more logical and balanced book structure.\n"
        "**4. Be Flexible with Minor Differences:** A chapter in the [TOC LIST] might be 'The Coming Storm', while the data has 'COMING STORM'. This is a valid match. Ignore differences in capitalization and minor words like 'The', 'A', or 'An'.\n"
        "**5. Extra hints:  'level': 1: This means the heading's font size is 2 points or more larger than the average. This is a strong signal that the text is a primary heading, like a chapter title. The 'level': 0: This means the font size is less than 2 points larger than the average, so it's less likely to be a header. Best to see if you can match everything with level 1, and only then start looking at level 0 if needed.\n"
        "-----\n"
        "### YOUR INPUTS:\n"
        "**[TOC LIST]**\n"
        + json.dumps(formatted_toc_for_prompt, indent=2) +
        "\n**[JAVA HEADINGS LIST]**\n"
        + json.dumps(java_headings, indent=2) +
        "\n-----\n"
        "### YOUR TASK:\n"
        "Now, analyze the two lists according to the critical rules above.\n"
        "Your output response should be ALWAYS return in JSON format, never anything else. Return only valid JSON in your reply with no Markdown, code blocks, comments, or explanations; the response must be a single JSON object that exactly matches the required keys and structure I provide, with no extra characters or formatting, and if you cannot comply output an empty JSON object {} instead.\n"
        "JSON format: \n[\n    {\"title\": \"LEVEL\",\"pageNumber\": 253,\"level\": 1},\n    {\"title\": \"the\",\"pageNumber\": 256,\"level\": 1},\n    {\"title\": \"Edgar\",\"pageNumber\": 256,\"level\": 1},\n    {\"title\": \"past\",\"pageNumber\": 256,\"level\": 1},\n    {\"title\": \"FUTURE\",\"pageNumber\": 262,\"level\": 1},\n    {\"title\": \"*\",\"pageNumber\": 262,\"level\": 1},\n    {\"title\": \"LSD\",\"pageNumber\": 262,\"level\": 1},\n    {\"title\": \"PSYCHOTHERAPY\",\"pageNumber\": 262,\"level\": 1}\n]"
    )
    headers = {"Content-Type": "application/json"}
    data = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        response = requests.post(url, headers=headers, json=data)
        print("[DEBUG] Gemini match API status:", response.status_code)
        if response.status_code == 200:
            result = response.json()
            print("[DEBUG] Gemini match raw response:", result)
            candidates = result.get("candidates", [])
            if candidates:
                text_response = candidates[0]["content"]["parts"][0]["text"]
                # Strip triple backticks and 'json' if present
                cleaned = text_response.strip()
                if cleaned.startswith('```json'):
                    cleaned = cleaned[len('```json'):].strip()
                if cleaned.startswith('```'):
                    cleaned = cleaned[len('```'):].strip()
                if cleaned.endswith('```'):
                    cleaned = cleaned[:-3].strip()
                try:
                    final_chapters = json.loads(cleaned)
                    if isinstance(final_chapters, list) and final_chapters:
                        return final_chapters
                except Exception:
                    print("[DEBUG] Gemini match response not valid JSON:", cleaned)
                    # Try fallback parsing
                    final_chapters = parse_chapter_list(cleaned)
                    if final_chapters:
                        print("[DEBUG] Parsed chapter list from markdown format.")
                        return final_chapters
                # Fallback: return original TOC if Gemini output is empty or invalid
                print("[DEBUG] Gemini output empty or invalid, returning original TOC.")
                return toc
        return []
    except Exception as e:
        print("[DEBUG] Gemini match API exception:", e)
        return []


@app.post("/extract-toc")
async def extract_toc_endpoint(file: UploadFile = File(...)):
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name
        # Call the new TOC extraction logic
        result = await get_toc_from_new_logic(tmp_path)
        toc = result["toc_entries"] if result and "toc_entries" in result else []
        # Only include chapter_title and reference_boolean for each entry
        filtered_toc = [
            {
                "chapter_title": entry.get("chapter_title"),
                "reference_boolean": entry.get("reference_boolean")
            }
            for entry in toc
        ]
        return JSONResponse(content={"toc": filtered_toc})
    except Exception as e:
        return JSONResponse(content={"error": str(e)})
    finally:
        # Clean up the temporary file
        if 'tmp_path' in locals() and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@app.post("/match-toc-java")
async def match_toc_java_endpoint(
    file: UploadFile = File(...)
):
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name
        # Call the new TOC extraction logic
        result = await get_toc_from_new_logic(tmp_path)
        toc = result["toc_entries"] if result and "toc_entries" in result else []
        metadata = result["metadata"] if result and "metadata" in result else {}
        book_title = metadata.get("book_title") or "Unknown Title"
        authors = metadata.get("authors") or ["Unknown Author"]
        java_headings = get_java_headings(tmp_path)
        print("[DEBUG] Java headings for matching:", java_headings)
        final_chapters = match_toc_with_java_headings_gemini(toc, java_headings, book_title) if GEMINI_API_KEY else []
        final_json = {
            "book_title": book_title,
            "authors": authors,
            "toc": final_chapters
        }
        print("[DEBUG] Final API response:", final_json)
        return JSONResponse(content=final_json)
    except Exception as e:
        return JSONResponse(content={"error": str(e)})
    finally:
        if 'tmp_path' in locals() and os.path.exists(tmp_path):
            os.unlink(tmp_path)


@app.post("/process-pdf")
async def process_pdf(
    file: UploadFile = File(...)
):
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(await file.read())
            tmp_path = tmp.name
        # Call the new TOC extraction logic
        result = await get_toc_from_new_logic(tmp_path)
        toc = result["toc_entries"] if result and "toc_entries" in result else []
        metadata = result["metadata"] if result and "metadata" in result else {}
        book_title = metadata.get("book_title") or "Unknown Title"
        authors = metadata.get("authors") or ["Unknown Author"]
        java_headings = get_java_headings(tmp_path)
        print("[DEBUG] Java headings for matching:", java_headings)
        final_chapters = match_toc_with_java_headings_gemini(toc, java_headings, book_title) if GEMINI_API_KEY else []
        final_json = {
            "book_title": book_title,
            "authors": authors,
            "toc": final_chapters
        }
        print("[DEBUG] Final API response:", final_json)
        return JSONResponse(content=final_json)
    except Exception as e:
        return JSONResponse(content={"error": str(e)})
    finally:
        if 'tmp_path' in locals() and os.path.exists(tmp_path):
            os.unlink(tmp_path)