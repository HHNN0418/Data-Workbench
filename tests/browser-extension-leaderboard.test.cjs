// Extension 1.4.0: read-only leaderboard capture (D104). Run: node --test tests/browser-extension-leaderboard.test.cjs
const test=require('node:test'),assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path'),{randomUUID}=require('node:crypto');
const {JSDOM}=require(path.join(__dirname,'..','frontend','node_modules','jsdom'));
const {DOMParser}=new JSDOM('').window;
// Values from a vm context have foreign prototypes; compare their JSON.
const plain=value=>JSON.parse(JSON.stringify(value));
const ORIGIN='https://community.capturethenarrative.com',TEAM=ORIGIN+'/user/teams/46',ID='a'.repeat(32);
const BOARD_HTML=`<html><body><table id="nav"><tr><th>Name</th><th>Link</th></tr><tr><td>Home</td><td>/</td></tr></table>
<table class="board"><thead><tr><th>#</th><th>Team</th><th>Members</th><th>Score</th></tr></thead><tbody>
<tr><td>1</td><td><a href="/user/teams/9">placeholder</a></td><td>4</td><td>44,128</td></tr>
<tr><td>2</td><td>C--</td><td>3</td><td>43,300</td></tr>
<tr><td>39</td><td><a href="/user/teams/46">Agent Zero</a></td><td>5</td><td>4,370</td></tr></tbody></table></body></html>`;

function page(options={}){
 const calls=[],records=[],boards=[],statuses=[],timers=[];
 const location={origin:ORIGIN,hostname:'community.capturethenarrative.com',pathname:'/user/teams/46',href:TEAM};
 const fetch=async(url,init)=>{
  calls.push({url,init});
  const target=new URL(url,location.href);
  if(target.pathname==='/user/competitions/2'){
   if(options.status)return {ok:false,status:options.status,url:target.href,headers:{get:name=>name==='Retry-After'?options.retryAfter||null:null},text:async()=>''};
   return {ok:true,status:200,url:options.redirect?ORIGIN+'/user/login':target.href,headers:{get:()=>null},text:async()=>options.html??BOARD_HTML};
  }
  return {ok:true,status:200,url:target.href,headers:{get:()=>null},text:async()=>String(url).endsWith('.csv')?'When (UTC),Type,Message\n':'<dl><dt>Score:</dt><dd>1</dd></dl>'};
 };
 const context={window:{fetch},location,DOMParser,URL,AbortSignal,Date,TextEncoder,setInterval:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearInterval:()=>{}};
 vm.runInNewContext(fs.readFileSync('browser-extension/page.js','utf8'),context);
 const collector=context.ctnCreatePortalCollector({records:value=>records.push(...value),status:value=>statuses.push(value),board:value=>boards.push(value)});
 return {calls,records,boards,statuses,timers,collector};
}
async function boardOnly(options){
 // Enabling starts the portal read at once; the leaderboard runs only when asked.
 const h=page(options);h.collector.enable(true);await new Promise(setImmediate);
 const before=h.calls.length;h.records.length=0;h.statuses.length=0;
 await h.collector.leaderboard();
 return {...h,boardCalls:h.calls.slice(before)};
}

test('leaderboard is one read-only same-origin GET that sends only the Team/Score table',async()=>{
 const h=await boardOnly();
 assert.equal(h.boardCalls.length,1);
 const [{url,init}]=h.boardCalls;
 assert.equal(url,'/user/competitions/2');assert.equal(init.credentials,'same-origin');assert.equal(init.cache,'no-store');assert.ok(!init.method||init.method==='GET');assert.ok(init.signal);
 assert.equal(h.records.length,1);const [record]=h.records;
 assert.equal(record.kind,'leaderboard');assert.ok(Date.parse(record.observed_at));
 assert.deepEqual(plain(Object.keys(record.data).sort()),['competition_id','rows','table_html']);assert.equal(record.data.competition_id,2);
 assert.match(record.data.table_html,/^<table class="board">/);assert.ok(!record.data.table_html.includes('Home'));
 assert.deepEqual(plain(record.data.rows),[{rank:1,team:'placeholder',score:44128},{rank:2,team:'C--',score:43300},{rank:39,team:'Agent Zero',score:4370}]);
 assert.equal(h.boards.at(-1).pause,false);assert.equal(h.boards.at(-1).diagnostic.rows,3);
 assert.ok(!h.statuses.some(s=>s.pause),'the portal status is untouched');
});

