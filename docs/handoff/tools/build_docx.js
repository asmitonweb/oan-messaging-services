// Build the OAN Messaging integration handoff (DOCX) from DEV-HANDOFF/*.md.
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, ImageRun, Table, TableRow, TableCell, WidthType,
  HeadingLevel, AlignmentType, BorderStyle, ShadingType, LevelFormat, TableOfContents,
  PageBreak, Footer, Header, PageNumber, TabStopType,
} = require("docx");

const SRC = process.argv[2];
const OUT = process.argv[3];
const ORDER = [
  "README.md", "01-architecture.md", "02-common-rules.md", "03-frappe-connector.md",
  "04-otp-senders.md", "10-credit-a2c.md", "11-dpi-consent.md", "12-grievance.md",
  "13-da-services.md", "14-auth.md", "15-esignet.md", "16-telegram.md",
  "20-testing-and-acceptance.md", "21-environments-and-onboarding.md",
  "22-decisions-and-open-items.md",
];
const FONT = "Arial";
const MONO = "Courier New";
const TEXT_W = 9638; // A4 (11906) minus 2 x 1134 margins
const BLUE = "1F4E79";

// ---------------------------------------------------------------- inline
function inlineRuns(text, base = {}) {
  const runs = [];
  // links -> text; keep anchors/paths out of the printed copy
  text = text.replace(/\[([^\]]+)\]\(([^)]+)\)/g, "$1");
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\*[^*\s][^*]*\*)/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) runs.push(new TextRun({ text: text.slice(last, m.index), font: FONT, ...base }));
    const t = m[0];
    if (t.startsWith("**")) runs.push(new TextRun({ text: t.slice(2, -2).replace(/`/g, ""), bold: true, font: FONT, ...base }));
    else if (t.startsWith("`")) runs.push(new TextRun({ text: t.slice(1, -1), font: MONO, size: (base.size || 20) - 2, color: "7A2E0E", ...base, ...{ font: MONO } }));
    else runs.push(new TextRun({ text: t.slice(1, -1), italics: true, font: FONT, ...base }));
    last = m.index + t.length;
  }
  if (last < text.length) runs.push(new TextRun({ text: text.slice(last), font: FONT, ...base }));
  return runs;
}

// ---------------------------------------------------------------- images
function pngSize(file) {
  const b = fs.readFileSync(file);
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20), data: b };
}
function imageParagraph(file) {
  const { w, h, data } = pngSize(file);
  const maxW = 640, maxH = 600; // px at 96 dpi; text width is ~642 px
  let width = maxW, height = Math.round((h * maxW) / w);
  if (height > maxH) { height = maxH; width = Math.round((w * maxH) / h); }
  return new Paragraph({
    alignment: AlignmentType.CENTER, spacing: { before: 120, after: 60 }, keepNext: true,
    children: [new ImageRun({ type: "png", data, transformation: { width, height },
      altText: { title: path.basename(file), description: path.basename(file), name: path.basename(file) } })],
  });
}

// ---------------------------------------------------------------- tables
const border = { style: BorderStyle.SINGLE, size: 4, color: "B7C3D0" };
const borders = { top: border, bottom: border, left: border, right: border };
function splitRow(line) {
  return line.trim().replace(/^\|/, "").replace(/\|$/, "").split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));
}
function table(rows) {
  const header = rows[0];
  const body = rows.slice(1);
  const n = header.length;
  // column widths from content length, clamped
  const lens = Array.from({ length: n }, (_, i) =>
    Math.max(...rows.map((r) => Math.min((r[i] || "").replace(/[`*]/g, "").length, 60)), 4));
  const total = lens.reduce((a, b) => a + b, 0);
  // never break an identifier: a column is at least as wide as its longest unbreakable token
  const minW = Array.from({ length: n }, (_, i) => Math.min(3200, Math.max(900, 110 * Math.max(
    ...rows.map((r) => Math.max(0, ...(r[i] || "").replace(/[`*]/g, "").split(/[\s/]+/).map((w) => w.length)))))));
  let widths = lens.map((l, i) => Math.max(minW[i], Math.round((l / total) * TEXT_W)));
  const s = widths.reduce((a, b) => a + b, 0);
  if (s > TEXT_W) {
    // shrink only the columns above their minimum
    const slack = widths.map((w, i) => w - minW[i]);
    const over = s - TEXT_W, totalSlack = slack.reduce((a, b) => a + b, 0) || 1;
    widths = widths.map((w, i) => Math.round(w - (slack[i] / totalSlack) * over));
  } else {
    widths = widths.map((w) => Math.round((w * TEXT_W) / s));
  }
  widths[n - 1] += TEXT_W - widths.reduce((a, b) => a + b, 0);
  const cell = (text, i, head) => new TableCell({
    borders, width: { size: widths[i], type: WidthType.DXA },
    shading: head ? { fill: "DCE6F1", type: ShadingType.CLEAR, color: "auto" } : undefined,
    margins: { top: 60, bottom: 60, left: 100, right: 100 },
    children: [new Paragraph({ spacing: { before: 0, after: 0 },
      children: inlineRuns(text || "", { size: 18, bold: head ? true : undefined }) })],
  });
  return new Table({
    width: { size: TEXT_W, type: WidthType.DXA }, columnWidths: widths,
    rows: [
      new TableRow({ tableHeader: true, children: header.map((t, i) => cell(t, i, true)) }),
      ...body.map((r) => new TableRow({ cantSplit: true, children: Array.from({ length: n }, (_, i) => cell(r[i], i, false)) })),
    ],
  });
}

