// Form and plan review test. All mutations are intercepted; no simulations start.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 let submitted=null, plan=null;
 await page.route('**/api/catalog/scopes',route=>route.fulfill({json:[{id:'test-scope',status:'COMPLETE',fields:2,datasets:1,neutralizations:['SUBINDUSTRY'],settings:{region:'USA',universe:'TOP3000',delay:1}}]}));
 await page.route('**/api/research/campaigns',async route=>{
  if(route.request().method()==='GET')return route.fulfill({json:plan?[plan]:[]});
  submitted=route.request().postDataJSON();
  plan={...submitted,id:'redevelopment-preview',status:'DRAFT',candidates:[],attempts:0,qualified:0,
   brief:{hypothesis:submitted.objective,settings:{region:'USA',delay:1,universe:'TOP3000'},scope_id:'test-scope',
    fields_considered:2,available_documented_fields:2,provider:'manual',model:'manual',templates:[],
    manual_expressions:[{expression:'rank(ts_regression(close,ts_step(1),40,rettype=0))'}],
    redevelopment:{parent_expression:submitted.source_expression,method:'Component diagnostics first; controlled revisions second.',
      parent:{run_id:null,metrics:{},note:'No local baseline available'},field_evidence:[{id:'close',description:'Closing share price.'}],
      branches:[{phase:1,template:'Residual branch',change:'Rank the trend residual.',rationale:'Test the error term, rettype=0.'}]}}};
  return route.fulfill({json:plan});
 });
 await page.route('**/api/research/campaigns/redevelopment-preview',route=>route.fulfill({json:plan}));
 await page.goto('http://127.0.0.1:8765/#research/new');
 await page.getByRole('tab',{name:'Develop an existing alpha',exact:true}).click();
 assert.equal(await page.locator('#research-provider').count(),0);
 assert.equal(await page.locator('#research-expressions').count(),0);
 await page.locator('#research-name').fill('Develop original alpha');
 await page.locator('#research-objective').fill('Test model trend and residual signals with meaningful documented transformations.');
 await page.locator('#research-source').fill('group_neutralize(rank(close)+rank(volume),subindustry)');
 assert.equal(await page.locator('#min-sharpe').inputValue(),'5');
 assert.equal(await page.locator('#max-turnover').inputValue(),'30');
 assert.equal(await page.locator('#concurrency').getAttribute('max'),'200');
 await page.locator('details > summary').filter({hasText:'Execution limits'}).click();
 await page.locator('#concurrency').fill('200');
 await page.getByRole('button',{name:'Save research project',exact:true}).click();
 await page.getByRole('heading',{name:'Alpha redevelopment',exact:true}).waitFor();
 assert.equal(submitted.mode,'redevelop');assert.equal(submitted.provider,'manual');
 assert.deepEqual(submitted.redevelopment_windows,[40,20,60,126]);
 assert.equal(submitted.manual_plan.expressions[0],submitted.source_expression);
 await page.getByRole('button',{name:'Start simulations',exact:true}).waitFor();
 const fs=require('node:fs');fs.mkdirSync('data/previews',{recursive:true});
 await page.screenshot({path:'data/previews/redevelopment-plan.png',fullPage:true});
 assert.deepEqual(errors,[]);
 await browser.close();console.log('Redevelopment form and review passed; no real writes or simulations.');
})().catch(e=>{console.error(e);process.exit(1)});
