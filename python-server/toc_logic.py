import sys
import os
import asyncio
import json
from pathlib import Path
from PIL import Image
from typing import Optional, List

# --- Library Check ---
try:
    import google.generativeai as genai
    from pydantic import BaseModel
    # pdf2image is optional when PyMuPDF fallback is available
    try:
        from pdf2image import convert_from_path
    except Exception:
        convert_from_path = None
    # Try optional PyMuPDF fallback
    try:
        import fitz  # PyMuPDF
    except Exception:
        fitz = None

    print("✅ Pre-flight check passed. Core libraries imported (pdf2image/fitz availability may vary).")
except ImportError as e:
    print(f"\n--- ❌ CRITICAL ERROR: A required library failed to import: {e} ---")
    sys.exit()

# --- API Configuration ---
# It's recommended to set this as an environment variable in production
API_KEY = os.environ.get("GEMINI_API_KEY", "") # Fallback to empty string if not set

if API_KEY:
    try:
        genai.configure(api_key=API_KEY)
        print("API Key configured successfully.")
    except Exception as e:
        print(f"An error occurred during API configuration: {e}")
        API_KEY = None
else:
    print("GEMINI_API_KEY environment variable not set.")

# --- Pydantic Data Models ---

# Updated TocEntry model without 'chapter_number'
class TocEntry(BaseModel):
    chapter_title: str
    page_number: int
    reference_boolean: bool

class BookMetadata(BaseModel):
    book_title: Optional[str]
    authors: Optional[List[str]]
    publishing_house: Optional[str]
    publishing_year: Optional[int]

class ExtractionResult(BaseModel):
    metadata: BookMetadata
    toc_entries: List[TocEntry]

# --- Core Logic ---

