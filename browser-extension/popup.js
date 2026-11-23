async function status(){
 const s=await chrome.runtime.sendMessage({type:'status'});
 const roles={portal:'本队积分 / 流水'};
 const page=s.pageStatus?.portal,stamp=value=>value?new Date(value).toLocaleString():'—';
 document.getElementById('status').textContent=[s.connection==='paired'?'已绑定 · '+(s.autoEnabled===false?'已暂停':s.collectionStatus?.label||'等待成功采集核验'):'等待连接',...Object.entries(s.managed||{}).filter(([role])=>role==='portal').map(([role,id])=>`${roles[role]}：${s.blocked?.[role]||s.pauses?.[role]?.reason||(s.tabs?.[id]?'已启用':'暂停')}`),`待入库 ${s.queueCount||0} 条 · 隔离 ${s.rejectedCount||0} 条`,s.connectionError||s.syncError||page?.error||'',`最近请求：${stamp(page?.last_attempt_at)}`,`成功采集：${stamp(s.collectionStatus?.last_collected_at)}`,`成功入库：${stamp(s.lastSync)}`,
  `排行榜：${s.board?.pause_until>Date.now()?'暂停至 '+stamp(s.board.pause_until)+'（'+(s.board.reason||'')+'）':s.board?.error||s.board?.text||'等待首次读取'} · 最近读取 ${stamp(s.board?.last_attempt_at)}`].filter(Boolean).join('\n');
}
document.getElementById('connect').onclick=async()=>{
 const url='http://127.0.0.1:8765/operations?pair_extension='+chrome.runtime.id;
 const tabs=await chrome.tabs.query({url:'http://127.0.0.1:8765/*'});
 const tab=tabs.find(t=>new URL(t.url).pathname==='/operations');
 if(tab)await chrome.tabs.update(tab.id,{url,active:true});else await chrome.tabs.create({url});
};
async function set(enabled){await chrome.runtime.sendMessage({type:'setAuto',enabled});await status();}
document.getElementById('start').onclick=()=>set(true);
document.getElementById('stop').onclick=()=>set(false);
document.getElementById('sync').onclick=async()=>{await chrome.runtime.sendMessage({type:'sync'});await status();};
status();setInterval(status,2000);
