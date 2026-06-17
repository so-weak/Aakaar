import { pathToFileURL } from "node:url"; import { resolve } from "node:path"; import puppeteer from "puppeteer";
const b=await puppeteer.launch({headless:true}); const p=await b.newPage();
await p.setViewport({width:1000,height:1400,deviceScaleFactor:1});
await p.goto(pathToFileURL(resolve(process.argv[2])).href,{waitUntil:"load"});
await p.waitForFunction("window.__done===true",{timeout:30000});
// find the first erDiagram svg and screenshot it
const el = await p.evaluateHandle(()=>{ const m=[...document.querySelectorAll(".mermaid svg")]; return m.find(s=>s.querySelector('[id*="entity"]')||s.innerHTML.includes("er"))||m[0]; });
await el.screenshot({path:resolve(process.argv[3])});
await b.close();
