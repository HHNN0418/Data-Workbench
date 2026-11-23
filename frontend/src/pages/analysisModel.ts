import type {EChartsCoreOption} from 'echarts/core';
import type {Row} from '../api';

export const analysisDefaults: Row = {window_minutes:60,tolerance_minutes:12,max_baseline_delay_minutes:10,bucket:'hour',bucket_timezone:'Australia/Sydney',own:'all',author_id:'',since:'',until:'',detail_limit:1000};

// Only detail payloads are selective. Every response still computes statistics
// over the complete scope; changing tabs never silently changes the sample.
export function analysisRequestParams(params: Row): Row {
 const scope=Object.fromEntries(Object.entries(params).filter(([key])=>!['analysis_tab','deep_view','rank_sort','ai_view'].includes(key)));
 const section=params.analysis_tab==='growth'?'growth':params.analysis_tab==='network'?'network':params.analysis_tab==='deep'?(params.deep_view==='replies'?'replies':params.deep_view==='coverage'?'sampling':'trending'):'core';
 return {...scope,sections:section,include_exportable:false};
}
// Exports and saved snapshots keep every account; the page shows the top ones.
export function analysisFullRequestParams(params:Row):Row {return {...analysisRequestParams(params),sections:'all',include_exportable:false,cohort_limit:10000};}
export const samplingLabels:Record<string,string> = {no_counters:'未采到计数',missing_created_at:'缺发布时间',no_early_baseline:'缺早期基线',waiting_endpoint:'等待窗口端点',missed_endpoint:'已错过窗口端点',missing_metrics:'窗口字段不全',negative_delta:'存在计数下降',eligible:'合资格样本'};
export const samplingStageLabels:Record<string,string> = {total:'全部范围内帖子',with_counters:'已有互动计数',early_baseline:'已有早期基线',window_matched:'已匹配窗口端点',complete_metrics:'窗口字段完整',nonnegative:'合资格非负样本'};
const colors=['#26705b','#5789b7','#d49142','#8b73b3','#b86f79','#67969b','#9a9161','#71806f'];
export function positiveId(value:unknown):number|null {if(value===null||value===undefined||String(value).trim()==='')return null;const n=Number(value);return Number.isSafeInteger(n)&&n>0?n:null;}
export function readAnalysisParams(search:URLSearchParams):Row {
 const result={...analysisDefaults};
 for(const key of Object.keys(analysisDefaults)) {const value=search.get(key);if(value===null||value.trim()==='')continue;if(typeof analysisDefaults[key]==='number'){const n=Number(value);if(Number.isFinite(n))result[key]=n;}else result[key]=value;}
 const focus=positiveId(search.get('focus_post_id'));if(focus)result.focus_post_id=focus;
 for(const key of ['analysis_tab','deep_view','rank_sort','sample_batch','ai_view','cohort_limit']){const value=search.get(key);if(value)result[key]=value;}
 const status=search.get('sample_status'),stage=search.get('sample_stage');if(status&&samplingLabels[status])result.sample_status=status;else if(stage&&samplingStageLabels[stage])result.sample_stage=stage;
 return result;
}
export function updateAnalysisSearch(search:URLSearchParams,changes:Row):URLSearchParams {const next=new URLSearchParams(search);for(const [key,value] of Object.entries(changes)){if(value===null||value===undefined||value==='')next.delete(key);else next.set(key,String(value));}return next;}
export function visibleTrajectories(rows:Row[],focus:number|null,limit=5):Row[] {
 const recent=[...rows].sort((a,b)=>(Date.parse(b.last_observed_at)||0)-(Date.parse(a.last_observed_at)||0));
 const visible=recent.slice(0,limit),selected=rows.find(row=>row.post_id===focus);
 if(selected&&!visible.some(row=>row.post_id===focus)){if(visible.length>=limit)visible.pop();visible.push(selected);}return visible;
}
export function visibleReplyNodes(nodes:Row[],focus:number|null,limit=40):Row[] {
 const byId=new Map(nodes.map(node=>[node.post_id,node])),selected=new Set<number>(),overrides=new Map<number,Row>();let omission:Row|undefined;
 const ancestors=(node:Row)=>{const path:Row[]=[],seen=new Set<number>();let current:Row|undefined=node;while(current&&!seen.has(current.post_id)){seen.add(current.post_id);path.push(current);current=byId.get(current.parent_id);}return path.reverse();};
 const focused=focus?byId.get(focus):undefined;
 if(focused) {const path=ancestors(focused);if(path.length>limit){const tail=path.slice(-(limit-2)),root=path[0];omission={post_id:-focused.post_id,parent_id:root.post_id,depth:(root.depth||0)+1,virtual:true,omitted:path.length-tail.length-1};selected.add(root.post_id);selected.add(omission.post_id);tail.forEach(node=>selected.add(node.post_id));overrides.set(tail[0].post_id,{...tail[0],parent_id:omission.post_id});}else path.forEach(node=>selected.add(node.post_id));}
 for(const node of [...nodes].sort((a,b)=>(a.depth||0)-(b.depth||0))) {if(selected.size>=limit)break;const path=ancestors(node).filter(item=>!selected.has(item.post_id));if(selected.size+path.length<=limit)path.forEach(item=>selected.add(item.post_id));}
 return [...nodes.filter(node=>selected.has(node.post_id)).map(node=>overrides.get(node.post_id)||node),...(omission?[omission]:[])];
}
export function trajectoryOption(rows:Row[],focus:number|null,timezone='Australia/Sydney',allRows:Row[]=rows):EChartsCoreOption {
 const observedTimes=new Set<number>();
 for(const row of allRows)for(const point of row.points||[]){const at=Date.parse(point.observed_at);if(Number.isFinite(at))observedTimes.add(at);}
 const times=[...observedTimes].sort((a,b)=>a-b),positions=new Map(times.map((at,index)=>[at,index]));
 // Preserve missing-round and long-gap breaks with one null per gap, instead
 // of materializing every other post's timestamps in each visible series.
 const sparsePoints=(row:Row)=>{
  const byTime=new Map<number,Row>();
  for(const point of row.points||[]){const at=Date.parse(point.observed_at);if(Number.isFinite(at))byTime.set(at,point);}
  const entries=[...byTime].sort(([a],[b])=>a-b),data:Row[]=[];
  const add=(at:number,rank:number|null)=>data.push({value:[at,rank],post_id:row.post_id});
  if(entries.length&&times[0]<entries[0][0])add(times[0],null);
  entries.forEach(([at,point],index)=>{
   const previous=entries[index-1]?.[0];
   if(previous!==undefined){const previousIndex=positions.get(previous),currentIndex=positions.get(at);const missing=previousIndex!==undefined&&currentIndex!==undefined&&currentIndex>previousIndex+1;
    if(missing||at-previous>15*60000)add(missing?times[previousIndex!+1]:(previous+at)/2,null);
   }
   add(at,point.rank??null);
  });
  if(entries.length&&times.at(-1)!>entries.at(-1)![0])add(times.at(-1)!,null);
  return data;
 };
 const stamp=(value:unknown)=>new Date(value as string).toLocaleString('zh-CN',{timeZone:timezone,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
 return {animation:false,color:colors,tooltip:{trigger:'item',renderMode:'richText',confine:true,formatter:(event:any)=>`${event.seriesName}\n${stamp(event.value[0])}\n榜单名次：${event.value[1]}`},legend:{show:false},grid:{left:64,right:22,top:24,bottom:40},xAxis:{type:'time',axisLabel:{fontSize:11,formatter:(value:number)=>stamp(value)},splitLine:{show:false}},yAxis:{type:'value',inverse:true,min:1,minInterval:1,name:'榜单名次',nameLocation:'middle',nameGap:42,splitLine:{lineStyle:{color:'#eef1eb'}}},series:rows.map((row,index)=>{const selected=row.post_id===focus;return {id:String(row.post_id),name:`${row.username||'未知账号'} · #${row.post_id}`,type:'line',connectNulls:false,showSymbol:true,symbolSize:selected?10:6,lineStyle:{width:selected?3:1.5,opacity:focus&&!selected?0.35:1},itemStyle:{color:colors[index%colors.length],opacity:focus&&!selected?0.45:1},emphasis:{focus:'series',scale:1.4},data:sparsePoints(row)};})};
}
export function replyTreeOption(nodes:Row[],focus:number|null):EChartsCoreOption {
 const byId=new Map<number,Row>();
 for(const node of nodes)byId.set(node.post_id,{...node,id:String(node.post_id),name:node.omitted?`省略 ${node.omitted} 层`:`${node.username|| (node.missing?'未采集父帖':'未知账号')}\n#${node.post_id}`,children:[],symbol:node.missing||node.virtual?'emptyCircle':'circle',symbolSize:node.post_id===focus?15:node.missing?9:10,itemStyle:{color:node.post_id===focus?'#d49142':node.missing?'#b9c1b2':'#26705b',borderWidth:node.post_id===focus?3:1,borderColor:node.post_id===focus?'#f4e4c7':'#fff'},label:{show:Boolean(nodes.length<=10||node.post_id===focus||!node.parent_id||node.virtual),...(!node.parent_id||node.post_id===focus?{position:'top',align:'center'}:{}),color:node.post_id===focus?'#8a5b1c':'#53614d',fontWeight:node.post_id===focus?'bold':'normal'}});
 const roots:Row[]=[];
 for(const node of byId.values()) {const parent=byId.get(node.parent_id);if(parent&&parent.post_id!==node.post_id){let ancestor:Row|undefined=parent;const seen=new Set<number>([node.post_id]);let cycle=false;while(ancestor){if(seen.has(ancestor.post_id)){cycle=true;break;}seen.add(ancestor.post_id);ancestor=byId.get(ancestor.parent_id);}if(!cycle){parent.children.push(node);continue;}}roots.push(node);}
 const tree=roots.length===1?roots:[{name:'已观测对话',virtual:true,children:roots,symbolSize:4,itemStyle:{color:'#a9b7a2'}}];
 return {animation:false,tooltip:{trigger:'item',renderMode:'richText',confine:true,formatter:(event:any)=>{const node=event.data;return node.virtual?(node.omitted?`中间 ${node.omitted} 层未在图中展开，详见关系明细`:'已观测回复关系'):`${node.username||'未知账号'} · #${node.post_id}\n${node.missing?'父帖尚未采集，仅展示已知关系':node.body_preview||'无文本内容'}\n${node.in_scope===false&&!node.missing?'位于当前筛选范围之外':'点击查看此帖证据'}`;}},series:[{type:'tree',data:tree,top:30,bottom:35,left:'12%',right:'22%',orient:'LR',roam:true,expandAndCollapse:false,initialTreeDepth:-1,edgeShape:'polyline',edgeForkPosition:'45%',lineStyle:{color:'#c5cfbf',width:1.4},label:{position:'left',align:'right',fontSize:11,distance:8,width:112,overflow:'truncate'},leaves:{label:{position:'right',align:'left'}},emphasis:{focus:'ancestor',itemStyle:{borderColor:'#d49142',borderWidth:3}}}]};
}
// Each clickable loss segment names the mutually exclusive reason it filters.
export function samplingOption(stages:Row[],statuses:Row[]):EChartsCoreOption {
 const counts=new Map(statuses.map(row=>[row.key,row.count]));
 const reasons:Record<string,string[]>={total:[],with_counters:['no_counters'],early_baseline:['missing_created_at','no_early_baseline'],window_matched:['waiting_endpoint','missed_endpoint'],complete_metrics:['missing_metrics'],nonnegative:['negative_delta']};
 return {animation:false,tooltip:{trigger:'item',renderMode:'richText',confine:true,formatter:(event:any)=>{const point=event.data;return `${point.label}\n${Number(point.value).toLocaleString('zh-CN')} 篇\n点击筛选：${point.status?samplingLabels[point.status]:samplingStageLabels[point.stage]}`;}},legend:{show:false},grid:{left:122,right:48,top:12,bottom:24},xAxis:{type:'value',minInterval:1,splitLine:{lineStyle:{color:'#eef1eb'}},axisLabel:{fontSize:11}},yAxis:{type:'category',inverse:true,axisTick:{show:false},axisLine:{show:false},data:stages.map(stage=>stage.label),axisLabel:{color:'#606b58',fontSize:11,lineHeight:16,formatter:(_value:string,index:number)=>`${stages[index].label}\n${Number(stages[index].count).toLocaleString('zh-CN')} 篇`}},series:[{type:'bar',name:'通过',stack:'coverage',barWidth:22,itemStyle:{color:'#51846c',borderRadius:3},label:{show:false},data:stages.map(stage=>({value:stage.count,label:stage.label,stage:stage.key}))},...Object.entries(samplingLabels).filter(([key])=>key!=='eligible').map(([key,label],index)=>({type:'bar',name:label,stack:'coverage',barWidth:22,itemStyle:{color:['#d8ba91','#dbbb9e','#d7c8a6','#c4ceda','#a6b8ce','#d7b2b6','#bd8992'][index]},data:stages.map(stage=>({value:reasons[stage.key]?.includes(key)?counts.get(key)||0:0,label,status:key}))}))]};
}

