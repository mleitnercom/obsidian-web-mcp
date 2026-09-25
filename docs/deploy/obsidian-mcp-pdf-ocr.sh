#!/usr/bin/env bash
# OCR for image-only PDFs: render pages with pdftoppm, read them with tesseract,
# print the text to stdout. Wired into the server as VAULT_PDF_OCR_CMD.
#
# Why not ocrmypdf: it produces a searchable PDF, which is not what the server wants
# (it wants text on stdout), and it drags in Ghostscript, qpdf and a pikepdf/Python
# stack. This box has 5.9 GB and already pages under the nightly reindex. pdftoppm plus
# tesseract is the same OCR engine with two packages instead of a dependency tree.
#
# Only reached when pypdf extracted no text at all, i.e. genuinely scanned documents.
# The result is cached as a .ocr.txt sidecar, so a given PDF is read once.
set -uo pipefail

PDF="${1:-${VAULT_PDF_PATH:-}}"
LANGS="${VAULT_PDF_OCR_LANGUAGES:-deu+eng}"
DPI="${VAULT_PDF_OCR_DPI:-200}"
MAX_PAGES="${VAULT_PDF_OCR_MAX_PAGES:-40}"

if [ -z "$PDF" ] || [ ! -f "$PDF" ]; then
  echo "pdf-ocr: no readable input file: ${PDF:-<empty>}" >&2
  exit 2
fi

for tool in pdftoppm tesseract pdfinfo; do
  command -v "$tool" >/dev/null 2>&1 || { echo "pdf-ocr: missing $tool" >&2; exit 3; }
done

# One OpenMP thread per tesseract. Its multithreading buys little on page-sized images
# and hurts badly when two OCR runs meet: on 2026-09-02 a three-page document that takes
# 24s alone hit the 300s timeout while the warm-up batch was running, because both
# instances oversubscribed the four cores. Serial per process, parallel across processes.
export OMP_THREAD_LIMIT=1

WORK="$(mktemp -d -t obsidian-pdf-ocr-XXXXXX)" || exit 4
trap 'rm -rf "$WORK"' EXIT

total=$(pdfinfo "$PDF" 2>/dev/null | awk '/^Pages:/{print $2}')
[ -n "${total:-}" ] || total=1

# VAULT_PDF_OCR_PAGES (set by the server for a PDF that has text on some pages): only
# these pages, comma-separated and 1-based. Without it, the whole document.
if [ -n "${VAULT_PDF_OCR_PAGES:-}" ]; then
  pages=$(printf '%s\n' "$VAULT_PDF_OCR_PAGES" | tr ',' '\n' | grep -E '^[0-9]+$' | awk -v t="$total" '$1 >= 1 && $1 <= t')
else
  pages=$(seq 1 "$total")
fi
pages=$(printf '%s\n' "$pages" | head -n "$MAX_PAGES")

# One page at a time on purpose. Rendering a whole document at 200 DPI would hold every
# page bitmap at once; page by page keeps peak memory at roughly one page, which matters
# more here than the few extra PDF parses it costs.
#
# With a page list, every page handled is labelled: a form feed and "PAGE <n>" on its
# own line, then the text (none for a blank page). A page that could not be rendered or
# read is labelled "PAGE <n> FAILED", so the server does not cache it as blank. The
# server matches text to pages by these labels only, never by position (tesseract 5.3
# prints no page separator of its own). Without a page list the output is unlabelled,
# as before.
label() { [ -n "${VAULT_PDF_OCR_PAGES:-}" ] && printf '\fPAGE %s%s\n' "$1" "$2"; return 0; }
for page in $pages; do
  if ! pdftoppm -f "$page" -l "$page" -r "$DPI" -png -singlefile "$PDF" "$WORK/page" 2>/dev/null; then
    echo "pdf-ocr: render failed on page $page" >&2
    label "$page" " FAILED"
    continue
  fi
  if text=$(tesseract "$WORK/page.png" - -l "$LANGS" -c page_separator= 2>/dev/null); then
    label "$page" ""
    [ -n "$text" ] && printf '%s\n' "$text"
  else
    echo "pdf-ocr: tesseract failed on page $page" >&2
    label "$page" " FAILED"
  fi
  rm -f "$WORK/page.png"
done
