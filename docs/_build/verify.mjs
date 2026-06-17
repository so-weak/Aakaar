import { pathToFileURL } from "node:url"; import { resolve } from "node:path"; import { readdirSync } from "node:fs"; import puppeteer from "puppeteer";
const dir = resolve("../pdf");
const htmls = readdirSync(dir).filter(f=>f.endsWith(".html")).sort();
const b = await puppeteer.launch({headless:true});
let totalOk=0, totalErr=0;
for (const h of htmls){
  const p = await b.newPage();
  await p.goto(pathToFileURL(resolve(dir,h)).href,{waitUntil:"load"});
  try { await p.waitForFunction("window.__done===true",{timeout:30000}); } catch(e){}
  const r = await p.evaluate(()=>{ const ns=[...document.querySelectorAll(".mermaid")]; let ok=0,err=0; for(const n of ns){ if(n.querySelector("svg")) ok++; else err++; } return {ok,err,total:ns.length}; });
  totalOk+=r.ok; totalErr+=r.err;
  console.log(`${r.err? "FAIL":"ok  "} ${h.replace(/\.html$/,"")}: ${r.ok}/${r.total} diagrams rendered${r.err?(" ("+r.err+" FAILED)"):""}`);
  await p.close();
}
await b.close();
console.log(`\nTOTAL: ${totalOk} diagrams OK, ${totalErr} failed`);