// ---------------------------------------------------------------- blocks
let listInstance = 0;
function convert(md, dir, out) {
  const lines = md.split("\n");
  let i = 0, para = [];
  const flush = () => {
    if (para.length) {
      out.push(new Paragraph({ spacing: { after: 120 }, children: inlineRuns(para.join(" ")) }));
      para = [];
    }
  };
  while (i < lines.length) {
    const line = lines[i];
    const t = line.trim();
    if (t === "") { flush(); i++; continue; }
    if (t.startsWith("```")) {
      flush(); i++;
      const code = [];
      while (i < lines.length && !lines[i].trim().startsWith("```")) code.push(lines[i++]);
      i++;
      out.push(...code.map((c, k) => new Paragraph({
        shading: { fill: "F3F5F7", type: ShadingType.CLEAR, color: "auto" },
        spacing: { before: k === 0 ? 80 : 0, after: k === code.length - 1 ? 120 : 0 },
        indent: { left: 140, right: 140 },
        children: [new TextRun({ text: c.length ? c : " ", font: MONO, size: 16 })],
      })));
      continue;
    }
    const h = t.match(/^(#{1,4}) (.*)$/);
    if (h) {
      flush();
      const level = [HeadingLevel.HEADING_1, HeadingLevel.HEADING_2, HeadingLevel.HEADING_3, HeadingLevel.HEADING_4][h[1].length - 1];
      out.push(new Paragraph({ heading: level, children: inlineRuns(h[2]) }));
      i++; continue;
    }
    const img = t.match(/^!\[([^\]]*)\]\(([^)]+)\)$/);
    if (img) {
      flush();
      const file = path.join(dir, img[2]);
      if (fs.existsSync(file)) {
        out.push(imageParagraph(file));
        out.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 160 },
          children: [new TextRun({ text: `Figure: ${img[1]}`, italics: true, size: 18, font: FONT, color: "555555" })] }));
      }
      i++; continue;
    }
    if (/^\*Source: .*\*$/.test(t)) { i++; continue; } // source pointer: not useful in print
    if (t.startsWith("|")) {
      flush();
      const rows = [];
      while (i < lines.length && lines[i].trim().startsWith("|")) {
        const r = lines[i].trim();
        if (!/^\|[\s:|-]+\|$/.test(r)) rows.push(splitRow(r));
        i++;
      }
      out.push(table(rows));
      out.push(new Paragraph({ spacing: { after: 80 }, children: [] }));
      continue;
    }
    const bullet = line.match(/^(\s*)[*-] (.*)$/);
    const num = line.match(/^(\s*)\d+\. (.*)$/);
    if (bullet || num) {
      flush();
      const inst = ++listInstance;
      const items = []; // {level, numbered, text}
      const emit = () => {
        for (const it of items.splice(0)) {
          out.push(new Paragraph({
            numbering: it.numbered ? { reference: "numbers", level: it.level, instance: inst } : { reference: "bullets", level: it.level },
            spacing: { after: 60 }, children: inlineRuns(it.text),
          }));
        }
      };
      while (i < lines.length) {
        const l = lines[i];
        const b = l.match(/^(\s*)[*-] (.*)$/), nm = l.match(/^(\s*)\d+\. (.*)$/);
        if (b || nm) {
          const m = b || nm;
          items.push({ level: Math.min(Math.floor(m[1].length / 2), 2), numbered: !!nm, text: m[2] });
          i++; continue;
        }
        if (/^\s{2,}```/.test(l)) {                     // code block inside a list item
          emit(); i++;
          const code = [];
          while (i < lines.length && !lines[i].trim().startsWith("```")) code.push(lines[i++].replace(/^\s{2,3}/, ""));
          i++;
          out.push(...code.map((c, k) => new Paragraph({
            shading: { fill: "F3F5F7", type: ShadingType.CLEAR, color: "auto" },
            spacing: { before: k === 0 ? 60 : 0, after: k === code.length - 1 ? 100 : 0 },
            indent: { left: 720, right: 140 },
            children: [new TextRun({ text: c.length ? c : " ", font: MONO, size: 16 })] })));
          continue;
        }
        if (l.trim() === "" && i + 1 < lines.length && /^(\s+\S|\s*[*-] |\s*\d+\. )/.test(lines[i + 1]) && !/^\s*\|/.test(lines[i + 1])) {
          i++; continue;                                // blank line between items of the same list
        }
        if (l.trim() !== "" && /^\s{2,}\S/.test(l) && !l.trim().startsWith("|") && items.length) {
          items[items.length - 1].text += " " + l.trim(); // continuation of the previous item
          i++; continue;
        }
        break;
      }
      emit();
      continue;
    }
    if (t.startsWith("<")) { i++; continue; } // raw HTML anchors
    para.push(t);
    i++;
  }
  flush();
}

