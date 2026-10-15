// Saves a travel plan as a clean A4 PDF. Used by the solo and group pages.
//
//   TripBuddyPdf.save({ source: element, title: "...", subtitle: "...", filename: "x.pdf" })
//
// html2pdf turns the page into a picture and cuts it into A4-sized slices, which on
// its own cuts table rows and lines of text in half. So before handing it over, this:
//   1. copies the plan into a white "sheet" exactly as wide as an A4 page's printable
//      area, with print-friendly styles (tables shrink to fit instead of being clipped);
//   2. walks through paragraphs, list items, headings and table rows, and pushes any
//      that would be cut onto the next page (repeating the table's header row there),
//      keeping each heading together with the start of what follows it;
//   3. draws list bullets and numbers as text (html2canvas mangles real list markers),
//      renders without the scroll offset that left a blank band on page 1, and adds
//      "Page X of Y" footers.

(function () {
  const PAGE = { widthMm: 210, heightMm: 297, marginMm: 12 };
  const MM_TO_PX = 96 / 25.4;
  const SAFETY_PX = 6; // breathing room so nothing sits right on a page edge

  const innerWidthMm = PAGE.widthMm - 2 * PAGE.marginMm;
  const innerHeightMm = PAGE.heightMm - 2 * PAGE.marginMm;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text) node.textContent = text;
    return node;
  }

  // html2canvas draws list bullets and numbers unreliably (e.g. "1." comes out as "1"),
  // so the copy gets them as ordinary text instead.
  function addListMarkers(body) {
    for (const list of body.querySelectorAll("ul, ol")) {
      const ordered = list.tagName === "OL";
      const nested = Boolean(list.parentElement.closest("li"));
      let number = ordered ? parseInt(list.getAttribute("start") || "1", 10) : 0;
      for (const item of list.children) {
        if (item.tagName !== "LI") continue;
        const text = ordered ? `${number++}.` : nested ? "◦" : "•";
        item.insertBefore(el("span", ordered ? "pdf-marker number" : "pdf-marker", text), item.firstChild);
      }
    }
  }

  function buildSheet(source, title, subtitle) {
    const host = el("div", "pdf-host");
    const sheet = el("div", "pdf-sheet");
    sheet.style.width = `${innerWidthMm}mm`;
    const header = el("header", "pdf-header");
    header.append(el("div", "pdf-brand", "TripBuddy"), el("h1", "pdf-doc-title", title));
    if (subtitle) header.appendChild(el("p", "pdf-doc-subtitle", subtitle));
    const body = el("div", "pdf-body");
    body.innerHTML = source.innerHTML;
    addListMarkers(body);
    sheet.append(header, body);
    host.appendChild(sheet);
    document.body.appendChild(host);
    return { host, sheet };
  }

  function topOf(node, sheet) {
    return node.getBoundingClientRect().top - sheet.getBoundingClientRect().top;
  }

  function heightOf(node) {
    return node.getBoundingClientRect().height;
  }

  // How much of the next block must sit on the same page as a heading.
  function leadHeight(heading) {
    const next = heading.nextElementSibling;
    if (!next) return 0;
    if (next.tagName === "TABLE") {
      const rows = next.querySelectorAll("tr");
      const upTo = rows[Math.min(1, rows.length - 1)];
      return upTo ? upTo.getBoundingClientRect().bottom - next.getBoundingClientRect().top : 0;
    }
    if (next.tagName === "UL" || next.tagName === "OL") {
      const first = next.querySelector("li");
      return first ? heightOf(first) : 0;
    }
    return Math.min(heightOf(next), 80);
  }

  // Insert a spacer before `block` so it starts at the top of the next page.
  // Returns [spacer element whose height can be adjusted, first node now on the new page].
  function insertSpacer(block, gap) {
    if (block.tagName !== "TR") {
      const spacer = el("div", "pdf-spacer");
      spacer.style.height = `${gap}px`;
      block.parentNode.insertBefore(spacer, block);
      return [spacer, block];
    }
    const table = block.closest("table");
    const columns = Math.max(1, ...Array.from(table.rows).map((r) => r.cells.length));
    const spacerRow = el("tr", "pdf-spacer");
    const cell = el("td");
    cell.colSpan = columns;
    cell.style.height = `${gap}px`;
    spacerRow.appendChild(cell);
    block.parentNode.insertBefore(spacerRow, block);
    let first = block;
    const headerRow = table.tHead ? table.tHead.rows[0] : null;
    if (headerRow) {
      // Repeat the table's header row at the top of the new page
      first = headerRow.cloneNode(true);
      first.classList.add("pdf-repeat-header");
      block.parentNode.insertBefore(first, block);
    }
    return [cell, first];
  }

  function paginate(sheet, pageHeight) {
    const blocks = sheet.querySelectorAll(
      ".pdf-body h1, .pdf-body h2, .pdf-body h3, .pdf-body h4, .pdf-body p, .pdf-body li," +
      " .pdf-body tbody tr, .pdf-body blockquote, .pdf-body pre, .pdf-body hr"
    );
    for (const block of blocks) {
      // Paragraphs inside list items or table cells move with their container
      if (block.tagName === "P" && block.parentElement.closest("li, td, th")) continue;
      // A list item holding a nested list can be long; its nested items are handled one by one
      if (block.tagName === "LI" && block.querySelector("ul, ol")) continue;

      const top = topOf(block, sheet);
      const height = heightOf(block);
      if (height === 0 || height > pageHeight * 0.85) continue; // a giant block can't avoid a cut
      const pageEnd = (Math.floor(top / pageHeight) + 1) * pageHeight;
      const isHeading = /^H[1-4]$/.test(block.tagName);
      const needed = height + (isHeading ? leadHeight(block) : 0);
      if (top + needed <= pageEnd - SAFETY_PX) continue; // fits on this page

      const target = pageEnd + SAFETY_PX;
      const gap = target - top;
      const [spacer, first] = insertSpacer(block, gap);
      // Margins and table borders shift things by a few pixels; correct the spacer once
      const corrected = gap - (topOf(first, sheet) - target);
      spacer.style.height = `${Math.max(0, corrected)}px`;
    }
  }

  function addFooters(pdf, label) {
    const total = pdf.internal.getNumberOfPages();
    const width = pdf.internal.pageSize.getWidth();
    const height = pdf.internal.pageSize.getHeight();
    for (let i = 1; i <= total; i += 1) {
      pdf.setPage(i);
      pdf.setFontSize(8);
      pdf.setTextColor(120);
      pdf.text(label, PAGE.marginMm, height - 5);
      pdf.text(`Page ${i} of ${total}`, width - PAGE.marginMm, height - 5, { align: "right" });
    }
  }

  async function save({ source, title, subtitle, filename }) {
    if (typeof html2pdf === "undefined") {
      throw new Error("The PDF library didn't load. Check your internet connection and reload.");
    }
    const { host, sheet } = buildSheet(source, title, subtitle);
    try {
      const sheetWidthPx = sheet.getBoundingClientRect().width;
      const pageHeight = sheetWidthPx * (innerHeightMm / innerWidthMm);
      paginate(sheet, pageHeight);

      await html2pdf()
        .set({
          margin: PAGE.marginMm,
          filename,
          image: { type: "jpeg", quality: 0.92 },
          // scrollX/scrollY 0: without them a scrolled page leaves a blank band at the top
          html2canvas: { scale: 2, useCORS: true, backgroundColor: "#ffffff", scrollX: 0, scrollY: 0 },
          jsPDF: { unit: "mm", format: "a4", orientation: "portrait" },
          pagebreak: { mode: ["css"] }, // breaks were already placed above
        })
        .from(sheet)
        .toPdf()
        .get("pdf")
        .then((pdf) => addFooters(pdf, "TripBuddy"))
        .save();
    } finally {
      host.remove();
    }
  }

  window.TripBuddyPdf = { save };
})();
