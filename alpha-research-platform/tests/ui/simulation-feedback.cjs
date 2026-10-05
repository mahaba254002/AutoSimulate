// Browser coverage with all research reads and cancellation writes mocked.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[];page.on('pageerror',error=>errors.push(error.message));
 const campaign={id:'feedback-test',name:'Simulation feedback test',objective:'Observe progress and explicit local stop controls.',
  status:'RUNNING',mode:'manual',category:'other',criteria:{},provider:'manual',model:'manual',max_attempts:50,
  concurrency:1,attempts:1,qualified:0,brief:null,error:null,candidates:[
   {id:'running-alpha',ordinal:1,expression:'rank(close)',template:'test',rationale:'test',status:'RUNNING',
    telemetry:{progress:35,stage:'Simulation processing',updated_at:new Date().toISOString()}},
   {id:'error-alpha',ordinal:2,expression:'rank(open)',template:'test',rationale:'test',status:'ERROR',
    telemetry:{stage:'Error',error:'BRAIN simulation error: Invalid operator argument'}},
   {id:'queued-alpha',ordinal:3,expression:'rank(volume)',template:'test',rationale:'test',status:'PLANNED',telemetry:{}}]};
 let cancelled;
 await page.route('**/api/research/campaigns',route=>route.fulfill({json:[campaign]}));
 await page.route('**/api/research/campaigns/feedback-test',route=>route.fulfill({json:campaign}));
 await page.route('**/api/research/candidates/*/cancel',async route=>{
  cancelled=route.request().url();
  campaign.candidates[0].telemetry.cancel_requested=true;
  campaign.status='STOPPING';
  await route.fulfill({json:campaign});
 });
 await page.goto('http://127.0.0.1:8765/#research/feedback-test');
 await page.getByRole('heading',{name:campaign.name,exact:true}).waitFor();
 assert.equal(await page.locator('progress').getAttribute('value'),'35');
 await page.getByText('BRAIN simulation error: Invalid operator argument',{exact:true}).waitFor();
 await page.getByRole('button',{name:'Cancel queued alpha',exact:true}).waitFor();
 await page.getByRole('button',{name:'Stop tracking alpha',exact:true}).click();
 await page.getByText(/BRAIN may continue its accepted simulation/).waitFor();
 await page.getByRole('button',{name:'Confirm',exact:true}).click();
 assert.ok(cancelled.endsWith('/running-alpha/cancel'));
 assert.deepEqual(errors,[]);
 await browser.close();
 console.log('Passed: reported progress, per-alpha error, queued cancellation control and explicit local-stop confirmation. No real cancellation requests.');
})().catch(error=>{console.error(error);process.exit(1)});
