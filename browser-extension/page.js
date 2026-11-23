// This factory runs only in Chrome's ISOLATED world. Never accept page messages
// or read score data from the shared live DOM.
globalThis.ctnCreatePortalCollector = function(sink) {
 if(location.hostname!=='community.capturethenarrative.com'||!/^\/user\/teams\/\d+\/?$/.test(location.pathname))return {enable(){},collect(){},leaderboard(){}};
 let enabled=false,activationKnown=false,portalTimer,busy=false,boardBusy=false,lastAttempt=0;
 const BOARD='/user/competitions/2',BOARD_MAX=300*1024;
 const nativeFetch=window.fetch.bind(window);
 const emit=records=>{if(enabled)sink.records(records);};
 let diagnostic={};
 const status=(text,pause=false,retryAfter=300000,details={})=>{diagnostic={...diagnostic,...details};if(enabled)sink.status({text,pause,retryAfter,diagnostic});};
 const retryDelay=value=>value?(Number.isFinite(Number(value))?Number(value)*1000:Math.max(0,Date.parse(value)-Date.now())):300000;
 // Export headers are UTC; preserve quoted CSV fields and remove access-code events.
 function cleanCSV(text){
  text=text.replace(/^\uFEFF/,'');
  let rows=[],row=[],field='',quoted=false;
  for(let i=0;i<text.length;i++){const c=text[i];if(c==='"'){if(quoted&&text[i+1]==='"'){field+='"';i++;}else quoted=!quoted;}else if(c===','&&!quoted){row.push(field);field='';}else if((c==='\n'||c==='\r')&&!quoted){if(c==='\r'&&text[i+1]==='\n')i++;row.push(field);rows.push(row);row=[];field='';}else field+=c;}
  if(field||row.length){row.push(field);rows.push(row);}
  if(quoted||JSON.stringify(rows[0])!==JSON.stringify(['When (UTC)','Type','Message']))throw Error('官方活动导出列已变化');
  if(rows.some(r=>r.length!==3&&r.some(v=>v.trim())))throw Error('活动 CSV 存在列数异常，已停止采集');
  const kept=rows.filter((r,i)=>i===0||(r.length===3&&r[1].trim().toUpperCase()!=='TEAMCODE'));
  return {csv:kept.map(r=>r.map(v=>'"'+v.replaceAll('"','""')+'"').join(',')).join('\r\n'),rows:kept.length-1};
 }
 // The live activity table runs ahead of the CSV export, which the site
 // refreshes about every two hours; the server keeps it apart. Each row's
 // exact UTC time is in data-utc (the visible time and header are rewritten
 // by the page's own script, which DOMParser does not run).
 function activityTable(doc){
  return [...doc.querySelectorAll('.activity-log-timestamp[data-utc]')].slice(0,100).map(stamp=>{
   const cells=stamp.closest('tr')?.querySelectorAll('td');
   return {when_utc:(stamp.getAttribute('data-utc')||'').slice(0,40),event:(cells?.[1]?.textContent||'').trim().slice(0,2000)};
  }).filter(row=>row.when_utc&&row.event);
 }
 async function portal(force=false){
  if(!enabled||busy||(!force&&Date.now()-lastAttempt<120000))return;busy=true;lastAttempt=Date.now();
  diagnostic={last_attempt_at:new Date(lastAttempt).toISOString(),http_status:null,error:'',code:'attempt'};
  status('正在读取本队总分与活动流水');
  try{
   const match=location.pathname.match(/^\/user\/teams\/(\d+)\/?$/);if(!match)return;
   const response=await nativeFetch(location.href,{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(20000)});
   diagnostic.http_status=response.status;
   if(!response.ok){const error='队伍页面受限 '+response.status;status(error,true,retryDelay(response.headers?.get('Retry-After')),{error,code:[401,403].includes(response.status)?'auth':response.status===429?'rate_limit':'network'});return;}
   if(new URL(response.url).pathname!==location.pathname)throw Error('队伍页面需要登录或访问受限');
   const html=await response.text();if(html.length>4_000_000)throw Error('队伍页面过大');
   const doc=new DOMParser().parseFromString(html,'text/html');
   const dt=[...doc.querySelectorAll('dt')].find(e=>e.textContent.trim()==='Score:');
   const total=dt?.parentElement.querySelector('dd')?.textContent.trim().replaceAll(',','');
   if(!total||!/^[-+]?\d+(?:\.\d+)?$/.test(total))throw Error('未找到有效 Score，已停止采集');
   const link=[...doc.querySelectorAll('a[href]')].find(a=>a.getAttribute('href')===`/user/teams/${match[1]}/activity.csv`);
   if(!link)throw Error('活动导出入口已变化');
   const csv=await nativeFetch(link.getAttribute('href'),{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(20000)});
   diagnostic.http_status=csv.status;
   if(!csv.ok){const error='活动导出受限 '+csv.status;status(error,true,retryDelay(csv.headers?.get('Retry-After')),{error,code:[401,403].includes(csv.status)?'auth':csv.status===429?'rate_limit':'network'});return;}
   if(new URL(csv.url,location.href).origin!==location.origin||new URL(csv.url,location.href).pathname!==`/user/teams/${match[1]}/activity.csv`)throw Error('活动导出需要登录或访问受限');
   const text=await csv.text();if(new TextEncoder().encode(text).byteLength>6_000_000)throw Error('活动导出超过 6 MB 单次上限，需要分批导入');
   const cleaned=cleanCSV(text),stamp=new Date().toISOString();
   status('本队数据已读取，等待缓存确认',false,300000,{code:'pending',last_collected_at:stamp,activity_rows:cleaned.rows});
   emit([{kind:'portal',observed_at:stamp,data:{team_id:Number(match[1]),total,activity_csv:cleaned.csv,activity_recent:activityTable(doc)}}]);
  }catch(e){status(e.message,true,300000,{error:e.message,code:/登录|访问受限/.test(e.message)?'auth':/fetch|network|timeout|abort/i.test(e.message)?'network':'schema'});}finally{busy=false;}
 }
 // Competition leaderboard (read-only GET, scheduled by background.js every 10–15 minutes). The server parses
 // table_html (ctn_data/leaderboard.py); rows here are only a rough preview. Failures never pause the portal.
 const headCells=table=>[...((table.tHead?.rows?.[0]||table.rows?.[0])?.cells||[])].map(cell=>cell.textContent.replace(/\s+/g,' ').trim().toLowerCase());
 const boardTable=doc=>[...doc.querySelectorAll('table')].find(table=>{const head=headCells(table);return head.some(text=>/team|name/.test(text))&&head.some(text=>/score|point/.test(text));});
 const number=text=>{const match=String(text).replace(/[,\s\u00a0\u202f']/g,'').replace(/\u2212/g,'-').match(/[-+]?\d+(?:\.\d+)?/);return match?Number(match[0]):null;};
 function boardRows(table){
  const head=headCells(table),score=head.findIndex(text=>/score|point/.test(text)),team=head.findIndex((text,i)=>i!==score&&/team|name/.test(text)),rank=head.findIndex(text=>/^(#|rank|pos|place|no\b)/.test(text));
  if(team<0||score<0)return [];
  return [...table.rows].slice(0,1000).map((row,i)=>{const cells=[...row.cells].map(cell=>cell.textContent.replace(/\s+/g,' ').trim());
   return {rank:rank>=0?number(cells[rank]):i,team:(cells[team]||'').slice(0,200),score:number(cells[score])};
  }).filter(row=>row.team&&Number.isFinite(row.score)).slice(0,500);
 }
 async function leaderboard(){
  if(!enabled||boardBusy)return;boardBusy=true;
  const stamp=new Date().toISOString(),report=(text,details={},pause=false,retryAfter=300000)=>{if(enabled)sink.board?.({text,pause,retryAfter,diagnostic:{last_attempt_at:stamp,...details}});};
  try{
   const response=await nativeFetch(BOARD,{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(20000)});
   if(!response.ok){const error='排行榜页面受限 '+response.status;report(error,{http_status:response.status,error,code:[401,403].includes(response.status)?'auth':response.status===429?'rate_limit':'network'},true,retryDelay(response.headers?.get('Retry-After')));return;}
   if(new URL(response.url,location.href).pathname.replace(/\/$/,'')!==BOARD){const error='排行榜需要登录或访问受限';report(error,{http_status:response.status,error,code:'auth'},true);return;}
   const html=await response.text();if(html.length>4_000_000)throw Error('排行榜页面过大');
   const table=boardTable(new DOMParser().parseFromString(html,'text/html'));
   if(!table)throw Error('未找到排行榜表格（表头需含 Team 和 Score）');
   const table_html=table.outerHTML;
   if(new TextEncoder().encode(table_html).byteLength>BOARD_MAX)throw Error('排行榜表格超过 300 KB');
   const rows=boardRows(table);
   report('排行榜已读取，等待缓存确认',{http_status:response.status,code:'pending',last_collected_at:stamp,rows:rows.length});
   emit([{kind:'leaderboard',observed_at:stamp,data:{competition_id:2,table_html,rows}}]);
  }catch(e){report(e.message,{error:e.message,code:/fetch|network|timeout|abort/i.test(e.message)?'network':'schema'});}finally{boardBusy=false;}
 }
 function enable(next){
  next=next===true;
  if(activationKnown&&enabled===next)return;
  enabled=next;activationKnown=true;clearInterval(portalTimer);
  if(enabled){portal();portalTimer=setInterval(portal,120000);}
 }
 return {enable,collect:portal,leaderboard};
};
