// Renders one Markdown doc -> branded A4 PDF with mermaid diagrams + a cover page.
// Usage: node render.mjs <input.md> <output.pdf> <metaJson>
import { readFileSync, writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { marked } from "marked";
import puppeteer from "puppeteer";

const HERE = dirname(fileURLToPath(import.meta.url));
const MERMAID_JS = readFileSync(resolve(HERE, "node_modules/mermaid/dist/mermaid.min.js"), "utf8");

const [, , inPath, outPath, metaJson] = process.argv;
const meta = JSON.parse(metaJson || "{}");
const md = readFileSync(inPath, "utf8");

// Extract mermaid fences before markdown parse.
const diagrams = [];
const mdClean = md.replace(/```mermaid\n([\s\S]*?)```/g, (_, code) => {
  const i = diagrams.length; diagrams.push(code.trim());
  return `<div class="mermaid" data-i="${i}">${code.trim().replace(/</g, "&lt;")}</div>`;
});
const body = marked.parse(mdClean, { gfm: true });

const audienceColor = { Leadership: "#7a3b9a", Architecture: "#0b4f8a", Component: "#0d7a5f", Operations: "#9a5a00", Security: "#9a1f3a", Reference: "#3a4a5a" }[meta.audience] || "#0b4f8a";
const cover = `
<section class="cover">
  <div class="cover-band" style="background:${audienceColor}"></div>
  <div class="cover-body">
    <div class="brand">AAKAAR</div>
    <div class="doc-code">${meta.code || ""}</div>
    <h1 class="cover-title">${meta.title || ""}</h1>
    <div class="cover-sub">${meta.subtitle || ""}</div>
    <div class="cover-meta">
      <span class="pill" style="background:${audienceColor}">${meta.audience || "Documentation"}</span>
      <table class="cover-tbl">
        <tr><td>Audience</td><td>${meta.readers || "—"}</td></tr>
        <tr><td>Version</td><td>${meta.version || "1.0"}</td></tr>
        <tr><td>Status</td><td>${meta.status || "Released"}</td></tr>
        <tr><td>Classification</td><td>Confidential</td></tr>
      </table>
    </div>
  </div>
  <div class="cover-foot">Aakaar Agentic Automation Platform — Documentation Suite</div>
</section>
<div class="page-break"></div>`;

const html = `<!doctype html><html><head><meta charset="utf8"><style>
  body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1c2733;line-height:1.55;font-size:11.5px;margin:0}
  .cover{height:267mm;position:relative;page-break-after:always}
  .cover-band{position:absolute;top:0;left:0;right:0;height:70mm}
  .cover-body{position:absolute;top:78mm;left:16mm;right:16mm}
  .brand{color:#fff;position:absolute;top:-58mm;left:0;font-size:30px;font-weight:800;letter-spacing:5px}
  .doc-code{position:absolute;top:-44mm;left:1px;color:#dfeaf5;font-size:12px;letter-spacing:3px}
  .cover-title{font-size:34px;color:#10202f;margin:0 0 6px;border:none;line-height:1.15}
  .cover-sub{font-size:15px;color:#5a6b7a;margin-bottom:24px}
  .pill{color:#fff;padding:4px 12px;border-radius:14px;font-size:11px;font-weight:600;display:inline-block;margin-bottom:14px}
  .cover-tbl{border-collapse:collapse;font-size:11px;margin-top:6px}
  .cover-tbl td{border:none;padding:3px 18px 3px 0;color:#33414f}
  .cover-tbl td:first-child{color:#8a98a6;text-transform:uppercase;font-size:9px;letter-spacing:1px;width:90px}
  .cover-foot{position:absolute;bottom:16mm;left:16mm;color:#9aa7b4;font-size:10px}
  .page-break{page-break-after:always}
  h1{color:${audienceColor};border-bottom:3px solid ${audienceColor};padding-bottom:6px;font-size:23px;margin:24px 0 12px;page-break-after:avoid}
  h2{color:${audienceColor};border-bottom:1px solid #d4dde6;padding-bottom:3px;font-size:17px;margin:20px 0 8px;page-break-after:avoid}
  h3{color:#26516f;font-size:13.5px;margin:14px 0 6px;page-break-after:avoid}
  h4{color:#3a4a5a;font-size:12px;margin:12px 0 4px;page-break-after:avoid}
  p,li{orphans:3;widows:3}
  table{border-collapse:collapse;width:100%;margin:10px 0;font-size:10.5px;page-break-inside:avoid}
  th,td{border:1px solid #d4dde6;padding:5px 8px;text-align:left;vertical-align:top}
  th{background:#eef3f8;color:${audienceColor}}
  tr:nth-child(even) td{background:#f8fafc}
  code{background:#eef1f5;padding:1px 4px;border-radius:3px;font-family:SFMono-Regular,Consolas,monospace;font-size:10px}
  pre{background:#f6f8fa;border:1px solid #e1e6ec;border-radius:6px;padding:10px;overflow:auto;font-size:9.5px;page-break-inside:avoid}
  pre code{background:none;padding:0}
  blockquote{border-left:4px solid ${audienceColor};background:#f1f6fb;margin:10px 0;padding:6px 14px;color:#33414f}
  .mermaid{margin:16px 0;text-align:center;page-break-inside:avoid}
  .mermaid svg{max-width:100%;height:auto}
  a{color:#0b6cc4;text-decoration:none}
  hr{border:none;border-top:1px solid #d4dde6;margin:18px 0}
</style></head><body>
${cover}
${body}
<script>${MERMAID_JS}</script>
<script>(async()=>{
  mermaid.initialize({startOnLoad:false,theme:"default",themeVariables:{primaryColor:"#eef3f8",primaryBorderColor:"${audienceColor}",lineColor:"#41515f",fontSize:"13px"},flowchart:{useMaxWidth:true,htmlLabels:true},sequence:{useMaxWidth:true},er:{useMaxWidth:true}});
  for(const n of document.querySelectorAll(".mermaid")){try{const{svg}=await mermaid.render("m"+n.dataset.i,n.textContent);n.innerHTML=svg;}catch(e){n.innerHTML="<pre style='color:#b00'>diagram error: "+e.message+"</pre>";}}
  window.__done=true;
})();</script></body></html>`;

const tmpHtml = resolve(outPath.replace(/\.pdf$/, ".html"));
writeFileSync(tmpHtml, html);
const browser = await puppeteer.launch({ headless: true });
const page = await browser.newPage();
await page.goto(pathToFileURL(tmpHtml).href, { waitUntil: "load" });
await page.waitForFunction("window.__done===true", { timeout: 60000 });
await page.pdf({
  path: outPath, format: "A4", printBackground: true,
  margin: { top: "16mm", bottom: "18mm", left: "15mm", right: "15mm" },
  displayHeaderFooter: true,
  headerTemplate: `<div style="font-size:7.5px;color:#aab6c2;width:100%;padding:4px 15mm 0;text-align:right">${(meta.title||"").replace(/&/g,"&amp;")} — ${meta.code||""}</div>`,
  footerTemplate: `<div style="font-size:7.5px;color:#aab6c2;width:100%;padding:0 15mm;display:flex;justify-content:space-between"><span>Aakaar — Confidential</span><span>Page <span class="pageNumber"></span> / <span class="totalPages"></span></span></div>`,
});
await browser.close();
console.log("WROTE " + outPath);
