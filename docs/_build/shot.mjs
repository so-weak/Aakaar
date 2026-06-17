import { pathToFileURL } from "node:url"; import { resolve } from "node:path"; import puppeteer from "puppeteer";
const b=await puppeteer.launch({headless:true}); const p=await b.newPage();
await p.setViewport({width:880,height:1240,deviceScaleFactor:1});
await p.goto(pathToFileURL(resolve(process.argv[2])).href,{waitUntil:"load"});
await p.waitForFunction("window.__done===true",{timeout:30000});
// cover screenshot
await p.screenshot({path:resolve(process.argv[3]),clip:{x:0,y:0,width:880,height:1130}});
await b.close();
