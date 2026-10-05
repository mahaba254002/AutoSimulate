// Read-only smoke coverage for the local workspace. No provider or BRAIN POSTs.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
(async () => {
 const browser = await chromium.launch({channel:'msedge',headless:true});
 const page = await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[];
 page.on('pageerror',error=>errors.push(error.message));
 await page.goto('http://127.0.0.1:8765');
 await page.getByRole('heading',{name:'Research overview',exact:true}).waitFor();
 assert.equal(await page.locator('nav a').count(),13);
 fs.mkdirSync('data/previews',{recursive:true});
 await page.screenshot({path:'data/previews/workspace.png',fullPage:true});
 const routes={matrix:'Simulation Matrix',data:'Data Explorer',research:'Research Labs',tasks:'Tasks',
   alphas:'Alphas',portfolio:'Portfolio',templates:'Template Library',knowledge:'Knowledge Library',
   learning:'Learning History',providers:'LLM Integration',sync:'Sync with BRAIN',tools:'Tools'};
 for (const [route,title] of Object.entries(routes)) {
   await page.goto(`http://127.0.0.1:8765/#${route}`);
   await page.getByRole('heading',{name:title,exact:true}).waitFor();
   assert.equal(await page.locator('nav a[aria-current="page"]').count(),1);
 }
 await page.locator('#inspect-expression').fill('rank(ts_mean(close, 21))');
 await page.getByRole('button',{name:'Inspect structure',exact:true}).click();
 await page.locator('#inspect-result').getByText('close',{exact:true}).waitFor();
 await page.goto('http://127.0.0.1:8765/#research/new');
 await page.getByRole('heading',{name:'New research',exact:true}).waitFor();
 await page.getByRole('tab',{name:'Existing alpha',exact:true}).click();
 await page.locator('#research-source').waitFor();
 await page.getByRole('tab',{name:'Data category',exact:true}).click();
 assert.equal(await page.locator('#research-source').count(),0);
 await page.screenshot({path:'data/previews/research.png',fullPage:true});
 await page.getByRole('tab',{name:'Manual research',exact:true}).click();
 await page.locator('#research-variant-count').waitFor({state:'attached'});
 await page.getByText('Generate field variants from a template',{exact:true}).click();
 await page.locator('#research-variant-count').fill('100');
 await page.locator('details').filter({has:page.locator('#attempts')}).locator('summary').click();
 await page.locator('#attempts').fill('100');
 await page.locator('#research-name').fill('Template substitution test');
 await page.locator('#research-objective').fill('Preserve a documented mixed category alpha structure.');
 await page.locator('#research-expressions').fill('rank(close)');
 let submitted;
 await page.route('**/api/research/campaigns',async route=>{
   if(route.request().method()!=='POST')return route.continue();
   submitted=route.request().postDataJSON();
   await route.fulfill({status:400,contentType:'application/json',body:JSON.stringify({detail:'Mock validation response; no project saved.'})});
 });
 await page.getByRole('button',{name:'Save research project',exact:true}).click();
 await page.getByText('Mock validation response; no project saved.',{exact:true}).waitFor();
 assert.equal(submitted.manual_plan.variant_count,100);
 assert.equal(submitted.manual_plan.variant_seed,42);
 assert.equal(submitted.provider,'manual');
 assert.deepEqual(submitted.manual_plan.expressions,['rank(close)']);
 await page.setViewportSize({width:390,height:844});
 await page.goto('http://127.0.0.1:8765/#dashboard');
 await page.getByRole('heading',{name:'Research overview',exact:true}).waitFor();
 assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
 await page.getByRole('button',{name:'Toggle navigation',exact:true}).click();
 assert.equal(await page.locator('#sidebar').evaluate(e=>e.classList.contains('open')),true);
 await page.screenshot({path:'data/previews/mobile.png',fullPage:true});
 assert.deepEqual(errors,[]);
 await browser.close();
 console.log('Passed: 13 pages, expression inspection, research modes, mobile navigation, no browser errors.');
})().catch(error=>{console.error(error);process.exit(1)});
