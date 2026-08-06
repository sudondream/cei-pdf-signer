# Page Navigation and "Apply Signature to All Pages"

Date: 2026-08-06
Status: approved

## Goal

Two additions to CEI PDF Signer:

1. Page navigation: jump to a page number, go to first page, go to last page.
2. "Apply signature to all pages": replicate the signature box drawn on the current
   page onto every other page of the selected document.

## Decisions Taken

### One qualified signature, visual marks elsewhere

A PDF digital signature is one cryptographic object bound to one signature field with
one visible appearance on one page. "Signature on all pages" is therefore ambiguous.

**Chosen:** one real signature field on the page the user drew, and a visual stamp on
every other page. The stamps are page content, not signature fields.

Because stamping happens *before* signing, the single signature's byte range covers
every stamp — they cannot be altered without invalidating the signature. This mirrors
the paper convention of initialling every page while signing once.

Rejected: a real signature field per page. It would cost one PKCS#11 sign operation and
one incremental update per page (the user's documents are image-heavy — the 6-page file
already produces 10.7MB), show 12 signatures on a 12-page document in Adobe, and amount
to signing the same document 12 times, which a recipient may query.

### Placement: same spot, clamped inside the page

**Chosen:** use the exact coordinates of the drawn box. If any part would fall outside
the target page, slide it back inside keeping a 4pt margin.

On uniformly-sized documents (all three of the user's files are A4, 595.2 x 841.92) this
is a no-op and every stamp lands identically.

```
M = 4                                  # margin, PDF points
x = min(max(x, M), pageW - w - M)
y = min(max(y, M), pageH - h - M)
```

Rejected: proportional placement (can push a stamp further into the content area on
landscape pages than expected). Rejected: whitespace detection — it requires parsing
every page's content stream, there is no server-side renderer available, results vary
per document, and it is hard to test.

## Scope Assumptions

- "Apply to all pages" acts on the **currently selected document only**.
- The replicated stamp keeps the **same size** as the drawn box; it is not scaled per page.

## Design

### A. Page navigation (frontend only)

Toolbar becomes `⏮ ◀ Page [6] of 6 ▶ ⏭`, with the page number an editable input.

- `goToPage(n)` clamps to `1..totalPages`, ignores non-numeric input, re-renders.
- The input commits on Enter and on blur; invalid input reverts to the current page.
- `firstPage()` / `lastPage()`, disabled at the bounds like the existing prev/next buttons.

No backend involvement.

### B. Apply to all pages (frontend)

A toolbar button, enabled when the selected document has at least one box on the current
page. It replicates that page's box(es) to every other page, clamped per page.

Clamping needs each page's true size, so `pageSizes[fileIdx]` is cached on PDF load from
pdf.js (`getPage(i).getViewport({scale: 1})`). Clamping client-side as well as server-side
means the on-screen preview matches the output exactly — the user can page through and
see where each stamp will land before signing.

Signature boxes are already stored in PDF points with a top-left origin, which is what the
backend expects, so no coordinate-model change is needed.

### C. Backend (`app.py`)

Today `app.py` takes `signature_boxes[0]` and silently discards the rest, so boxes drawn
on other pages never appear. New contract:

- `signature_boxes[0]` → the real signature field (`Signature1`; existing behaviour).
- `signature_boxes[1:]` → visual stamps via `TextStamp(writer, style).apply(page_ix, x, y)`.

This fixes the existing silent-drop bug as a side effect.

Order of operations:

1. Build `IncrementalPdfFileWriter` from the reader.
2. `append_signature_field` for box 0.
3. Apply a `TextStamp` for each remaining box.
4. `pdf_signer.sign_pdf(writer)`.

Stamp text is built from the signing certificate's CN plus the timestamp so it reads the
same as the real signature appearance. The backend re-clamps against the true MediaBox
(via `get_page_media_box`, which already handles page-tree walking and MediaBox
inheritance); the frontend clamp exists for preview accuracy, the backend clamp is
authoritative.

### D. Error handling

- Signature box on a page beyond the document → HTTP 400 (already implemented).
- Box larger than the page → pinned at the margin and allowed to overflow, rather than
  silently resized.
- **Document already signed by someone else** → adding stamps modifies page content,
  which invalidates any pre-existing signature. Appending a signature field alone does
  not. Detect existing signature fields and refuse the all-pages stamp with a clear
  message, while still permitting a normal single signature.

### E. Testing

- `clamp_box()` as a pure function: portrait, landscape, oversized box, tiny page.
- N boxes produce exactly one signature field and N-1 stamps in the output.
- All-pages stamping over the synthetic nested-page-tree PDF from the page-tree fix.
- Refusal path when the source document already contains a signature.
- The existing 18 tests must continue to pass.