test('429 pauses only the leaderboard and honours Retry-After',async()=>{
 const h=await boardOnly({status:429,retryAfter:'900'});
 assert.equal(h.records.length,0);
 const status=h.boards.at(-1);assert.equal(status.pause,true);assert.equal(status.retryAfter,900000);assert.equal(status.diagnostic.code,'rate_limit');
 assert.ok(!h.statuses.some(s=>s.pause));
});

test('401/403 and a login redirect pause the leaderboard as an auth problem',async()=>{
 for(const options of [{status:401},{status:403},{redirect:true}]){
  const h=await boardOnly(options);
  assert.equal(h.records.length,0);const status=h.boards.at(-1);
  assert.equal(status.pause,true);assert.equal(status.diagnostic.code,'auth');assert.equal(status.retryAfter,300000);
 }
});

test('a page without a Team/Score table or with an oversized table sends nothing and does not pause',async()=>{
 for(const html of ['<table><tr><th>Name</th><th>Link</th></tr></table>',
  '<table><tr><th>Team</th><th>Score</th></tr>'+'<tr><td>x</td><td>1</td></tr>'.repeat(12000)+'</table>']){
  const h=await boardOnly({html});
  assert.equal(h.records.length,0);const status=h.boards.at(-1);
  assert.equal(status.pause,false);assert.equal(status.diagnostic.code,'schema');
 }
});

test('rough rows tolerate a moved score column, missing rank column and ties; unparseable rows give no rows',async()=>{
 const h=await boardOnly({html:'<table><tr><th>Points</th><th>Team name</th></tr><tr><td>1 200</td><td>B</td></tr><tr><td>1,200</td><td>C</td></tr></table>'});
 assert.deepEqual(plain(h.records[0].data.rows),[{rank:1,team:'B',score:1200},{rank:2,team:'C',score:1200}]);
 const empty=await boardOnly({html:'<table><tr><th>Team</th><th>Score</th></tr><tr><td>A</td><td>—</td></tr></table>'});
 assert.deepEqual(plain(empty.records[0].data.rows),[]);
});

test('disabled collector never fetches the leaderboard',async()=>{
 const h=page();await h.collector.leaderboard();assert.equal(h.calls.length,0);
});

function content(){
 const runtimeListeners=[],sent=[],calls=[];let sink;
 const location={origin:ORIGIN,hostname:'community.capturethenarrative.com',pathname:'/user/teams/46'};
 const chrome={runtime:{onMessage:{addListener:fn=>runtimeListeners.push(fn)},sendMessage:async m=>{sent.push(structuredClone(m));
  if(m.type==='enabled')return {enabled:true,managed:true};
  if(m.type==='capture')return {ok:true,accepted:m.records.map(r=>r.id)};return {ok:true};}}};
 const ctnCreatePortalCollector=value=>{sink=value;return {enable:()=>{},collect:()=>calls.push('collect'),leaderboard:()=>calls.push('leaderboard')};};
 vm.runInNewContext(fs.readFileSync('browser-extension/content.js','utf8'),{ctnCreatePortalCollector,chrome,location,crypto:require('node:crypto').webcrypto,setTimeout,clearTimeout});
 return {runtimeListeners,sent,calls,get sink(){return sink;},ask:type=>new Promise(resolve=>runtimeListeners[0]({type},{},resolve))};
}

