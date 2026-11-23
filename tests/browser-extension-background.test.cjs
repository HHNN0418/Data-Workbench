const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),{randomUUID}=require('node:crypto');
const ID='a'.repeat(32), TEAM='https://community.capturethenarrative.com/user/teams/46';
const targets=()=>[{id:1,url:'https://social.legitreal.com/feed?sort=trending'},{id:2,url:'https://social.legitreal.com/feed?sort=recent'},{id:3,url:TEAM},{id:4,url:'https://community.capturethenarrative.com/user/teams/47'},{id:5,url:'https://example.com/'}];
function harness(options={}){
 const state=structuredClone(options.state||{}),tabs=structuredClone(options.tabs||targets());
 const captureResult=options.captureResult;
 const calls={created:[],updated:[],reloaded:[],signals:[],uploads:[],heartbeat:[],requests:[]},listeners={};
 let paired=options.paired!==false,offline=false,nextId=100,block,failQueueWrites=options.failQueueWrites||false,clock=Date.now();
 class ClockDate extends Date{constructor(...args){super(...(args.length?args:[clock]));}static now(){return clock;}}
 const storageSize=value=>Object.entries(value).reduce((n,[k,v])=>n+Buffer.byteLength(k)+Buffer.byteLength(JSON.stringify(v)),0);
 const chrome={storage:{local:{QUOTA_BYTES:options.quotaBytes||10485760,get:async()=>structuredClone(state),getBytesInUse:async()=>storageSize(state),set:async value=>{if((failQueueWrites&&Object.hasOwn(value,'queue'))||storageSize({...state,...value})>(options.quotaBytes||10485760))throw Error('QUOTA_BYTES quota exceeded');Object.assign(state,structuredClone(value));}}},alarms:{create:()=>{},onAlarm:{addListener:fn=>listeners.alarm=fn}},runtime:{id:ID,getManifest:()=>({version:'1.3.2'}),onInstalled:{addListener:fn=>listeners.installed=fn},onStartup:{addListener:fn=>listeners.startup=fn},onMessage:{addListener:fn=>listeners.message=fn}},tabs:{
  get:async id=>{const t=tabs.find(t=>t.id===id);if(!t)throw Error('no tab');return {...t};},
  query:async()=>structuredClone(tabs.filter(t=>/^https:\/\/(social\.legitreal\.com\/|community\.capturethenarrative\.com\/user\/teams\/)/.test(t.url))),
  create:async arg=>{calls.created.push(arg);const t={id:nextId++,...arg};tabs.push(t);return {...t};},
  update:async(id,arg)=>{calls.updated.push({id,...arg});Object.assign(tabs.find(t=>t.id===id),arg);},
  reload:async id=>calls.reloaded.push(id),sendMessage:async(id,m)=>{calls.signals.push({id,...m});return {ok:true};}
 }};
 const fetch=async(url,options={})=>{
  calls.requests.push(url);
  if(offline)throw Error('offline');
  if(url.endsWith('/api/extension-status')&&!options.method)return {ok:true,json:async()=>({extension_id:paired?ID:null,team_id:paired?'46':null})};
  if(url.endsWith('/api/bootstrap')){if(block)await block;return {ok:true,json:async()=>({csrf_token:'token'})};}
  const body=JSON.parse(options.body);assert.equal(options.headers['X-CTN-Token'],'token');
  if(url.endsWith('/api/extension-permit'))return {ok:true,json:async()=>({allowed:true,retry_after_ms:0})};
  if(url.endsWith('/api/browser-capture')){calls.uploads.push(body);return {ok:true,json:async()=>captureResult?captureResult(body.records,calls.uploads.length):({accepted:body.records.map(r=>r.id),errors:[]})};}
  if(url.endsWith('/api/extension-status')){calls.heartbeat.push(body);return {ok:true};}
  throw Error('unexpected '+url);
 };
 const context=vm.createContext({chrome,fetch,URL,AbortSignal,crypto:{randomUUID},Date:ClockDate,TextEncoder});
 vm.runInContext(fs.readFileSync('browser-extension/background.js','utf8'),context);
 return {state,tabs,calls,listeners,tick:()=>vm.runInContext('requestTick()',context),idle:()=>vm.runInContext('chain',context),advance:ms=>clock+=ms,message:(m,sender={})=>new Promise(resolve=>listeners.message(m,sender,resolve)),set paired(v){paired=v},set offline(v){offline=v},set block(v){block=v},set failQueueWrites(v){failQueueWrites=v}};
}

