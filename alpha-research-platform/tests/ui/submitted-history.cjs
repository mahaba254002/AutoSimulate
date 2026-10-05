// Browser check with mocked BRAIN history responses. No remote requests or simulations.
const {chromium}=require('playwright');
const assert=require('node:assert/strict');

(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:1000},acceptDownloads:true});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 const alpha={id:'submitted-1',name:'Value model revision',type:'REGULAR',status:'ACTIVE',
   dateSubmitted:'2025-06-01T00:00:00Z',settings:{instrumentType:'EQUITY',region:'USA',delay:1,universe:'TOP3000',language:'FASTEXPR',neutralization:'SUBINDUSTRY',decay:0,truncation:0.08},
   regular:{code:'rank(close)+rank(volume)'},is:{sharpe:5.1,fitness:3.2,turnover:0.18},os:{sharpe:2.4,fitness:1.2}};
 let status={status:'COMPLETE',downloaded:1,total:1,saved:1,progress_pct:100,error:null,completed_at:'2025-06-01T00:00:00Z'};
 const posts=[];
 await page.route('**/api/submitted/status',r=>r.fulfill({json:status}));
 await page.route('**/api/submitted?*',r=>r.fulfill({json:{count:1,regions:['USA'],results:[{id:alpha.id,name:alpha.name,type:alpha.type,status:alpha.status,region:'USA',universe:'TOP3000',delay:1,date_submitted:alpha.dateSubmitted,expression:alpha.regular.code,metrics:{is:alpha.is,os:alpha.os},score:null,in_latest_sync:true}]}}));
 await page.route('**/api/submitted/export',r=>r.fulfill({json:{source:'saved_submitted_alpha_history',count:1,alphas:[alpha]}}));
 await page.route('**/api/submitted/submitted-1',r=>r.fulfill({json:alpha}));
 await page.route('**/api/submitted/sync',r=>{posts.push(r.request().postDataJSON());status={...status,status:'RUNNING',downloaded:1,total:2,progress_pct:50};return r.fulfill({json:status})});
 await page.route('**/api/submitted/stop',r=>{status={...status,status:'STOPPED'};return r.fulfill({json:status})});
 await page.route('**/api/catalog/scopes',r=>r.fulfill({json:[{id:'EQUITY/USA/1/TOP3000',status:'COMPLETE',fields:2,datasets:1,neutralizations:['SUBINDUSTRY'],settings:{instrumentType:'EQUITY',region:'USA',universe:'TOP3000',delay:1}}]}));
 await page.goto('http://127.0.0.1:8765/#alphas');
 await page.getByRole('tab',{name:'Submitted history'}).click();
 await page.getByText('submitted-1',{exact:true}).waitFor();
 require('node:fs').mkdirSync('data/previews',{recursive:true});
 await page.screenshot({path:'data/previews/submitted-history.png',fullPage:true});
 assert.equal(await page.getByText('5.10 / 3.20',{exact:true}).count(),1);
 assert.equal(await page.getByText('2.40 / 1.20',{exact:true}).count(),1);
 assert.equal(await page.getByRole('cell',{name:'—',exact:true}).count()>=1,true);
 await page.getByRole('button',{name:'Inspect',exact:true}).click();
 await page.getByText('Saved BRAIN metadata; inspecting does not resimulate').waitFor();
 await page.locator('#dialog-close').click();
 const downloadPromise=page.waitForEvent('download');
 await page.getByRole('button',{name:'Export saved history as JSON'}).click();
 const download=await downloadPromise;assert.equal(download.suggestedFilename(),'submitted-alphas.json');
 await page.getByRole('button',{name:'Refresh all submitted alphas'}).click();
 assert.deepEqual(posts,[{restart:true}]);
 await page.getByRole('progressbar',{name:'Submitted history sync progress'}).waitFor();
 assert.equal(await page.getByRole('progressbar').getAttribute('value'),'50');
 await page.getByRole('button',{name:'Stop after current page'}).click();
 await page.getByRole('button',{name:'Resume interrupted sync'}).click();
 assert.deepEqual(posts,[{restart:true},{restart:false}]);
 await page.getByRole('button',{name:'Develop alpha'}).click();
 await page.locator('#research-source').waitFor();
 assert.equal(await page.locator('#research-source').inputValue(),alpha.regular.code);
 assert.equal(await page.locator('#research-scope').inputValue(),'EQUITY/USA/1/TOP3000');
 assert.deepEqual(errors,[]);
 await browser.close();console.log('Submitted history browser flow passed with mocked records and no remote work.');
})().catch(e=>{console.error(e);process.exit(1)});