test('content bridge runs the leaderboard on request and queues its record without touching portal status',async()=>{
 const c=content();await new Promise(setImmediate);
 assert.deepEqual(plain(await c.ask('leaderboard')),{ok:true});assert.deepEqual(c.calls,['leaderboard']);
 c.sink.board({text:'排行榜已读取',pause:false,diagnostic:{code:'pending',rows:3}});
 c.sink.records([{kind:'leaderboard',observed_at:new Date().toISOString(),data:{competition_id:2,table_html:'<table></table>',rows:[]}}]);
 await new Promise(setImmediate);await new Promise(setImmediate);
 const capture=c.sent.find(m=>m.type==='capture');assert.equal(capture.records[0].kind,'leaderboard');assert.ok(capture.records[0].id);
 assert.ok(c.sent.some(m=>m.type==='boardStatus'&&m.diagnostic.rows===3));
 assert.ok(!c.sent.some(m=>m.type==='pageStatus'),'a leaderboard capture is not reported as own-team data');
 c.sink.records([{kind:'post',data:{}}]);await new Promise(setImmediate);
 assert.equal(c.sent.filter(m=>m.type==='capture').length,1,'other kinds are still dropped');
});

function background(options={}){
 const state=structuredClone(options.state||{}),tabs=[{id:3,url:TEAM,status:'complete'}],signals=[],uploads=[];let clock=Date.now(),started=options.started!==false,listener;
 class ClockDate extends Date{constructor(...args){super(...(args.length?args:[clock]));}static now(){return clock;}}
 const chrome={storage:{local:{QUOTA_BYTES:10485760,get:async()=>structuredClone(state),getBytesInUse:async()=>0,set:async value=>{Object.assign(state,structuredClone(value));}}},
  alarms:{create:()=>{},onAlarm:{addListener:()=>{}}},runtime:{id:ID,getManifest:()=>({version:'1.4.0'}),onInstalled:{addListener:()=>{}},onStartup:{addListener:()=>{}},onMessage:{addListener:fn=>listener=fn}},
  tabs:{get:async id=>({...tabs.find(t=>t.id===id)}),query:async()=>structuredClone(tabs),create:async()=>{throw Error('no new tabs');},update:async()=>{},reload:async()=>{},
   sendMessage:async(id,m)=>{signals.push({id,...m});return m.type==='leaderboard'?{ok:started}:{ok:true};}}};
 const fetch=async(url,init={})=>{
  if(url.endsWith('/api/extension-status')&&!init.method)return {ok:true,json:async()=>({extension_id:ID,team_id:'46'})};
  if(url.endsWith('/api/bootstrap'))return {ok:true,json:async()=>({csrf_token:'token'})};
  if(url.endsWith('/api/browser-capture')){const body=JSON.parse(init.body);uploads.push(body);return {ok:true,json:async()=>({accepted:body.records.map(r=>r.id),errors:[]})};}
  return {ok:true,json:async()=>({})};
 };
 const context=vm.createContext({chrome,fetch,URL,AbortSignal,crypto:{randomUUID},Date:ClockDate,TextEncoder});
 vm.runInContext(fs.readFileSync('browser-extension/background.js','utf8'),context);
 return {state,signals,uploads,now:()=>clock,tick:()=>vm.runInContext('requestTick()',context),advance:ms=>clock+=ms,set started(v){started=v;},
  message:(m,sender={tab:{id:3},url:TEAM})=>new Promise(resolve=>listener(m,sender,resolve))};
}
const boards=h=>h.signals.filter(m=>m.type==='leaderboard').length;
const board=(extra={})=>({kind:'leaderboard',observed_at:new Date().toISOString(),data:{competition_id:2,table_html:'<table><tr><th>Team</th><th>Score</th></tr></table>',rows:[],...extra}});