test('binding reuses only our portal; existing social and unrelated tabs are untouched',async()=>{
 const h=harness();await h.tick();assert.equal(h.calls.created.length,0);assert.deepEqual(h.calls.reloaded,[3]);assert.deepEqual(h.state.managed,{portal:3});
 await h.tick();assert.deepEqual(h.calls.reloaded,[3]);assert.deepEqual(h.calls.heartbeat[0].pages.map(p=>p.role),['portal']);assert.equal(h.state.tabs[1],undefined);assert.equal(h.state.tabs[4],undefined);
});
test('creates only the missing portal in background, never a Feed page',async()=>{
 const h=harness({tabs:targets().filter(t=>t.id!==3)});await h.tick();assert.equal(h.calls.created.length,1);assert.equal(h.calls.created[0].active,false);assert.equal(h.calls.updated[0].url,TEAM);
 assert.equal((await h.message({type:'enabled'},{tab:{id:100},url:TEAM})).enabled,true);await h.tick();assert.equal(h.calls.created.length,1);
});
test('empty queue detects later binding; offline startup does not create pages',async()=>{
 const h=harness({paired:false,tabs:[]});await h.tick();assert.equal(h.calls.created.length,0);
 h.paired=true;h.offline=true;await h.tick();assert.equal(h.calls.created.length,0);
 h.offline=false;await h.tick();assert.equal(h.calls.created.length,1);assert.equal(h.calls.updated[0].url,TEAM);assert.equal(h.state.connection,'paired');
});
test('worker restart avoids reload storm; install or reload refreshes only portal',async()=>{
 const h=harness();await h.tick();const restarted=harness({state:h.state,tabs:h.tabs});await restarted.tick();assert.equal(restarted.calls.reloaded.length,0);
 restarted.listeners.installed();await restarted.tick();assert.deepEqual(restarted.calls.reloaded,[3]);
});
test('portal cooldown persists, expiration refreshes portal and global pause persists',async()=>{
 const h=harness();await h.tick();await h.message({type:'pause',reason:'429'},{tab:{id:3},url:TEAM});await h.tick();assert.equal(h.state.tabs[3],false);assert.deepEqual(h.calls.reloaded,[3]);
 h.state.pauses.portal.until=1;await h.tick();assert.deepEqual(h.calls.reloaded,[3]);
 await h.message({type:'setAuto',enabled:false});await h.tick();assert.deepEqual(h.state.tabs,{});const n=h.calls.reloaded.length;await h.tick();assert.equal(h.calls.reloaded.length,n);
 await h.message({type:'setAuto',enabled:true});await h.tick();assert.equal(h.state.tabs[3],true);
});
test('closed portal is replaced without touching existing Feed pages',async()=>{
 const h=harness();await h.tick();h.tabs.splice(h.tabs.findIndex(t=>t.id===3),1);await h.tick();assert.equal(h.calls.created.length,1);assert.equal(h.calls.updated[0].url,TEAM);
 assert.ok(!h.calls.updated.some(t=>t.id===1||t.id===2));assert.ok(!h.calls.reloaded.some(id=>id===1||id===2));
});
test('offline portal records retain IDs and drain in batches; public records and wrong team rejected',async()=>{
 const h=harness();await h.tick();h.offline=true;
 const records=Array.from({length:65},()=>({kind:'portal',observed_at:'2026-10-19T01:00:00Z',data:{team_id:46,total:'0',activity_csv:'When (UTC),Type,Message\n'}}));
 assert.equal((await h.message({type:'capture',records},{tab:{id:3},url:TEAM})).ok,true);await h.tick();assert.equal(h.state.queue.length,65);const ids=h.state.queue.map(r=>r.id);
 h.offline=false;await h.tick();assert.equal(h.state.queue.length,0);assert.deepEqual(h.calls.uploads.map(b=>b.records.length),[50,15]);assert.deepEqual(h.calls.uploads.flatMap(b=>b.records.map(r=>r.id)),ids);
 assert.equal((await h.message({type:'capture',records:[{kind:'post',data:{post_id:1}}]},{tab:{id:1},url:h.tabs[0].url})).ok,false);
 for(const kind of ['post','counter'])assert.ok((await h.message({type:'capture',records:[{kind,data:{team_id:46}}]},{tab:{id:3},url:TEAM})).error);
 assert.ok((await h.message({type:'capture',records:[{kind:'portal',data:{team_id:47}}]},{tab:{id:3},url:TEAM})).error);
});
test('initial portal enable reply never waits for backend upload',async()=>{
 const h=harness();await h.tick();let release;h.block=new Promise(r=>release=r);const pending=h.tick();await new Promise(setImmediate);
 const result=await h.message({type:'enabled'},{tab:{id:3},url:TEAM});assert.equal(result.enabled,true);release();await pending;
});
test('temporary and unclassified errors retain receipt IDs and wait before retrying, including worker restart',async()=>{
 for(const retryable of [true,undefined]){
  const record={id:'portal-retry-123',kind:'portal',observed_at:'2026-10-19T01:00:00Z',data:{team_id:46,total:'0',activity_csv:'When (UTC),Type,Message\n'}};
  const h=harness({state:{queue:[record]},captureResult:records=>({accepted:[],errors:records.map(r=>({id:r.id,error:'database is locked',...(retryable===undefined?{}:{retryable})}))})});
  await h.tick();assert.equal(h.calls.uploads.length,1);assert.equal(h.state.queue[0].id,record.id);assert.equal(h.state.rejected.length,0);
  assert.ok(h.state.syncRetryAfter>Date.now());assert.match(h.state.syncError,/30 秒后重试/);
  await h.tick();assert.equal(h.calls.uploads.length,1);
  const restarted=harness({state:h.state,tabs:h.tabs});await restarted.tick();assert.equal(restarted.calls.uploads.length,0);
  restarted.state.syncRetryAfter=1;await restarted.tick();assert.equal(restarted.calls.uploads.length,1);assert.equal(restarted.calls.uploads[0].records[0].id,record.id);assert.equal(restarted.state.queue.length,0);
 }
});
test('only explicit permanent errors are quarantined and unacknowledged records back off',async()=>{
 const records=['permanent-123','pending-123','success-123'].map(id=>({id,kind:'portal',data:{team_id:46}}));
 const h=harness({state:{queue:records},captureResult:()=>({accepted:['success-123'],errors:[{id:'permanent-123',error:'invalid CSV',retryable:false}]})});
 await h.tick();assert.deepEqual(h.state.queue.map(r=>r.id),['pending-123']);assert.deepEqual(h.state.rejected.map(r=>r.id),['permanent-123']);
 await h.tick();assert.equal(h.calls.uploads.length,1);
});
test('full offline cache pauses portal and resumes after pending records drain',async()=>{
 const h=harness();await h.tick();h.offline=true;h.state.queue=[{id:'saved-record',kind:'portal',data:{team_id:46,activity_csv:'x'.repeat(7_000_000)}}];
 assert.equal((await h.message({type:'capture',records:[{kind:'portal',data:{team_id:46}}]},{tab:{id:3},url:TEAM})).ok,false);
 assert.equal(h.state.bufferFull,true);assert.deepEqual(h.state.tabs,{});await h.tick();assert.deepEqual(h.state.tabs,{});
 h.offline=false;await h.tick();assert.equal(h.state.bufferFull,false);assert.equal(h.state.tabs[3],true);assert.equal(h.state.queue.length,0);
});
test('Retry-After is not cleared by manual resume',async()=>{
 const h=harness();await h.tick();await h.message({type:'pause',reason:'429',retryAfter:900000},{tab:{id:3},url:TEAM});
 const until=h.state.pauses.portal.until;assert.ok(until-Date.now()>890000);
 await h.message({type:'setAuto',enabled:true});await h.tick();assert.equal(h.state.pauses.portal.until,until);assert.equal(h.state.tabs[3],false);
});
test('login redirects retain original portal tab and recover after login without duplicates',async()=>{
 const h=harness();await h.tick();h.tabs.find(t=>t.id===3).url='https://community.capturethenarrative.com/user/login';
 await h.tick();await h.tick();assert.equal(h.calls.created.length,0);assert.equal(h.state.managed.portal,3);assert.equal(h.state.tabs[3],false);assert.ok(h.state.blocked.portal);
 h.tabs.find(t=>t.id===3).url=TEAM;await h.tick();assert.equal(h.state.tabs[3],true);assert.equal(h.state.blocked.portal,undefined);assert.equal(h.calls.reloaded.at(-1),3);
});
test('upgrade disables old public collectors without closing pages and archives pending public observations',async()=>{
 const old={id:'already-captured',kind:'counter',data:{post_id:42,like_count:3}};
 const h=harness({state:{managed:{trending:1,latest:2,portal:3},tabs:{1:true,2:true,3:true},queue:[old]}});await h.tick();
 assert.deepEqual(h.state.managed,{portal:3});assert.equal(h.state.legacyQueue[0].id,old.id);assert.equal(h.state.queue.length,0);assert.equal(h.calls.uploads.length,0);
 for(const id of [1,2]){assert.ok(h.calls.signals.some(s=>s.id===id&&!s.enabled&&!s.managed));assert.ok(h.tabs.some(t=>t.id===id));assert.ok(!h.calls.reloaded.includes(id));}
 await h.tick();assert.equal(h.state.legacyQueue.length,1);
});
test('Feed and counter permit requests are always denied without contacting a coordinator or public endpoint',async()=>{
 const h=harness();await h.tick();
 for(const path of ['/api/posts','/api/posts/25/counters']){
  const result=await h.message({type:'permit',path},{tab:{id:1},url:h.tabs[0].url});assert.equal(result.allowed,false);assert.equal(result.reason,'portal_only');
 }
 assert.ok(h.calls.requests.every(url=>url.startsWith('http://127.0.0.1:8765/')));
 assert.ok(!h.calls.requests.some(url=>url.endsWith('/api/extension-permit')));
});

