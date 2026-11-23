const test=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');
function harness(options={}){
 const events=[],listeners=[],calls=[],timers=[];let clock=Date.now();
 class ClockDate extends Date{constructor(...args){super(...(args.length?args:[clock]));}static now(){return clock;}}
 const location={origin:'https://community.capturethenarrative.com',hostname:'community.capturethenarrative.com',pathname:'/user/teams/46',href:'https://community.capturethenarrative.com/user/teams/46',...options.location};
 const originalFetch=async(url,init)=>{
  calls.push({url,options:init});
  const csv=String(url).endsWith('.csv');
  return {ok:!options.denied,status:options.denied?429:200,url:new URL(url,location.href).href,headers:{get:()=>options.retryAfter||null},text:async()=>csv?(options.csv||'When (UTC),Type,Message\n2026-10-19T00:00:00Z,SCORE,"earned 5 points"\n2026-10-19T00:01:00Z,TEAMCODE,secret\n'):'<private html not emitted>'};
 };
 const window={postMessage:m=>events.push(m),addEventListener:(_,fn)=>listeners.push(fn),fetch:originalFetch};
 class XHR{}const originalOpen=XHR.prototype.open=function(){};const originalSend=XHR.prototype.send=function(){};
 class Parser{parseFromString(){return {querySelectorAll:selector=>selector==='dt'?[{textContent:'Score:',parentElement:{querySelector:()=>({textContent:options.total||' 0 '})}}]:[{getAttribute:()=>'/user/teams/46/activity.csv'}]};}}
 const context={window,location,XMLHttpRequest:XHR,DOMParser:Parser,URL,AbortSignal,Date:ClockDate,TextEncoder,setInterval:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearInterval:()=>{}};
 vm.runInNewContext(fs.readFileSync('browser-extension/page.js','utf8'),context);
 const collector=context.ctnCreatePortalCollector({records:records=>events.push({ctnRecords:records}),status:value=>events.push({...value,ctnStatus:value.text})});
 return {events,calls,timers,window,originalFetch,XHR,originalOpen,originalSend,advance:ms=>clock+=ms,collect:(force=false)=>collector.collect(force),enable:(enabled=true)=>collector.enable(enabled)};
}
test('portal-only script never hooks fetch or XHR and does nothing on social pages',async()=>{
 const h=harness({location:{origin:'https://social.legitreal.com',hostname:'social.legitreal.com',pathname:'/feed',href:'https://social.legitreal.com/feed?sort=recent'}});
 h.enable();await new Promise(setImmediate);assert.equal(h.calls.length,0);assert.equal(h.events.length,0);assert.equal(h.timers.length,0);
 assert.equal(h.window.fetch,h.originalFetch);assert.equal(h.XHR.prototype.open,h.originalOpen);assert.equal(h.XHR.prototype.send,h.originalSend);
});
test('portal emits only total and sanitized activity CSV using two read-only same-origin requests',async()=>{
 const h=harness();assert.equal(h.calls.length,0);h.enable();await new Promise(setImmediate);
 const records=h.events.flatMap(e=>e.ctnRecords||[]);assert.equal(records.length,1);const record=records[0];assert.equal(record.kind,'portal');assert.equal(record.data.total,'0');assert.equal(record.data.team_id,46);
 assert.ok(!JSON.stringify(record).includes('secret'));assert.ok(!JSON.stringify(record).includes('private html'));assert.equal(h.calls.length,2);
 assert.ok(h.calls.every(c=>!c.options.method||c.options.method==='GET'));assert.ok(h.calls.every(c=>c.options.credentials==='same-origin'));
 assert.deepEqual(h.calls.map(c=>new URL(c.url,'https://community.capturethenarrative.com').pathname),['/user/teams/46','/user/teams/46/activity.csv']);
 assert.equal(h.window.fetch,h.originalFetch);assert.equal(h.timers[0].ms,120000);
});
test('timer resamples portal automatically and disabling prevents subsequent reads',async()=>{
 const h=harness();h.enable();await new Promise(setImmediate);assert.equal(h.calls.length,2);
 h.advance(120000);await h.timers[0].fn();assert.equal(h.calls.length,4);assert.equal(h.events.flatMap(e=>e.ctnRecords||[]).length,2);
 h.enable(false);await h.timers[0].fn();assert.equal(h.calls.length,4);
});
test('429 pauses only portal and respects Retry-After',async()=>{
 const h=harness({denied:true,retryAfter:'900'});h.enable();await new Promise(setImmediate);
 assert.equal(h.calls.length,1);assert.equal(h.events.flatMap(e=>e.ctnRecords||[]).length,0);assert.ok(h.events.some(e=>e.pause&&e.retryAfter===900000));
});
test('malformed activity export produces no partial score snapshot',async()=>{
 const h=harness({csv:'Wrong,Header\nSECRET,CODE'});h.enable();await new Promise(setImmediate);
 assert.equal(h.events.flatMap(e=>e.ctnRecords||[]).length,0);assert.ok(h.events.some(e=>e.pause));
});
test('manifest uses only isolated scripts and grants only local backend and community portal',()=>{
 const m=JSON.parse(fs.readFileSync('browser-extension/manifest.json','utf8'));
 const portal='https://community.capturethenarrative.com/user/teams/*';assert.equal(m.version,'1.3.3');
 assert.ok(m.content_scripts.every(s=>s.world==='ISOLATED'));
 assert.deepEqual(m.content_scripts[0].js,['page.js','content.js']);
 assert.deepEqual(m.host_permissions,['http://127.0.0.1:8765/*',portal]);assert.ok(m.content_scripts.every(s=>JSON.stringify(s.matches)===JSON.stringify([portal])));
 assert.deepEqual(m.permissions,['storage','alarms']);assert.ok(!JSON.stringify(m).includes('social.legitreal.com'));
});
test('content bridge ignores social pages and never installs refresh timers',()=>{
 let messages=0;const listeners=[];const location={origin:'https://social.legitreal.com',hostname:'social.legitreal.com',pathname:'/feed'};
 const chrome={runtime:{onMessage:{addListener:fn=>listeners.push(fn)},sendMessage:()=>{messages++;return Promise.resolve({enabled:true});}}};
 vm.runInNewContext(fs.readFileSync('browser-extension/content.js','utf8'),{chrome,location});assert.equal(messages,0);assert.equal(listeners.length,0);
});

