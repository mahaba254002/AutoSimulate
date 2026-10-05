const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage();
 const writes=[];
 await page.route('**/api/**',route=>{
  if(route.request().method()!=='GET'){writes.push(route.request().url());return route.abort();}
  return route.continue();
 });
 await page.goto('http://127.0.0.1:8765/#providers');
 await page.locator('#openai-model').fill('unsaved-model');
 await page.locator('#openai-key').fill('demonstration-only-not-a-real-credential');
 await page.locator('h1').click();
 await page.waitForTimeout(11000);
 assert.equal(await page.locator('#openai-model').inputValue(),'unsaved-model');
 assert.equal(await page.locator('#openai-key').inputValue(),'demonstration-only-not-a-real-credential');
 assert.deepEqual(writes,[]);
 await browser.close();console.log('Passed: unfocused provider form retains unsaved values across the background refresh interval; no writes.');
})().catch(e=>{console.error(e);process.exit(1)});
