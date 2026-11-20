# Regenerating the Word file

```
npm install docx@9.5.1                      # in a scratch folder, next to build_docx.js
export ORDER=00-about.md,01-scope.md,02-architecture.md,03-sms-service.md,04-sms-callers.md,05-telegram-platform.md,06-telegram-modules.md,07-operations-and-testing.md,08-build-order-and-decisions.md,A1-fallback.md
export KEEP_H1=1
export TITLE="OAN Channels Specification" SUBTITLE="SMS and Telegram: version 2.0"
export CONTRACT="OAN Messaging contract 3.1.0; RapidPro courier v26.3.82"
export AUDIENCE="ATI, module teams, OAN Messaging and Telegram bot developers, DevOps, security"
export HEADER="OAN Channels Specification v2.0 (Review)"
export NOTE="Hosts are example.invalid placeholders except 8028's published API host and the Master Data development host; phone numbers are 0900000xxx test numbers."
node build_docx.js <CHANNELS-SPEC dir> <out.docx> contents.json
```

`contents.json` holds the contents-page entries and their page numbers. After editing,
render the file to PDF (`soffice --headless --convert-to pdf`), read the page on which
each chapter starts, update `contents.json`, and build again.

Diagrams are Mermaid sources (`diagrams/*.mmd`) rendered to PNG with
`mermaid-config.json`.