test('manual sync asks the page for fresh data without reloading it and preserves cooldowns',async()=>{
 const h=harness({state:{portalOnlyVersion:130,pageStatus:{portal:{last_attempt_at:new Date().toISOString()}}}});await h.tick();
 h.calls.signals.length=0;const reloads=h.calls.reloaded.length;
 await h.message({type:'sync'});await h.idle();await new Promise(setImmediate);
 assert.ok(h.calls.signals.some(m=>m.type==='collect'&&m.force===true));assert.equal(h.calls.reloaded.length,reloads);
 await h.message({type:'pause',reason:'429',retryAfter:900000},{tab:{id:3},url:TEAM});await h.tick();
 const until=h.state.pauses.portal.until;h.calls.signals.length=0;
 await h.message({type:'sync'});await h.idle();await new Promise(setImmediate);
 assert.ok(!h.calls.signals.some(m=>m.type==='collect'));assert.equal(h.state.pauses.portal.until,until);
});

test('background alarm requests overdue collection when the page timer did not execute',async()=>{
 const h=harness({state:{portalOnlyVersion:130,pageStatus:{portal:{last_attempt_at:new Date().toISOString()}}}});await h.tick();
 h.calls.signals.length=0;await h.tick();assert.ok(!h.calls.signals.some(m=>m.type==='collect'));
 h.advance(120001);h.listeners.alarm({name:'sync'});await h.idle();await new Promise(setImmediate);
 assert.equal(h.calls.signals.filter(m=>m.type==='collect').length,1);
 // Sending a command is not reported as an actual HTTP attempt.
 assert.ok(Date.parse(h.state.pageStatus.portal.last_attempt_at)<h.state.lastCollectionRequest);
 h.advance(30001);await h.tick();assert.equal(h.calls.signals.filter(m=>m.type==='collect').length,2);
});