test('manual collection reads fresh data while timer and background commands cannot duplicate the same scheduled attempt',async()=>{
 const h=harness();h.enable();await new Promise(setImmediate);assert.equal(h.calls.length,2);
 h.collect();await new Promise(setImmediate);assert.equal(h.calls.length,2);
 h.advance(120000);h.collect();await h.timers[0].fn();await new Promise(setImmediate);assert.equal(h.calls.length,4);
 h.collect(true);await new Promise(setImmediate);assert.equal(h.calls.length,6);
 h.enable(false);h.collect(true);await new Promise(setImmediate);assert.equal(h.calls.length,6);
 const attempts=h.events.filter(e=>e.diagnostic?.code==='attempt');assert.equal(attempts.length,3);
 assert.ok(attempts.every(e=>e.diagnostic.last_attempt_at));
});

test('CSV cleaning preserves quoted newlines and a trailing carriage return inside a field',async()=>{
 const h=harness({csv:'When (UTC),Type,Message\n2026-10-19T00:00:00Z,SCORE,"line1\nends-with-cr\r"\n'});h.enable();await new Promise(setImmediate);
 const records=h.events.flatMap(e=>e.ctnRecords||[]);assert.equal(records.length,1);
 assert.ok(records[0].data.activity_csv.includes('"line1\nends-with-cr\r"'));
 assert.equal(h.events.find(e=>e.diagnostic?.code==='pending').diagnostic.activity_rows,1);
});

test('content bridge retains a rejected capture and retries the same ID before accepting a new collection',async()=>{
 const listeners=[],runtimeListeners=[],events=[],sent=[],timers=[];let reject=true;
 const location={origin:'https://community.capturethenarrative.com',hostname:'community.capturethenarrative.com',pathname:'/user/teams/46'};
 const window={postMessage:m=>events.push(m),addEventListener:(_,fn)=>listeners.push(fn)};
 const chrome={runtime:{onMessage:{addListener:fn=>runtimeListeners.push(fn)},sendMessage:async message=>{
  sent.push(structuredClone(message));
  if(message.type==='enabled')return {enabled:true,managed:true};
  if(message.type==='capture')return reject?{ok:false,retryable:true,error:'QUOTA_BYTES quota exceeded'}:{ok:true,accepted:message.records.map(r=>r.id)};
  return {ok:true};
 }}};
 let sink;
 const ctnCreatePortalCollector=value=>{sink=value;return {enable:enabled=>events.push({ctnControl:true,enabled}),collect:force=>events.push({ctnCollect:true,force})};};
 vm.runInNewContext(fs.readFileSync('browser-extension/content.js','utf8'),{ctnCreatePortalCollector,chrome,window,location,crypto:require('node:crypto').webcrypto,setTimeout:fn=>{timers.push(fn);return timers.length;},clearTimeout:()=>{}});
 await new Promise(setImmediate);
 assert.equal(listeners.length,0,'page messages must never reach the extension');
 sink.records([{kind:'portal',observed_at:new Date().toISOString(),data:{team_id:46,total:'0',activity_csv:'When (UTC),Type,Message\n'}}]);
 await new Promise(setImmediate);
 const first=sent.find(m=>m.type==='capture');assert.ok(first.records[0].id);assert.ok(events.some(e=>e.ctnControl&&e.enabled===false));assert.equal(timers.length,1);
 assert.ok(sent.some(m=>m.type==='pageStatus'&&m.diagnostic?.code==='buffer'));
 const controls=events.length;
 runtimeListeners[0]({type:'enable',enabled:true,managed:true},{},()=>{});await new Promise(setImmediate);
 assert.ok(!events.slice(controls).some(e=>e.ctnControl&&e.enabled===true));
 reject=false;const before=events.filter(e=>e.ctnCollect).length;let response;
 runtimeListeners[0]({type:'collect',force:true},{},value=>response=value);await new Promise(setImmediate);
 assert.equal(response.pending,true);assert.equal(events.filter(e=>e.ctnCollect).length,before);
 const attempts=sent.filter(m=>m.type==='capture');assert.equal(attempts.length,3);assert.ok(attempts.every(m=>m.records[0].id===first.records[0].id));
 runtimeListeners[0]({type:'collect',force:true},{},()=>{});assert.equal(events.filter(e=>e.ctnCollect).length,before+1);
});

