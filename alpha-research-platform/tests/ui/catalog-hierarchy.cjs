// Real indexed catalogue reads; sync and simulation submissions are prohibited.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
const fs=require('node:fs');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[],writes=[];page.on('pageerror',e=>errors.push(e.message));
 await page.route('**/api/**',route=>{
  if(route.request().method()!=='GET'){writes.push(route.request().url());return route.abort();}
  return route.continue();
 });
 await page.goto('http://127.0.0.1:8765/#data');
 await page.locator('#scope-select').selectOption('EQUITY/GLB/1/TOP3000');
 await page.locator('#catalog-category').selectOption('fundamental');
 const fundamental=page.locator('tr').filter({hasText:'Global Fundamental Data'});
 await fundamental.getByRole('button',{name:'Open fields',exact:true}).click();
 await page.getByRole('heading',{name:'Global Fundamental Data',exact:true}).waitFor();
 assert.match(await page.locator('#page').innerText(),/GLB · Delay 1 · TOP3000/);
 assert.equal(await page.locator('#catalog-category').inputValue(),'fundamental');
 await page.getByRole('button',{name:'Sync this dataset',exact:true}).click();
 await page.locator('#sync-dataset').waitFor();
 assert.equal(await page.locator('#sync-dataset').inputValue(),'Global Fundamental Data');
 assert.equal(await page.locator('#sync-form').getAttribute('data-scope'),'EQUITY/GLB/1/TOP3000');
 await page.keyboard.press('Escape');
 await page.locator('#scope-select').selectOption('EQUITY/GLB/1/MINVOL10M');
 await page.locator('#catalog-search').fill('techindi_model');
 const model=page.locator('tr').filter({hasText:'techindi_model'});
 await model.getByRole('button',{name:'Open fields',exact:true}).click();
 await page.getByRole('button',{name:'Sync this dataset',exact:true}).waitFor();
 await page.locator('tbody tr').first().waitFor();
 const fieldRequest=page.waitForResponse(r=>r.url().includes('min_instrument_coverage=80')&&r.url().includes('dataset_id=techindi_model'));
 await page.locator('#catalog-instrument').fill('80');
 await page.locator('#catalog-instrument').press('Tab');
 const response=await fieldRequest;assert.equal(response.status(),200);
 const data=await response.json();
 assert.ok(data.results.length>0);
 assert.ok(data.results.every(f=>f.instrument_coverage_pct>=80));
 fs.mkdirSync('data/previews',{recursive:true});
 await page.screenshot({path:'data/previews/catalog-hierarchy.png',fullPage:true});
 assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
 await browser.close();console.log('Passed: real scope/category/dataset/field navigation, coverage filters and scoped sync form; no writes.');
})().catch(e=>{console.error(e);process.exit(1)});
