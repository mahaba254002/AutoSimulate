const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:900}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:8765/#data');
 const selected=page.waitForResponse(r=>r.url().includes('/api/catalog/browse')&&r.url().includes('MINVOL10M'));
 await page.locator('#scope-select').selectOption('EQUITY/GLB/1/MINVOL10M');
 await selected;
 await page.locator('.inline-actions .eyebrow').filter({hasText:'MINVOL10M'}).waitFor();
 await page.locator('tbody tr').first().waitFor();
 await page.evaluate(()=>{
  window.originalCatalogue=document.querySelector('#scope-select');
  window.pageChanges=[];
  new MutationObserver(records=>window.pageChanges.push(...records.map(r=>r.target.textContent))).observe(document.querySelector('#page'),{childList:true});
  window.scrollTo(0,500);
 });
 const beforeScroll=await page.evaluate(()=>window.scrollY);
 const poll=page.waitForResponse(r=>r.url().includes('/api/catalog/browse'),{timeout:20000});
 await poll;
 await page.waitForTimeout(500);
 assert.equal(await page.evaluate(()=>window.originalCatalogue.isConnected),true,'Unchanged page should retain its existing elements');
 assert.deepEqual(await page.evaluate(()=>window.pageChanges),[],'Background refresh should not replace unchanged content');
 assert.equal(await page.evaluate(()=>window.scrollY),beforeScroll);
 assert.deepEqual(errors,[]);
 await browser.close();console.log('Passed: automatic refresh preserves page elements and scroll without loading flashes.');
})().catch(e=>{console.error(e);process.exit(1)});