test('background asks for the leaderboard every 10–15 minutes, retries a minute after a skipped request',async()=>{
 const h=background({state:{portalOnlyVersion:130}});await h.tick();await h.tick();assert.equal(boards(h),1);
 const next=h.state.board.next_at-h.now();assert.ok(next>=600000&&next<=900000,String(next));
 h.advance(599000);await h.tick();assert.equal(boards(h),1);
 h.advance(301000);h.started=false;await h.tick();assert.equal(boards(h),2);
 assert.ok(h.state.board.next_at-h.now()<=60000);h.started=true;
 h.advance(60001);await h.tick();assert.equal(boards(h),3);
});

test('a leaderboard pause honours Retry-After without pausing the portal',async()=>{
 const h=background({state:{portalOnlyVersion:130}});await h.tick();
 assert.deepEqual(plain(await h.message({type:'boardStatus',text:'排行榜页面受限 429',pause:true,retryAfter:3600000,diagnostic:{code:'rate_limit',http_status:429}})),{ok:true});
 assert.ok(h.state.board.pause_until-h.now()>=3599000);assert.equal(h.state.board.code,'rate_limit');
 assert.equal(h.state.pauses?.portal,undefined);assert.equal(h.state.tabs[3],true);
 const asked=boards(h);h.advance(900001);await h.tick();assert.equal(boards(h),asked,'still paused');
 assert.equal(h.state.tabs[3],true,'the portal keeps collecting');
 h.advance(2700000);await h.tick();assert.equal(boards(h),asked+1);
 // A short or missing Retry-After still waits the portal's minimum of five minutes.
 await h.message({type:'boardStatus',text:'排行榜需要登录或访问受限',pause:true,diagnostic:{code:'auth'}});
 assert.ok(h.state.board.pause_until-h.now()>=299000);
});

test('capture accepts valid leaderboard records, uploads them, and rejects malformed ones',async()=>{
 const h=background({state:{portalOnlyVersion:130}});await h.tick();
 const result=await h.message({type:'capture',records:[board()],retry:true});assert.equal(result.ok,true);
 await h.tick();assert.equal(h.uploads.at(-1).records[0].kind,'leaderboard');assert.equal((h.state.queue||[]).length,0);
 for(const bad of [board({competition_id:3}),board({table_html:'x'.repeat(300*1024+1)}),board({rows:'nope'}),board({table_html:42}),board({extra:'no'}),{...board(),observed_at:undefined},{kind:'leaderboard',data:{team_id:46}}]){
  const response=await h.message({type:'capture',records:[bad],retry:true});assert.equal(response.ok,false);assert.match(response.error,/排行榜/);
 }
 const other=await h.message({type:'capture',records:[board()]},{tab:{id:9},url:'https://example.com/'});assert.equal(other.ok,false);
});

test('portal-only migration keeps queued leaderboard records and archives only old public kinds',async()=>{
 const keep={id:'board-record-1',...board()},old={id:'old-counter-1',kind:'counter',data:{post_id:1}};
 const h=background({state:{managed:{portal:3},tabs:{3:true},queue:[keep,old]}});
 h.message({type:'status'},{});await h.tick();
 assert.equal(h.state.portalOnlyVersion,130);assert.deepEqual(plain(h.state.legacyQueue.map(r=>r.id)),[old.id]);
 assert.ok(h.uploads.some(b=>b.records.some(r=>r.id===keep.id)),'the leaderboard record is uploaded, not archived');
});

test('manifest 1.4.0 keeps the same permissions: the leaderboard is a same-origin read from the team page',()=>{
 const m=JSON.parse(fs.readFileSync('browser-extension/manifest.json','utf8'));
 assert.equal(m.version,'1.4.0');assert.deepEqual(m.permissions,['storage','alarms']);
 assert.deepEqual(m.host_permissions,['http://127.0.0.1:8765/*',ORIGIN+'/user/teams/*']);
 assert.ok(m.content_scripts.every(s=>s.world==='ISOLATED'&&JSON.stringify(s.matches)===JSON.stringify([ORIGIN+'/user/teams/*'])));
});
