import {chromium} from '../../../web/node_modules/playwright/index.mjs';
import {parseArgs} from 'node:util';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import fs from 'node:fs';
const {values}=parseArgs({options:{config:{type:'string'},evidence:{type:'string'},proxy:{type:'string'},reload:{type:'boolean'}}});
if(!values.config||!values.evidence)throw Error('Provide --config and --evidence');
const config=JSON.parse(fs.readFileSync(values.config,'utf8'));
if(!config.isolated||new URL(config.origin).hostname!=='relay.ccr.test')throw Error('Disposable .test installation required');
fs.mkdirSync(values.evidence,{recursive:true,mode:0o700});
const folder=pathToFileURL(path.resolve(values.evidence)+path.sep);
const target=config.target??'5.3.19';
const browser=await chromium.launch({headless:true,...(values.proxy?{proxy:{server:values.proxy}}:{})});
const context=await browser.newContext({ignoreHTTPSErrors:true,colorScheme:'dark',viewport:{width:1280,height:900}});
const page=await context.newPage();const report={scenario:'real UI upgrade and auth reconnect preserve in-memory draft',started:new Date().toISOString(),starts:[],states:[],pass:false};
const draft='验收草稿：更新结束后必须保留，不发送';let request;
page.on('request',r=>{if(r.url().includes('/api/updates/')&&r.method()==='POST'){const body=r.postDataJSON();if(body.op==='start'){report.starts.push(body.job_id);request=body;}}});
async function login(){await page.locator('input[name="password"]:visible,button[aria-label="更多设置"]:visible').first().waitFor({timeout:30000});if(await page.getByPlaceholder('访问密码').isVisible()){await page.getByPlaceholder('访问密码').fill(config.password);await page.getByPlaceholder('访问密码').press('Enter');}await page.getByRole('button',{name:'更多设置',exact:true}).waitFor({timeout:30000});}
async function open(){await page.getByRole('button',{name:'更多设置',exact:true}).click();await page.getByRole('button',{name:/^检查更新/}).click();}
try{
 await page.goto(config.origin);await login();await page.locator('textarea').waitFor();
 await page.waitForFunction(()=>{const t=document.querySelector('textarea');return t&&!t.disabled;},{timeout:30000});
 if(!values.reload)await page.locator('textarea').fill(draft);await open();
 await page.getByRole('button',{name:'更新到 v'+target,exact:true}).click();
 if(!request)throw Error('UI did not submit the real update');
 const replay=await context.request.post(config.origin+'/api/updates/'+config.machine,{headers:{Origin:config.origin},data:request});
 report.duplicate=await replay.json();if(report.duplicate.job?.id!==request.job_id)throw Error('Duplicate did not return original job');
 await context.request.post(config.origin+'/api/logout',{headers:{Origin:config.origin},data:{}});
 if(values.reload)await page.reload();
 await page.getByPlaceholder('访问密码').waitFor({timeout:30000});await login();await open();
 let last='';const deadline=Date.now()+12*60*1000;
 while(Date.now()<deadline){
  if(await page.getByPlaceholder('访问密码').isVisible()){await login();await open();}
  try{
   const response=await context.request.get(config.origin+'/api/updates/'+config.machine);
   if(response.status()===401){await page.getByPlaceholder('访问密码').waitFor({timeout:30000});await login();await open();continue;}
   const state=await response.json();const key=[state.current,state.relay_version,state.job?.phase,state.job?.reason,state.maintenance].join('/');
   if(key!==last){last=key;report.states.push({at:new Date().toISOString(),...state});console.log(key);}
   if(['failed','blocked','cancelled'].includes(state.job?.phase))throw Error('Actual update failed: '+JSON.stringify(state.job));
   if(state.current===target&&state.relay_version===target&&state.job?.phase==='succeeded'&&state.maintenance===false){report.final=state;break;}
  }catch(error){if(String(error).includes('Actual update failed'))throw error;console.log('Waiting for control reconnect');}
  await page.waitForTimeout(2000);
 }
 if(!report.final)throw Error('Real update did not finish within the acceptance deadline');
 await page.getByRole('button',{name:'收起更新窗口',exact:true}).click();await page.waitForFunction(()=>{const t=document.querySelector('textarea');return t&&!t.disabled;},{timeout:30000});
 report.draftPreserved=await page.locator('textarea').inputValue()===draft;
 if(!values.reload&&!report.draftPreserved)throw Error('Draft was lost across update/login');
 if((await page.locator('body').innerText()).includes('当前操作暂时无法执行'))throw Error('Unexpected operation warning after update');
 await open();
 const refresh=page.getByRole('button',{name:'刷新页面',exact:true});if(!values.reload&&await refresh.isVisible()){await refresh.click();await page.getByText('还有未发送的内容或正在导入的附件，请先发送或自行保存并清空，再更新页面。',{exact:true}).waitFor();report.unsavedRefreshGuard=true;}
 await page.screenshot({path:new URL('real-update-complete.png',folder).pathname});
 if(values.reload){await page.reload();await login();await open();const state=await(await context.request.get(config.origin+'/api/updates/'+config.machine)).json();if(state.job?.id!==request.job_id||state.job?.phase!=='succeeded'||state.maintenance)throw Error('Full reload lost the original completed job');report.reloadPreservedJob=true;}
 report.pass=true;console.log('REAL UI ACCEPTANCE PASSED');
}catch(error){report.error=String(error);await page.screenshot({path:new URL('real-update-failure.png',folder).pathname}).catch(()=>{});console.error(report.error);process.exitCode=1;}
finally{report.finished=new Date().toISOString();fs.writeFileSync(new URL('report.json',folder),JSON.stringify(report,null,2));await browser.close();}