// ---------------------------------------------------------------- contents
function contentsTable() {
  const file = process.argv[4];
  const entries = file && fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : [];
  const rows = entries.length ? entries : ORDER.map((f) => [f, "-"]);
  return rows.map(([title, page]) => new Paragraph({
    spacing: { after: 80 },
    tabStops: [{ type: TabStopType.RIGHT, position: TEXT_W, leader: "dot" }],
    children: [new TextRun({ text: title, font: FONT, size: 22 }), new TextRun({ text: `\t${page}`, font: FONT, size: 22 })],
  }));
}

// ---------------------------------------------------------------- document
const body = [];
for (const [k, f] of ORDER.entries()) {
  if (k > 0) body.push(new Paragraph({ children: [new PageBreak()] }));
  let md = fs.readFileSync(path.join(SRC, f), "utf8");
  if (f === "README.md") md = md.replace(/^# .*\n/, "# 00 Overview and module index\n");
  convert(md, SRC, body);
}

const title = [
  new Paragraph({ spacing: { before: 2400, after: 240 }, children: [new TextRun({ text: "OAN Messaging Service", font: FONT, size: 56, bold: true, color: BLUE })] }),
  new Paragraph({ spacing: { after: 480 }, children: [new TextRun({ text: "Integration handoff for development teams", font: FONT, size: 36, color: "333333" })] }),
  new Paragraph({ border: { bottom: { style: BorderStyle.SINGLE, size: 12, color: BLUE, space: 4 } }, children: [] }),
  ...[
    ["Version", "15 November 2026"],
    ["Status", "Review (not deployed)"],
    ["Contract", "OAN Messaging external contract 3.0.0"],
    ["Audience", "Credit (A2C), DPI Consent Manager, Grievance, DA Services, Auth and eSignet teams; QA; DevOps"],
    ["Note", "All hosts are example.invalid placeholders and all phone numbers are 0900000xxx test numbers."],
  ].map(([k, v]) => new Paragraph({ spacing: { before: 160 }, children: [
    new TextRun({ text: `${k}: `, bold: true, font: FONT, size: 22 }), new TextRun({ text: v, font: FONT, size: 22 })] })),
  new Paragraph({ children: [new PageBreak()] }),
  new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun({ text: "Contents", font: FONT })] }),
  ...contentsTable(),
  new Paragraph({ children: [new PageBreak()] }),
];

const doc = new Document({
  creator: "OAN Messaging", lastModifiedBy: "OAN Messaging", title: process.env.TITLE || "OAN Messaging integration handoff",
  description: process.env.SUBTITLE || "Integration handoff for development teams",
  features: { updateFields: true },
  styles: {
    default: { document: { run: { font: FONT, size: 20 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 32, bold: true, color: BLUE }, paragraph: { spacing: { before: 120, after: 200 }, keepNext: true, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 26, bold: true, color: BLUE }, paragraph: { spacing: { before: 280, after: 120 }, keepNext: true, outlineLevel: 1 } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 22, bold: true, color: "333333" }, paragraph: { spacing: { before: 200, after: 80 }, keepNext: true, outlineLevel: 2 } },
      { id: "Heading4", name: "Heading 4", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { font: FONT, size: 20, bold: true, color: "333333" }, paragraph: { spacing: { before: 160, after: 60 }, keepNext: true, outlineLevel: 3 } },
    ],
  },
  numbering: { config: [
    { reference: "bullets", levels: [0, 1, 2].map((lvl) => ({ level: lvl, format: LevelFormat.BULLET,
      text: ["•", "–", "•"][lvl], alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 360 + 360 * lvl, hanging: 260 } } } })) },
    { reference: "numbers", levels: [0, 1, 2].map((lvl) => ({ level: lvl, format: lvl === 1 ? LevelFormat.LOWER_LETTER : LevelFormat.DECIMAL,
      text: `%${lvl + 1}.`, alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 360 + 360 * lvl, hanging: 300 } } } })) },
  ] },
  sections: [{
    properties: { page: { size: { width: 11906, height: 16838 }, margin: { top: 1134, bottom: 1134, left: 1134, right: 1134 } } },
    headers: { default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT,
      children: [new TextRun({ text: "OAN Messaging integration handoff (Review)", font: FONT, size: 16, color: "777777" })] })] }) },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER,
      children: [new TextRun({ children: ["Page ", PageNumber.CURRENT, " of ", PageNumber.TOTAL_PAGES], font: FONT, size: 16, color: "777777" })] })] }) },
    children: [...title, ...body],
  }],
});

Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(OUT, buf); console.log("wrote", OUT, buf.length, "bytes"); });
