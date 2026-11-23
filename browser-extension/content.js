(()=>{
 if(location.hostname!=='community.capturethenarrative.com'||!/^\/user\/teams\/\d+\/?$/.test(location.pathname))return;
 let enabled=false,managed=false,resolved=false,delivering=false,retryTimer;
 const pending=new Map();
 const collector=globalThis.ctnCreatePortalCollector({
  records(records){
   if(!enabled)return;
   if(Array.isArray(records)&&records.length<=200&&records.every(record=>['portal','leaderboard'].includes(record?.kind))){
    for(const record of records){const id=crypto.randomUUID();pending.set(id,{...record,id});}
    // Pause new collection until the durable extension queue acknowledges it.
    setEnabled(false);
    void flush();
   }
  },
  status({text,pause,retryAfter,diagnostic}){
   if(!enabled)return;
   chrome.runtime.sendMessage({type:'pageStatus',text,diagnostic}).catch(()=>{});
   if(pause){setEnabled(false);chrome.runtime.sendMessage({type:'pause',reason:text,retryAfter}).catch(()=>{});}
  },
  // Leaderboard outcomes pause only the leaderboard (background.js), never the portal collection.
  board({text,pause,retryAfter,diagnostic}){
   if(enabled)chrome.runtime.sendMessage({type:'boardStatus',text,pause,retryAfter,diagnostic}).catch(()=>{});
  }
 });
 function setEnabled(value,isManaged){
  managed=isManaged===undefined?(managed||value):isManaged;
  resolved=true;enabled=value;
  collector.enable(enabled);
 }
 async function flush(){
  if(delivering||!pending.size)return;delivering=true;clearTimeout(retryTimer);
  const portal=[...pending.values()].some(record=>record.kind==='portal');
  try{
   const result=await chrome.runtime.sendMessage({type:'capture',records:[...pending.values()],retry:true});
   if(!result?.ok||!Array.isArray(result.accepted))throw Error(result?.error||'本地缓存尚未确认，保留本次数据重试');
   for(const id of result.accepted)pending.delete(id);
   if(pending.size)throw Error('部分本队记录尚未确认，保留原记录重试');
   if(portal)await chrome.runtime.sendMessage({type:'pageStatus',text:'本队数据已缓存，等待数据库确认',diagnostic:{code:'pending',error:''}}).catch(()=>{});
   const state=await chrome.runtime.sendMessage({type:'enabled'});
   setEnabled(state?.enabled===true,state?.managed===true);
  }catch(e){
   setEnabled(false);
   await chrome.runtime.sendMessage({type:'pageStatus',text:'本次数据尚未保存；请保留此标签页，正在重试',diagnostic:{code:'buffer',error:String(e.message).slice(0,180)}}).catch(()=>{});
   retryTimer=setTimeout(flush,30000);
  }finally{delivering=false;}
 }
 chrome.runtime.onMessage.addListener((m,sender,reply)=>{
  if(m.type==='enable'){setEnabled(m.enabled===true&&!pending.size,m.managed);if(pending.size)void flush();reply({ok:true});}
  if(m.type==='collect'){
   if(pending.size){void flush();reply({ok:true,pending:true});}
   else{if(enabled)void collector.collect(m.force===true);reply({ok:enabled});}
  }
  if(m.type==='leaderboard'){const ok=enabled&&!pending.size;if(ok)void collector.leaderboard?.();reply({ok});}
 });
 chrome.runtime.sendMessage({type:'enabled'}).then(r=>{if(!resolved)setEnabled(r?.enabled===true,r?.managed===true);}).catch(()=>setEnabled(false));
})();
