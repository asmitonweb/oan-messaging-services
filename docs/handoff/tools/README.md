# Regenerating the Word file

```
npm install docx@9.5.1                      # in a scratch folder
node build_docx.js <DEV-HANDOFF dir> <out.docx> contents.json
```

`contents.json` holds the contents-page entries and their page numbers. After editing,
render the file to PDF (`soffice --headless --convert-to pdf`), read the page on which
each chapter starts, update `contents.json`, and build again.
