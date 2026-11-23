const BASE = 'http://127.0.0.1:8765';
const SITE_PATTERNS = ['https://community.capturethenarrative.com/user/teams/*'];
const KINDS = ['portal','leaderboard'], BOARD_MAX = 300*1024;
// Own-team records must carry the paired team; leaderboard records only the competition table (ctn_data/leaderboard.py).
const validRecord = (r, team) => r?.kind === 'portal' ? String(r.data?.team_id) === team
 : r?.kind === 'leaderboard' && r.data?.competition_id === 2 && typeof r.data.table_html === 'string' && new TextEncoder().encode(r.data.table_html).byteLength <= BOARD_MAX
  && typeof r.observed_at === 'string' && Array.isArray(r.data.rows) && r.data.rows.length <= 500 && Object.keys(r.data).every(key => ['competition_id','table_html','rows'].includes(key));
let chain = Promise.resolve(), ticking, refreshRequested = false, collectionRequested = false, tickAgain = false;
let volatileError = null, volatileBufferFull = false;
function serial(fn) { chain = chain.then(fn, fn); return chain; }
const save = value => chrome.storage.local.set(value);
const read = () => chrome.storage.local.get(null);
const bytes = value => new TextEncoder().encode(JSON.stringify(value)).byteLength;
const storageBytes = state => Object.entries(state).reduce((n,[key,value])=>n+new TextEncoder().encode(key).byteLength+bytes(value),0);
async function hasCapacity(state, queue) {
 const used = chrome.storage.local.getBytesInUse ? await chrome.storage.local.getBytesInUse(null) : storageBytes(state);
 const previous = Object.hasOwn(state,'queue') ? bytes(state.queue)+5 : 0;
 const quota = chrome.storage.local.QUOTA_BYTES || 10485760;
 return bytes(queue)<=7_000_000 && used-previous+bytes(queue)+5<=quota-524288;
}
async function bufferFailure(message) {
 volatileBufferFull=true;volatileError=message;
 await save({bufferFull:true,syncError:message}).catch(()=>{});
 await stopAll(await read());
}
function roleOf(url, team) {
 try {
  const u = new URL(url);
  if (u.origin === 'https://community.capturethenarrative.com' && u.pathname.replace(/\/$/, '') === '/user/teams/' + team) return 'portal';
 } catch {}
 return null;
}
function active(state, tabId, url) {
 const role = roleOf(url, state.teamId);
 return !!role && state.autoEnabled !== false && !state.bufferFull && !volatileBufferFull && state.managed?.[role] === tabId && state.tabs?.[tabId] === true && !(state.pauses?.[role]?.until > Date.now());
}
async function signal(id, enabled, managed=true) { try { return (await chrome.tabs.sendMessage(id, {type:'enable', enabled, managed}))?.ok === true; } catch { return false; } }
async function stopAll(state) {
 await save({tabs:{}}).catch(()=>{});
 await Promise.all([...new Set([...Object.values(state.managed || {}),...Object.keys(state.tabs || {}).map(Number)])].map(id => signal(id, false, false)));
}
async function migratePortalOnly() {
 const state=await read();
 if(state.portalOnlyVersion===130)return;
 const portalId=state.managed?.portal;
 // Disable old collectors without navigating, refreshing or closing pages.
 // Previously captured public records stay archived locally, never discarded.
 for(const id of new Set([...Object.values(state.managed||{}),...Object.keys(state.tabs||{}).map(Number)])) {
  if(id!==portalId)await signal(id,false,false);
 }
 const queue=state.queue||[];
 await save({portalOnlyVersion:130,managed:portalId?{portal:portalId}:{},
  tabs:portalId?{[portalId]:state.tabs?.[portalId]===true}:{},
  pauses:state.pauses?.portal?{portal:state.pauses.portal}:{},
  blocked:state.blocked?.portal?{portal:state.blocked.portal}:{},
  pageStatus:state.pageStatus?.portal?{portal:state.pageStatus.portal}:{},
  queue:queue.filter(record=>KINDS.includes(record.kind)),
  legacyQueue:[...(state.legacyQueue||[]),...queue.filter(record=>!KINDS.includes(record.kind))]});
}
async function reconcile(force = false) {
 const s = await read();
 if (!s.teamId || s.autoEnabled === false || s.bufferFull || volatileBufferFull) { await stopAll(s); return; }
 const found = await chrome.tabs.query({url:SITE_PATTERNS});
 const desired = {portal:'https://community.capturethenarrative.com/user/teams/' + s.teamId};
 const managed = {}, tabs = {}, pauses = {...s.pauses}, reload = [], navigate = {}, blocked = {};
 for (const [role, url] of Object.entries(desired)) {
  let tab = found.find(t => t.id === s.managed?.[role] && roleOf(t.url, s.teamId) === role)
   || found.find(t => roleOf(t.url, s.teamId) === role);
  // A portal redirect may hide its URL under the narrow host permission. Keep
  // the owned tab available for login instead of spawning a login tab every tick.
  if (!tab && role === 'portal' && s.managed?.portal) {
   const owned = await chrome.tabs.get(s.managed.portal).catch(() => null);
   if (owned) { managed.portal=owned.id; tabs[owned.id]=false; blocked.portal='等待原标签页登录并返回本队页面'; continue; }
  }
  const existing = !!tab;
  const paused = pauses[role]?.until > Date.now();
  // During a cooldown do not open a page that would immediately issue more requests.
  if (!tab && paused) continue;
  if (!tab) { tab = await chrome.tabs.create({url:'about:blank', active:false}); navigate[tab.id] = url; }
  managed[role] = tab.id;
  tabs[tab.id] = !paused;
  if (!paused && existing && (force || s.refreshPending || s.managed?.[role] !== tab.id)) reload.push(tab.id);
  if (!paused) delete pauses[role];
 }
 // Persist before reload, so document_start can enable without waiting for a network call.
 await save({managed, tabs, pauses, blocked, refreshPending:false});
 for (const id of new Set([...Object.values(s.managed || {}),...Object.keys(s.tabs || {}).map(Number)])) if (!Object.values(managed).includes(id)) await signal(id, false, false);
 for (const id of Object.values(managed)) {
  if (navigate[id]) await chrome.tabs.update(id, {url:navigate[id]});
  else if (reload.includes(id)) await chrome.tabs.reload(id).catch(() => {});
  else if (!await signal(id, tabs[id]) && tabs[id] && found.find(t => t.id === id)?.status !== 'loading') await chrome.tabs.reload(id).catch(() => {});
 }
}
async function bootstrap() {
 const r = await fetch(BASE + '/api/bootstrap', {signal:AbortSignal.timeout(5000)});
 if (!r.ok) throw Error('本地连接未授权（' + r.status + '）');
 return (await r.json()).csrf_token;
}
async function sync(token) {
 const initial = await read();
 if (initial.syncRetryAfter > Date.now()) return;
 for (let batch = 0; batch < 4; batch++) {
  const state = await read(), queue = state.queue || [];
  if (!queue.length) break;
  const r = await fetch(BASE + '/api/browser-capture', {method:'POST', signal:AbortSignal.timeout(10000), headers:{'Content-Type':'application/json','X-CTN-Token':token}, body:JSON.stringify({records:queue.slice(0, 50)})});
  if (!r.ok) throw Error('本地入库失败 ' + r.status);
  const result = await r.json(), accepted = new Set(result.accepted || []), rejected = new Set((result.errors || []).filter(e => e.retryable === false).map(e => e.id));
  const remaining = queue.filter(r => !accepted.has(r.id) && !rejected.has(r.id));
  const unconfirmed = queue.slice(0,50).some(record => !accepted.has(record.id) && !rejected.has(record.id));
  const error = (result.errors || []).find(e => !rejected.has(e.id)) || result.errors?.[0];
  await save({queue:remaining, rejected:[...(state.rejected || []), ...queue.filter(r => rejected.has(r.id))].slice(-100), ...(accepted.size?{lastSync:new Date().toISOString()}:{}), syncRetryAfter:unconfirmed?Date.now()+30000:0, syncError:unconfirmed?'入库暂未确认，30 秒后重试：' + (error?.error || '服务端未确认记录'):error?'已隔离无效记录：' + error.error:null});
  if (unconfirmed) break;
  if (remaining.length === queue.length) break;
 }
 const state = await read();
 if ((state.bufferFull||volatileBufferFull) && await hasCapacity(state,state.queue||[])) {
  await save({bufferFull:false,syncError:null});volatileBufferFull=false;volatileError=null;
 }
}
async function collect(force=false) {
 const s=await read(),id=s.managed?.portal;
 if(!id||!active(s,id,'https://community.capturethenarrative.com/user/teams/'+s.teamId)||volatileBufferFull)return;
 const attempt=Date.parse(s.pageStatus?.portal?.last_attempt_at||'');
 if(!force&&Number.isFinite(attempt)&&Date.now()-attempt<120000)return;
 // A request is not a successful observation. Retry the handshake after 30s
 // if the page did not report that its HTTP request actually started.
 if(!force&&s.lastCollectionRequest&&Date.now()-s.lastCollectionRequest<25000)return;
 await save({lastCollectionRequest:Date.now()});
 await chrome.tabs.sendMessage(id,{type:'collect',force}).catch(()=>{});
}
// One read-only leaderboard GET every 10–15 minutes from the portal tab. Its own pause (board.pause_until)
// never stops the portal; a paused or busy portal page skips it and is asked again a minute later.
async function collectBoard() {
 const s=await read(),id=s.managed?.portal,board=s.board||{};
 if(!id||!active(s,id,'https://community.capturethenarrative.com/user/teams/'+s.teamId)||volatileBufferFull)return;
 if(Date.now()<Math.max(board.next_at||0,board.pause_until||0))return;
 const started=(await chrome.tabs.sendMessage(id,{type:'leaderboard'}).catch(()=>null))?.ok===true;
 await save({board:{...board,next_at:Date.now()+(started?600000+Math.floor(Math.random()*300000):60000)}});
}
async function heartbeat(token) {
 const s = await read();
 const r = await fetch(BASE + '/api/extension-status', {method:'POST', signal:AbortSignal.timeout(5000), headers:{'Content-Type':'application/json','X-CTN-Token':token}, body:JSON.stringify({extension_id:chrome.runtime.id, version:chrome.runtime.getManifest().version, auto_enabled:s.autoEnabled !== false, queue_count:(s.queue || []).length, buffer_full:!!s.bufferFull||volatileBufferFull, pages:Object.entries(s.managed || {}).map(([role, tab_id]) => ({...diagnostics(s.pageStatus?.[role]),role,tab_id,enabled:s.tabs?.[tab_id] === true, pause_until:s.pauses?.[role]?.until || null, bucket:s.pauses?.[role]?.bucket || null, reason:s.blocked?.[role] || s.pauses?.[role]?.reason || ''})), error:volatileError || s.syncError || s.connectionError || null})});
 if (!r.ok) throw Error('采集状态上报失败 ' + r.status);
}
async function tick(force = false, collectNow = false) {
 let token;
 try {
  await migratePortalOnly();
  const response = await fetch(BASE + '/api/extension-status', {signal:AbortSignal.timeout(5000)});
  if (!response.ok) {
   if (response.status === 403) { const s = await read(); await stopAll(s); await save({teamId:null, connection:'unpaired'}); }
   throw Error('请先在本地工作台连接此扩展');
  }
  const pair = await response.json();
  if (pair.extension_id !== chrome.runtime.id || !/^[1-9][0-9]*$/.test(String(pair.team_id))) {
   const s = await read(); await stopAll(s); await save({teamId:null,connection:'unpaired',syncError:'等待本地工作台绑定'}); return;
  }
  const old = await read();
  if (old.teamId && old.teamId !== String(pair.team_id)) { await stopAll(old); await save({managed:{},pauses:{}}); }
  await save({teamId:String(pair.team_id),connection:'paired',connectionError:null,collectionStatus:pair.collection_status||null,...(old.connection!=='paired'?{syncError:null}:{}),checkedAt:new Date().toISOString()});
  await reconcile(force);
  token = await bootstrap();
  await sync(token);
  if(!volatileBufferFull)volatileError=null;
  await reconcile();
  await collect(collectNow);
  await collectBoard();
  await heartbeat(token);
 } catch (e) { volatileError=e.message;await save({connectionError:e.message}).catch(()=>{});if(token)await heartbeat(token).catch(()=>{}); }
}
function requestTick(force = false, collectNow = false) {
 refreshRequested ||= force;
 collectionRequested ||= collectNow;
 if(ticking){tickAgain=true;return ticking;}
 if (!ticking) {
  ticking = serial(async () => { const refresh = refreshRequested,collection=collectionRequested;refreshRequested=false;collectionRequested=false;tickAgain=false;await tick(refresh,collection); }).finally(() => { ticking = null; if (refreshRequested||collectionRequested||tickAgain) requestTick(); });
 }
 return ticking;
}
chrome.alarms.create('sync', {periodInMinutes:0.5});
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === 'sync') requestTick(); });
chrome.runtime.onInstalled.addListener(() => requestTick(true));
chrome.runtime.onStartup.addListener(() => requestTick());
function diagnostics(input) {
 const result={};if(!input||typeof input!=='object')return result;
 for(const key of ['last_attempt_at','last_collected_at'])if(typeof input[key]==='string'&&Number.isFinite(Date.parse(input[key])))result[key]=new Date(input[key]).toISOString();
 if(Number.isInteger(input.http_status)&&input.http_status>=100&&input.http_status<=599)result.http_status=input.http_status;
 if(Number.isInteger(input.activity_rows)&&input.activity_rows>=0&&input.activity_rows<=10000000)result.activity_rows=input.activity_rows;
 if(Number.isInteger(input.rows)&&input.rows>=0&&input.rows<=500)result.rows=input.rows;
 if(typeof input.activity_digest==='string'&&/^[a-f0-9]{64}$/.test(input.activity_digest))result.activity_digest=input.activity_digest;
 if(typeof input.error==='string')result.error=input.error.slice(0,180);
 if(['attempt','success','pending','auth','rate_limit','network','schema','buffer'].includes(input.code))result.code=input.code;
 return result;
}
chrome.runtime.onMessage.addListener((message, sender, reply) => {
 if(message.type==='permit'){reply({allowed:false,reason:'portal_only',retry_after_ms:30000});return false;}
 // This must never sit behind a slow local upload: initial page requests arrive immediately.
 if (message.type === 'enabled') {
  read().then(s => reply({enabled:active(s, sender.tab?.id, sender.url),managed:s.managed?.[roleOf(sender.url,s.teamId)]===sender.tab?.id}), () => reply({enabled:false})); return true;
 }
 if (message.type === 'status') { read().then(s => reply({...s,bufferFull:!!s.bufferFull||volatileBufferFull,syncError:volatileError||s.syncError, queue:undefined, legacyQueue:undefined, rejected:undefined, queueCount:(s.queue || []).length, legacyQueueCount:(s.legacyQueue||[]).length,rejectedCount:(s.rejected || []).length,id:chrome.runtime.id})); return true; }
 serial(async () => {
  const s = await read();
  if (message.type === 'setAuto' && !sender.tab) {
   await save({autoEnabled:message.enabled === true});
   if (!message.enabled) await stopAll(s);
   return {ok:true};
  }
  const role = roleOf(sender.url, s.teamId);
  if (message.type === 'pause' && role && s.managed?.[role] === sender.tab?.id) {
   const pauses = {...s.pauses}, tabs = {...s.tabs};
   const roles = ['portal'];
   for (const item of roles) { pauses[item] = {until:Date.now() + Math.max(300000, Math.min(604800000, Number(message.retryAfter) || 0)),bucket:null,reason:String(message.reason || '页面访问受限').slice(0,180)}; if (s.managed?.[item]) tabs[s.managed[item]] = false; }
   await save({pauses,tabs});
   await Promise.all(roles.filter(r => s.managed?.[r]).map(r => signal(s.managed[r],false)));
   return {ok:true};
  }
  if (message.type === 'capture' && role && s.managed?.[role] === sender.tab?.id && (active(s,sender.tab.id,sender.url)||message.retry===true)) {
   const records = message.records;
   if (!Array.isArray(records) || records.length > 200 || records.some(r => !validRecord(r, s.teamId))) throw Error('仅接受本队总分、活动流水与赛事排行榜');
   const incoming=records.map(r=>({...r,id:r.id||crypto.randomUUID()}));
   if(incoming.some(r=>!/^[-a-zA-Z0-9]{8,80}$/.test(r.id)))throw Error('记录 ID 无效');
   const known=new Set((s.queue||[]).map(r=>r.id)),queue=[...(s.queue||[]),...incoming.filter(r=>!known.has(r.id))];
   try{
    if(!await hasCapacity(s,queue))throw Error('扩展缓存空间不足');
    await save({queue,bufferFull:false,syncError:null});volatileError=null;volatileBufferFull=false;
   }catch(e){
    const error='本次数据尚未缓存，保留原标签页自动重试：'+String(e.message).slice(0,100);
    await bufferFailure(error);return {ok:false,retryable:true,error};
   }
   return {ok:true,accepted:incoming.map(r=>r.id)};
  }
  if (message.type === 'pageStatus' && role && s.managed?.[role] === sender.tab?.id) { await save({pageStatus:{...s.pageStatus,[role]:{...s.pageStatus?.[role],...diagnostics(message.diagnostic),text:String(message.text).slice(0,180),at:new Date().toISOString()}}}); return {ok:true}; }
  if (message.type === 'boardStatus' && role && s.managed?.[role] === sender.tab?.id) {
   // Same cooldown rule as a portal pause (5 minutes to 7 days, Retry-After honoured), for the leaderboard only.
   const text=String(message.text).slice(0,180),pause=message.pause===true?{pause_until:Date.now()+Math.max(300000,Math.min(604800000,Number(message.retryAfter)||0)),reason:text}:{};
   await save({board:{...s.board,error:'',...diagnostics(message.diagnostic),text,at:new Date().toISOString(),...pause}}); return {ok:true};
  }
  if (message.type === 'sync' && !sender.tab) return {ok:true};
  return {ok:false};
 }).then(result => { reply(result); if (['setAuto','sync','capture'].includes(message.type) && result.ok) requestTick(false,message.type==='sync'||(message.type==='setAuto'&&message.enabled===true)); }, e => reply({ok:false,retryable:true,error:e.message}));
 return true;
});
requestTick();
