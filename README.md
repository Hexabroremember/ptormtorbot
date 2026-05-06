# Form PDF Name Editor

A small local web UI for replacing the printed name, ID number, and
expiration date in `FormPDFPreview (1) (1).pdf`.

The PDF does not contain fillable form fields, so the app uses PyMuPDF to
redact the printed regions in `assets/template.pdf` and draw the new values
with a bundled Arimo Bold font (and a rendered image for Hebrew), matching the
original layout.

## Run

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`, enter the fields, optionally enable **Watermark**
to overlay `watermark.png` on top of the page, then use Preview PDF or
Download PDF.

## Watermark

Check **Overlay watermark** in the form to draw `watermark.png` **above** all
other page content (full page, preserves aspect ratio). Put the file at
`assets/watermark.png`, or place `watermark.png` in the project root.

## Files

- `assets/template.pdf` is the stable template copied from the original PDF.
- `fonts/Arimo-Bold.ttf` is used to draw the replacement text.
- `assets/watermark.png` (or root `watermark.png`) is optional; used when the
  watermark checkbox is enabled.