async def get_structured_data_from_images(model, image_paths: List[str]):
    """
    Analyzes a list of image paths using the provided Gemini model and returns structured
    JSON data containing metadata and TOC entries.
    """
    print(f"Processing a chunk of {len(image_paths)} images with model: {model.model_name}...")

    # Updated prompt without 'chapter_number'
    structured_prompt = """
Analyze the following book pages to extract metadata and the main table of contents.
Your response will be programmatically constrained to the JSON schema provided.

The JSON object you return has two top-level keys: "metadata" and "toc_entries".

1.  **"metadata"**: This object contains the book's metadata.
    * "book_title": The full title of the book.
    * "authors": A list of all author names.
    * "publishing_house": The name of the publisher.
    * "publishing_year": The integer year of publication.
    * If any metadata field is not found on the pages, its value MUST be null.

2.  **"toc_entries"**: This is a JSON array containing ONLY THE MAIN, TOP-LEVEL CHAPTERS.
    * **CRITICAL**: You MUST IGNORE indented sub-chapters. Main chapters are typically not indented and have larger page gaps between them. Do not include sub-chapters in the list.
    * Each object in the array represents one main chapter and MUST have these three keys:
        * "chapter_title": The string name of the chapter.
        * "page_number": The integer page number.
        * "reference_boolean": A boolean value. It MUST be `true` ONLY for sections explicitly titled "Bibliography" or "References". For all other entries (including "Index", "Appendix", "Coda", etc.), it MUST be `false`.

If no table of contents entries are found, "toc_entries" MUST be an empty list [].

IMPORTANT: Return ONLY valid JSON. Do NOT include any markdown, explanations, or extra text. The output must be a single valid JSON object and nothing else.
"""

    prompt_parts = [structured_prompt]
    for path in image_paths:
        prompt_parts.append(Image.open(path))

    generation_config = genai.GenerationConfig(
        response_mime_type="application/json",
        response_schema=ExtractionResult
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            # Using asyncio.to_thread for the blocking SDK call
            response = await asyncio.to_thread(
                model.generate_content,
                contents=prompt_parts,
                generation_config=generation_config
            )
            return response.text
        except Exception as e:
            error_str = str(e)
            print(f"API call attempt {attempt + 1} failed: {error_str}")
            if "Deadline Exceeded" in error_str or "503" in error_str:
                if attempt + 1 < max_retries:
                    await asyncio.sleep(2 ** attempt) # Exponential backoff
                else:
                    return '{"error": "API call failed after multiple retries"}'
            else:
                return f'{{"error": "API call failed", "details": "{error_str}"}}'
    return '{"error": "API call failed after all retries"}'

async def process_pdf(pdf_path: str):
    """
    Extracts TOC and metadata from a PDF using a two-pass approach.
    Pass 1 (Discovery): Uses a fast model to find pages containing the TOC.
    Pass 2 (Verification): Uses a powerful model on only the identified pages for accurate extraction.
    """
    if not API_KEY:
        print("Cannot proceed without a valid API Key.")
        return None

    print("\nStep 1: Converting first 20 PDF pages to JPEG images...")
    output_dir = Path("pages")
    output_dir.mkdir(exist_ok=True)
    # Clean up old images before conversion
    for old_image in output_dir.glob("*.jpg"):
        try:
            old_image.unlink()
        except Exception:
            pass

    # Helper: try pdf2image (pdftoppm) first, fallback to PyMuPDF (fitz)
    def _render_with_pymupdf(pdf_path_local: str, out_dir: Path, max_pages: int = 20):
        if fitz is None:
            raise RuntimeError("PyMuPDF (fitz) is not available for fallback rendering")
        doc = fitz.open(pdf_path_local)
        rendered = []
        for page_index in range(min(max_pages, doc.page_count)):
            page = doc.load_page(page_index)
            pix = page.get_pixmap(dpi=150)
            out_path = out_dir / f"page_{page_index+1:03d}.jpg"
            pix.save(str(out_path))
            rendered.append(str(out_path))
        return rendered

    image_paths = []
    # Prefer pdf2image if convert_from_path is available and pdftoppm is on PATH
    import shutil
    use_pdf2image = convert_from_path is not None and shutil.which("pdftoppm")
    try:
        if use_pdf2image:
            images = convert_from_path(pdf_path, last_page=20, fmt='jpeg', output_folder=output_dir, output_file="page_")
            image_paths = sorted([str(p) for p in output_dir.glob("*.jpg")])
            print(f"Successfully converted {len(image_paths)} pages using pdf2image/pdftoppm.")
        else:
            # Attempt PyMuPDF fallback
            print("pdftoppm not available: attempting PyMuPDF fallback (if installed)...")
            image_paths = _render_with_pymupdf(pdf_path, output_dir, max_pages=20)
            print(f"Successfully rendered {len(image_paths)} pages using PyMuPDF fallback.")
    except Exception as e:
        print(f"[ERROR] Failed to convert/render PDF pages to images: {e}")
        if not use_pdf2image:
            print("[HINT] Install PyMuPDF with: python -m pip install pymupdf or install Poppler and ensure pdftoppm is on PATH.")
        else:
            print("[HINT] Is poppler installed and its `bin` folder added to PATH? See https://github.com/Belval/pdf2image#installing-poppler-on-windows")
        return None

    # --- Pass 1: Discovery Pass with Flash Model ---
    print("\n--- Starting Pass 1: Discovery (using gemini-2.5-flash) ---")
    model_flash = genai.GenerativeModel(model_name="gemini-2.5-flash")
    chunk_size = 5
    discovery_tasks = []
    for i in range(0, len(image_paths), chunk_size):
        chunk_paths = image_paths[i:i + chunk_size]
        discovery_tasks.append(get_structured_data_from_images(model_flash, chunk_paths))
    discovery_results = await asyncio.gather(*discovery_tasks)

    toc_page_indices = set()
    all_parsed_results_pass1 = []
    for i, res_str in enumerate(discovery_results):
        try:
            res_json = json.loads(res_str)
            all_parsed_results_pass1.append(res_json)
            if res_json.get("toc_entries"):
                start_index = i * chunk_size
                end_index = start_index + chunk_size
                # Add all page indices from this successful chunk
                for page_idx in range(start_index, min(end_index, len(image_paths))):
                    toc_page_indices.add(page_idx)
        except (json.JSONDecodeError, TypeError):
            print(f"Warning: Could not parse JSON from discovery chunk {i+1}.")
            continue

    if not toc_page_indices:
        print("\n--- Discovery Pass found no pages with TOC entries. Aborting. ---")
        return None

    print(f"\n--- Discovery Pass identified {len(toc_page_indices)} potential TOC pages. ---")

    # --- Pass 2: Verification Pass with Pro Model ---
    print("\n--- Starting Pass 2: Verification (using gemini-2.5-pro) ---")
    model_pro = genai.GenerativeModel(model_name="gemini-2.5-pro")
    # Create a list of image paths from the discovered indices
    targeted_image_paths = [image_paths[i] for i in sorted(list(toc_page_indices))]
    final_result_str = await get_structured_data_from_images(model_pro, targeted_image_paths)

    try:
        final_data = json.loads(final_result_str)
    except (json.JSONDecodeError, TypeError):
        print("\n--- ❌ FINAL RESULT ---")
        print("ERROR: Failed to parse the final JSON output from the Pro model.")
        return None

    # --- Final Consolidation ---
    print("\n--- Consolidating final results ---")

    # Although Pass 2 gives the definitive TOC, we can still pick the best metadata
    # from the broader scan in Pass 1 for robustness.
    best_metadata = {}
    max_filled_fields = -1
    for result in all_parsed_results_pass1:
        metadata = result.get("metadata", {})
        if metadata:
            filled_count = sum(1 for value in metadata.values() if value is not None)
            if filled_count > max_filled_fields:
                max_filled_fields = filled_count
                best_metadata = metadata

    # Get the high-quality TOC from the Verification Pass
    final_combined_toc = final_data.get("toc_entries", [])

    # Relaxed: Accept all entries from LLM output, no deduplication or filtering
    final_combined_toc.sort(key=lambda item: item.get('page_number', 0))

    final_result_obj = {
        "metadata": best_metadata,
        "toc_entries": final_combined_toc
    }

    print("\n\n--- ✅ SUCCESS: COMBINED & PROCESSED FINAL DATA ---")
    print(json.dumps(final_result_obj, indent=2))

    return final_result_obj

## This main function is for standalone testing of this script.
## In the FastAPI app, you will import and call `process_pdf` directly.
async def main():
    if len(sys.argv) < 2:
        print("Usage: python toc_logic.py <path_to_pdf>")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    if not os.path.exists(pdf_path):
        print(f"Error: File not found at {pdf_path}")
        sys.exit(1)
    
    await process_pdf(pdf_path)

if __name__ == "__main__":
    asyncio.run(main())