test('capacity accounts for UTF8 bytes and archived records without discarding them',async()=>{
 const archive={id:'archived-123',kind:'counter',data:{body:'x'.repeat(3_800_000)}};
 const h=harness({state:{portalOnlyVersion:130,legacyQueue:[archive]}});await h.tick();h.offline=true;
 const record={id:'stable-utf8-123',kind:'portal',observed_at:new Date().toISOString(),data:{team_id:46,total:'0',activity_csv:'汉'.repeat(2_300_000)}};
 const result=await h.message({type:'capture',records:[record],retry:true},{tab:{id:3},url:TEAM});
 assert.equal(result.ok,false);assert.equal(result.retryable,true);assert.match(result.error,/尚未缓存/);
 assert.equal((h.state.queue||[]).length,0);assert.equal(h.state.bufferFull,true);assert.equal(h.state.legacyQueue[0].data.body.length,3_800_000);
 const status=await h.message({type:'status'});assert.equal(status.bufferFull,true);assert.ok(status.syncError);
});

test('storage write failure returns a retryable error and retries keep the same receipt ID',async()=>{
 const h=harness({state:{portalOnlyVersion:130},failQueueWrites:true});await h.tick();h.offline=true;
 const record={id:'stable-quota-123',kind:'portal',observed_at:new Date().toISOString(),data:{team_id:46,total:'0',activity_csv:'When (UTC),Type,Message\n'}};
 const failed=await h.message({type:'capture',records:[record],retry:true},{tab:{id:3},url:TEAM});
 assert.equal(failed.ok,false);assert.equal(failed.retryable,true);assert.equal((h.state.queue||[]).length,0);
 h.failQueueWrites=false;
 const result=await h.message({type:'capture',records:[record],retry:true},{tab:{id:3},url:TEAM});await h.tick();
 assert.equal(result.ok,true);assert.deepEqual(result.accepted,[record.id]);assert.equal(h.state.queue[0].id,record.id);
 await h.message({type:'capture',records:[record],retry:true},{tab:{id:3},url:TEAM});await h.tick();assert.equal(h.state.queue.length,1);
 h.offline=false;await h.tick();assert.equal(h.state.queue.length,0);assert.equal(h.calls.uploads[0].records[0].id,record.id);
});

test('ordinary cooldown recovery resumes in place so unconfirmed content records survive',async()=>{
 const h=harness();await h.tick();const before=h.calls.reloaded.length;
 await h.message({type:'pause',reason:'temporary network issue'},{tab:{id:3},url:TEAM});
 h.state.pauses.portal.until=1;await h.tick();assert.equal(h.calls.reloaded.length,before);
 assert.equal(h.state.tabs[3],true);assert.ok(h.calls.signals.some(m=>m.type==='enable'&&m.enabled));
});
