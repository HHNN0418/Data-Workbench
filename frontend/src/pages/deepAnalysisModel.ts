import type {Row} from '../api';

export const stamp=(value:string|number,timezone='Australia/Sydney')=>new Date(value).toLocaleString('zh-CN',{timeZone:timezone,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
export const percent=(n:number,d:number)=>d?`${(100*n/d).toFixed(1)}%`:'—';
const grid={left:65,right:30,top:30,bottom:65};
const tooltip={trigger:'item',renderMode:'richText',confine:true};
export function rankedRows(rows:Row[],sort:string,focus:number|null,limit=20):Row[]{
 const sorted=[...rows].sort((a,b)=>sort==='peak'?a.best_rank-b.best_rank:sort==='observations'?b.observations-a.observations:((b.observations>1?b.first_rank-b.last_rank:-Infinity)-(a.observations>1?a.first_rank-a.last_rank:-Infinity))||a.best_rank-b.best_rank);
 const result=sorted.slice(0,limit),selected=rows.find(row=>row.post_id===focus);
 if(selected&&!result.includes(selected)){if(result.length===limit)result.pop();result.push(selected);}return result;
}
export function rankHeatmap(rows:Row[],timezone:string):any {
 const times=rows.flatMap(row=>(row.points||[]).map((p:Row)=>Date.parse(p.observed_at))).filter(Number.isFinite);
 const start=Math.min(...times),end=Math.max(...times),step=Math.max(60000,Math.ceil((end-start+1)/60/60000)*60000);
 const slots=times.length?Math.floor((end-start)/step)+1:0;
 const data:Row[]=[];
 rows.forEach((row,y)=>{const buckets=new Map<number,Row>();for(const point of row.points||[]){const at=Date.parse(point.observed_at);if(!Number.isFinite(at))continue;const x=Math.floor((at-start)/step),old=buckets.get(x);buckets.set(x,{value:[x,y,Math.min(old?.value[2]??Infinity,point.rank)],post_id:row.post_id,observations:(old?.observations||0)+1});}data.push(...buckets.values());});
 return {animation:false,tooltip:{...tooltip,formatter:(e:any)=>`${rows[e.value[1]].username||''} #${e.data.post_id}\n${stamp(start+e.value[0]*step,timezone)}\n时段最佳 #${e.value[2]} · ${e.data.observations} 次观测`},grid:{...grid,left:150,bottom:75},xAxis:{type:'category',data:Array.from({length:slots},(_,i)=>stamp(start+i*step,timezone)),axisLabel:{fontSize:10,hideOverlap:true},splitArea:{show:true,areaStyle:{color:['#fafbf9','#f4f6f2']}}},yAxis:{type:'category',inverse:true,data:rows.map(r=>`#${r.post_id} ${r.username||'未知'}`),axisLabel:{width:135,overflow:'truncate',fontSize:11},splitArea:{show:true}},visualMap:{min:1,max:Math.max(2,...data.map(p=>p.value[2])),orient:'horizontal',left:'center',bottom:0,itemWidth:12,itemHeight:150,text:['低排名','高排名'],inRange:{color:['#26705b','#85b39e','#e9f2eb']}},series:[{type:'heatmap',data,itemStyle:{borderWidth:1,borderColor:'#fff'},emphasis:{itemStyle:{borderColor:'#d49142',borderWidth:2}}}]};
}
export function threadScatter(threads:Row[],focusRoot:number|null):any {
 return {animation:false,grid,tooltip:{...tooltip,formatter:(e:any)=>`${e.data.username||'未知'} #${e.data.post_id}\n${e.value[0]} 篇 · ${e.value[1]} 层\n${e.data.participants} 位参与者`},xAxis:{type:'value',name:'帖子数',minInterval:1},yAxis:{type:'value',name:'回复深度',minInterval:1},series:[{type:'scatter',data:threads.filter(r=>r.replies>0||r.max_depth>0).map(r=>({...r,post_id:r.root_id,value:[r.posts,r.max_depth],symbolSize:Math.min(36,10+Math.sqrt(r.participants||1)*3),itemStyle:{color:r.root_id===focusRoot?'#d49142':'#26705b',opacity:r.root_id===focusRoot?1:.65}}))}]};
}
export function branchNodes(nodes:Row[],root:number|null):Row[]{
 if(!root)return nodes;const children=new Map<number,Row[]>();for(const node of nodes){const group=children.get(node.parent_id)||[];group.push(node);children.set(node.parent_id,group);}const ids=new Set<number>(),queue=[root];while(queue.length){const id=queue.pop()!;if(ids.has(id))continue;ids.add(id);for(const child of children.get(id)||[])queue.push(child.post_id);}return nodes.filter(node=>ids.has(node.post_id)).map(node=>node.post_id===root?{...node,parent_id:null}:node);
}
export function replyStats(nodes:Row[]):Row {
 const byId=new Map(nodes.map(n=>[n.post_id,n])),real=nodes.filter(n=>!n.missing&&!n.virtual&&n.in_scope!==false),depths=new Map<number,number>(),hours=new Map<string,number>(),gaps:number[]=[];
 const base=Math.min(...nodes.map(n=>n.depth||0));
 for(const n of real){const d=(n.depth||0)-base;depths.set(d,(depths.get(d)||0)+1);if(n.parent_id&&n.created_at){const ms=Date.parse(n.created_at);if(Number.isFinite(ms)){const hour=new Date(Math.floor(ms/3600000)*3600000).toISOString();hours.set(hour,(hours.get(hour)||0)+1);}const p=byId.get(n.parent_id);const gap=(ms-Date.parse(p?.created_at))/60000;if(Number.isFinite(gap)&&gap>=0)gaps.push(gap);}}
 const roots=nodes.filter(n=>!byId.has(n.parent_id)),root=roots[0];
 const sizes=new Map<number,number>();for(const n of [...nodes].sort((a,b)=>b.depth-a.depth)){const size=(sizes.get(n.post_id)||0)+(real.includes(n)?1:0);sizes.set(n.post_id,size);if(byId.has(n.parent_id))sizes.set(n.parent_id,(sizes.get(n.parent_id)||0)+size);}
 const branches=nodes.filter(n=>n.parent_id===root?.post_id).map(n=>({post_id:n.post_id,username:n.username,count:sizes.get(n.post_id)||0})).sort((a,b)=>b.count-a.count);
 gaps.sort((a,b)=>a-b);
 return {posts:real.length,participants:new Set(real.map(n=>n.author_id??n.username).filter(x=>x!=null)).size,max_depth:Math.max(0,...depths.keys()),largest_branch:branches[0]?.count||0,reply_count:real.filter(n=>n.parent_id).length,median_gap:gaps.length?(gaps[Math.floor((gaps.length-1)/2)]+gaps[Math.floor(gaps.length/2)])/2:null,depths:[...depths].sort((a,b)=>a[0]-b[0]).map(([value,count])=>({label:`第 ${value} 层`,count})),hours:[...hours].sort().map(([at,count])=>({at,count})),branches};
}
export function bars(rows:Row[],color='#26705b'):any {return {animation:false,grid:{...grid,bottom:45},tooltip:{trigger:'axis',renderMode:'richText',confine:true},xAxis:{type:'category',data:rows.map(r=>r.label),axisLabel:{fontSize:11,hideOverlap:true}},yAxis:{type:'value',minInterval:1},series:[{type:'bar',barMaxWidth:32,itemStyle:{color,borderRadius:[3,3,0,0]},data:rows.map(r=>({value:r.count,post_id:r.post_id}))}]};}
export function coverageHeatmap(cells:Row[],timezone:string):any {
 const batches=Array.from(new Set(cells.map(c=>c.batch))).sort();
 return {animation:false,tooltip:{...tooltip,formatter:(e:any)=>`${stamp(e.data.batch,timezone)} · ${e.data.minutes} 分钟\n覆盖 ${percent(e.data.eligible,e.data.mature)}（${e.data.eligible}/${e.data.mature} 已到期）\n未到期 ${e.data.pending} · 缺基线 ${e.data.missing_baseline}\n漏采端点 ${e.data.missed_endpoint} · 指标异常 ${e.data.invalid_metrics}`},grid:{...grid,left:70,bottom:80},xAxis:{type:'category',data:batches.map(at=>stamp(at,timezone)),axisLabel:{fontSize:10,hideOverlap:true}},yAxis:{type:'category',data:['30 分钟','60 分钟','180 分钟']},visualMap:{min:0,max:100,orient:'horizontal',left:'center',bottom:0,itemWidth:12,itemHeight:140,text:['100%','0%'],inRange:{color:['#eef3ee','#94bea8','#26705b']}},series:[{type:'heatmap',label:{show:batches.length<18,fontSize:11,formatter:(e:any)=>e.data.rate===null?'—':`${Math.round(e.data.rate*100)}%`},itemStyle:{borderWidth:3,borderColor:'#fff'},data:cells.map(c=>({...c,value:[batches.indexOf(c.batch),[30,60,180].indexOf(c.minutes),c.rate===null?-1:c.rate*100],...(c.rate===null?{itemStyle:{color:'#e8eaed'}}:{})}))}]};
}
export function interactionOption(counters:Row[],timezone:string):any {
 const sorted=[...counters].filter(r=>Number.isFinite(Date.parse(r.observed_at))).sort((a,b)=>Date.parse(a.observed_at)-Date.parse(b.observed_at));
 const fields=[['like_count','点赞'],['reply_count','回复'],['repost_count','转发']];
 return {animation:false,grid:{...grid,bottom:60},legend:{bottom:0},tooltip:{trigger:'axis',renderMode:'richText',confine:true},xAxis:{type:'time',axisLabel:{formatter:(v:number)=>stamp(v,timezone),fontSize:10,hideOverlap:true}},yAxis:{type:'value',name:'相邻快照增量'},series:fields.map(([key,name])=>({name,type:'line',connectNulls:false,showSymbol:true,data:sorted.map((r,i)=>{const previous=sorted[i-1];const valid=previous&&typeof previous[key]==='number'&&typeof r[key]==='number'&&r[key]>=previous[key];return [r.observed_at,valid?r[key]-previous[key]:null];})}))};
}