test('same-page scripts cannot inject records, pause collection, or request collection through window messages',async()=>{
 const listeners=[],sent=[],collects=[];
 const location={origin:'https://community.capturethenarrative.com',hostname:'community.capturethenarrative.com',pathname:'/user/teams/46'};
 const window={postMessage:()=>{},addEventListener:(_,fn)=>listeners.push(fn)};
 const chrome={runtime:{onMessage:{addListener:()=>{}},sendMessage:async m=>{sent.push(m);return {enabled:true,managed:true};}}};
 vm.runInNewContext(fs.readFileSync('browser-extension/content.js','utf8'),{
  window,location,chrome,ctnCreatePortalCollector:()=>({enable:()=>{},collect:()=>collects.push(true)})
 });
 await new Promise(setImmediate);
 for(const data of [{ctnRecords:[{kind:'portal',data:{total:'999999',team_id:46}}]},{ctnStatus:'forged',pause:true},{ctnCollect:true,force:true},{ctnControl:true,enabled:true}]){
  for(const listener of listeners)listener({source:window,origin:location.origin,data});
 }
 await new Promise(setImmediate);
 assert.equal(listeners.length,0);
 assert.equal(collects.length,0);
 assert.deepEqual(sent.map(m=>m.type),['enabled']);
});

test('isolated collector and content queue complete a capture without any page messaging',async()=>{
 const sent=[],runtimeListeners=[],network=[],intervals=[];
 const location={origin:'https://community.capturethenarrative.com',hostname:'community.capturethenarrative.com',pathname:'/user/teams/46',href:'https://community.capturethenarrative.com/user/teams/46'};
 const window={fetch:async(url,options)=>{
  network.push({url,options});return {ok:true,status:200,url:new URL(url,location.href).href,
   text:async()=>String(url).endsWith('.csv')?'When (UTC),Type,Message\n2026-10-19T00:00:00Z,SCORE,earned 5 points\n':'page'};
 },postMessage:()=>{throw Error('page bridge forbidden');},addEventListener:()=>{throw Error('page bridge forbidden');}};
 class Parser{parseFromString(){return {querySelectorAll:selector=>selector==='dt'?[{textContent:'Score:',parentElement:{querySelector:()=>({textContent:'105'})}}]:[{getAttribute:()=>'/user/teams/46/activity.csv'}]};}}
 const chrome={runtime:{onMessage:{addListener:fn=>runtimeListeners.push(fn)},sendMessage:async m=>{
  sent.push(structuredClone(m));
  if(m.type==='enabled')return {enabled:true,managed:true};
  if(m.type==='capture')return {ok:true,accepted:m.records.map(r=>r.id)};
  return {ok:true};
 }}};
 const context=vm.createContext({window,location,chrome,DOMParser:Parser,URL,AbortSignal,Date,TextEncoder,
  crypto:require('node:crypto').webcrypto,setInterval:fn=>{intervals.push(fn);return intervals.length;},clearInterval:()=>{},clearTimeout:()=>{},setTimeout});
 vm.runInContext(fs.readFileSync('browser-extension/page.js','utf8'),context);
 vm.runInContext(fs.readFileSync('browser-extension/content.js','utf8'),context);
 await new Promise(setImmediate);
 const captures=sent.filter(m=>m.type==='capture');assert.equal(captures.length,1);
 assert.equal(captures[0].records[0].data.total,'105');
 assert.equal(captures[0].records[0].data.team_id,46);
 assert.equal(network.length,2);
 assert.ok(sent.some(m=>m.type==='pageStatus'&&m.text.includes('已缓存')));
 runtimeListeners[0]({type:'collect',force:true},{},()=>{});
 await new Promise(setImmediate);
 assert.equal(sent.filter(m=>m.type==='capture').length,2);
});
