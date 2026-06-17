import { readFileSync } from "node:fs"; import { pathToFileURL } from "node:url"; import { resolve } from "node:path"; import puppeteer from "puppeteer";
const MJS = readFileSync(resolve("node_modules/mermaid/dist/mermaid.min.js"),"utf8");
const file = process.argv[2];
const md = readFileSync(file,"utf8");
const blocks = [...md.matchAll(/```mermaid\n([\s\S]*?)```/g)].map(m=>m[1].trim());
const b = await puppeteer.launch({headless:true}); const p = await b.newPage();
await p.setContent(`<body><script>${MJS}</script></body>`);
await p.waitForFunction("typeof mermaid!=='undefined'");
for (let i=0;i<blocks.length;i++){
  const res = await p.evaluate(async (code,i)=>{ try{ mermaid.initialize({startOnLoad:false}); await mermaid.render("t"+i, code); return "ok"; }catch(e){ return e.message.split("\n").slice(0,3).join(" | "); } }, blocks[i], i);
  if (res!=="ok") console.log(`BLOCK ${i+1} FAILED: ${res}\n----\n${blocks[i].slice(0,300)}\n====`);
}
await b.close(); console.log("done "+file);
