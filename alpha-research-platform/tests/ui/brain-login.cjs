const {chromium}=require('playwright');
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({channel:'msedge',headless:true});
 try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 let verified=false;
 await page.route('**/api/brain/auth',route=>route.fulfill({json:{status:verified?'CONNECTED':'DISCONNECTED'}}));
 await page.route('**/api/brain/login',route=>{
  const body=route.request().postDataJSON();assert.equal(body.email,'research@example.com');assert.equal(body.password,'test-password');
  return route.fulfill({json:{status:'BIOMETRIC_REQUIRED',attempt_id:'mock-verification-attempt',verification_url:'https://api.worldquantbrain.com/authentication/persona/mock',retry_after:60}});
 });
 await page.route('**/api/brain/verify',route=>{verified=true;return route.fulfill({json:{status:'CONNECTED'}})});
 await page.goto('http://127.0.0.1:8765/#sync');
 await page.getByLabel('Email address',{exact:true}).fill('research@example.com');
 await page.getByLabel('Password',{exact:true}).fill('test-password');
 await page.getByRole('button',{name:'Sign in to BRAIN',exact:true}).click();
 await page.getByRole('heading',{name:'Verify your identity with BRAIN',exact:true}).waitFor();
 const link=page.getByRole('link',{name:/Open biometric verification/});
 assert.equal(await link.getAttribute('target'),'_blank');
 assert.equal(await page.locator('#brain-password').count(),0);
 await page.getByRole('button',{name:'Check verification',exact:true}).click();
 await page.getByText('Your BRAIN session is connected.',{exact:false}).waitFor();
 await page.screenshot({path:'data/previews/brain-login.png',fullPage:true});
 assert.equal(await page.locator('body').innerText().then(t=>t.includes('terminal')),false);
 assert.deepEqual(errors,[]);
 console.log('Passed: login form, biometric link, verification completion, password clearing, no terminal instructions.');
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exit(1)});
