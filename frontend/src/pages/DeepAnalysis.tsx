import {useEffect,useMemo,useState} from 'react';
import {Button,Drawer,Empty,Segmented,Select,Space,Table,Tag,Tooltip} from 'antd';
import {useApi,type Row} from '../api';
import {Chart,fmt,time} from '../common';
import {positiveId,replyTreeOption,samplingLabels,samplingOption,samplingStageLabels,trajectoryOption,visibleReplyNodes} from './analysisModel';
import {bars,branchNodes,coverageHeatmap,interactionOption,percent,rankedRows,rankHeatmap,replyStats,stamp,threadScatter} from './deepAnalysisModel';
import {PostEvidence} from './Analysis';
const emptyRows:Row[]=[];

function Info({text}:{text:string}){return <Tooltip title={text}><span className="deep-info" tabIndex={0} aria-label={text}>ⓘ</span></Tooltip>;}
function Stat({label,value}:{label:string;value:any}){return <div><span>{label}</span><strong>{value??'—'}</strong></div>;}
function Plot({title,info,children,extra}:{title:string;info?:string;children:React.ReactNode;extra?:React.ReactNode}){return <section className="deep-plot"><div className="deep-plot-head"><h3>{title} {info&&<Info text={info}/>}</h3>{extra}</div>{children}</section>;}
function Interactions({id,timezone,extent,zoom,onZoom}:{id:number;timezone:string;extent:Row;zoom:Row;onZoom:(event:any)=>void}){
 const query=useApi<Row>(`/posts/${id}`),counters=query.data?.counters||[];
 const option=interactionOption(counters,timezone);option.xAxis={...option.xAxis,...extent};option.grid={...option.grid,left:64,right:22};option.dataZoom=[{type:'inside',...zoom}];
 return <Plot title="互动变化" info="与排名曲线共用时间范围；相邻有效快照的计数增量，各段间隔可能不同。缺失或计数下降保留为空。">{counters.length>1?<Chart ariaLabel="互动增量时间线" option={option} onEvents={{datazoom:onZoom}} height={230}/>:<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description={query.isError?'读取失败':query.isLoading?'加载中':'重复快照不足'}/>}</Plot>;
}
export default function DeepAnalysis({data,params,onUpdate,updating=false}:{data:Row;params:Row;onUpdate:(values:Row)=>void;updating?:boolean}){
 const focus=positiveId(params.focus_post_id),view=['ranks','replies','coverage'].includes(params.deep_view)?params.deep_view:'ranks',timezone=data.parameters?.bucket_timezone||'Australia/Sydney';
 const trending=data.trending||{},replies=data.replies||{},sampling=data.sampling||{},sort=params.rank_sort||'rise';
 const allRanks:Row[]=trending.trajectories||emptyRows,ranked=useMemo(()=>rankedRows(allRanks,sort,focus,12),[allRanks,sort,focus]),selectedRank=allRanks.find(r=>r.post_id===focus);
 const rankDistribution=useMemo(()=>rankHeatmap(ranked,timezone),[ranked,timezone]);
 const allNodes:Row[]=replies.nodes||[],threads:Row[]=replies.threads||[],sampleRows:Row[]=sampling.posts||[];
 const [drawer,setDrawer]=useState(false),[branch,setBranch]=useState<number|null>(null),[depth,setDepth]=useState(3),[samplePage,setSamplePage]=useState(1);
 const [zoom,setZoom]=useState<Row>({start:0,end:100});
 useEffect(()=>setZoom({start:0,end:100}),[focus]);
 const onZoom=(event:any)=>{const value=event.batch?.[0]||event;if(Number.isFinite(value.start)&&Number.isFinite(value.end))setZoom({start:value.start,end:value.end});};
 useEffect(()=>{setBranch(null);setDepth(3);},[replies.selected_root_id]);
 useEffect(()=>{if(!focus)setDrawer(false);const i=sampleRows.findIndex(r=>r.post_id===focus);setSamplePage(i<0?1:Math.floor(i/8)+1);},[focus,sampling.posts]);
 const choose=(value:unknown)=>{const id=positiveId(value);if(id)onUpdate({focus_post_id:id});};
 const scopedNodes=branchNodes(allNodes,branch),baseDepth=Math.min(...scopedNodes.map(n=>n.depth||0)),stats=replyStats(scopedNodes);
 const pathIds=new Set<number>(),byId=new Map(scopedNodes.map(n=>[n.post_id,n]));let cursor=focus?byId.get(focus):undefined;
 while(cursor&&!pathIds.has(cursor.post_id)){pathIds.add(cursor.post_id);cursor=byId.get(cursor.parent_id);}
 const treeNodes=visibleReplyNodes(scopedNodes.filter(n=>n.depth<=baseDepth+depth||pathIds.has(n.post_id)),focus,50);
 const chosen=selectedRank||sampleRows.find(r=>r.post_id===focus)||allNodes.find(n=>n.post_id===focus);
 const cells:Row[]=sampling.coverage?.cells||[],stages:Row[]=sampling.stages||[],statuses:Row[]=sampling.statuses||[];
 const summary=[30,60,180].map(minutes=>cells.filter(c=>c.minutes===minutes).reduce((acc,c)=>({...acc,eligible:acc.eligible+c.eligible,mature:acc.mature+c.mature,pending:acc.pending+c.pending}),{minutes,eligible:0,mature:0,pending:0}));
 const sampleClick=(e:any)=>{if(e.data?.status)onUpdate({sample_status:e.data.status,sample_stage:null});else if(e.data?.stage)onUpdate({sample_stage:e.data.stage==='total'?null:e.data.stage,sample_status:null});};
 const selectCell=(e:any)=>{if(e.data?.batch)onUpdate({sample_batch:e.data.batch,window_minutes:e.data.minutes,tolerance_minutes:e.data.minutes/5,sample_status:null,sample_stage:null});};
 const curveRows=useMemo(()=>selectedRank?[selectedRank]:ranked.slice(0,5),[selectedRank,ranked]);
 const peak=selectedRank?.points?.find((p:Row)=>p.rank===selectedRank.best_rank);
 const toPeak=peak?(Date.parse(peak.observed_at)-Date.parse(selectedRank!.first_observed_at))/60000:null;
 const extent=selectedRank&&Date.parse(selectedRank.last_observed_at)>Date.parse(selectedRank.first_observed_at)?{min:Date.parse(selectedRank.first_observed_at),max:Date.parse(selectedRank.last_observed_at)}:{};
 const baseRankCurve:any=useMemo(()=>trajectoryOption(curveRows,focus,timezone,allRanks),[curveRows,focus,timezone,allRanks]);
 const rankCurve={...baseRankCurve,xAxis:{...baseRankCurve.xAxis,...extent},dataZoom:[{type:'inside',...zoom},{type:'slider',height:16,bottom:0,...zoom}],grid:{...baseRankCurve.grid,bottom:60}};
 return <div className="deep-analysis" aria-busy={updating}>
  <div className="deep-toolbar"><Segmented aria-label="深度分析视图" value={view} options={[{label:'热门走势',value:'ranks'},{label:'回复传播',value:'replies'},{label:'采样覆盖',value:'coverage'}]} onChange={value=>onUpdate({deep_view:value})}/>{updating&&<span className="muted">更新中…</span>}</div>
  {focus&&<div className="deep-focus"><span><strong>#{focus}</strong> {chosen?.username||''}</span><span className="deep-focus-text">{chosen?.body_preview||''}</span><Space size={4}><Button size="small" onClick={()=>setDrawer(true)}>查看原帖</Button><Button size="small" type="text" onClick={()=>onUpdate({focus_post_id:null})}>清除选择</Button></Space></div>}
  {view==='ranks'&&<>
   <div className="deep-controls"><Select aria-label="排名排序" value={sort} options={[{value:'rise',label:'上升幅度'},{value:'peak',label:'最佳排名'},{value:'observations',label:'观测次数'}]} onChange={value=>onUpdate({rank_sort:value})}/><Select aria-label="搜索热门帖子" showSearch allowClear optionFilterProp="label" value={selectedRank?.post_id} placeholder="搜索帖子 / 作者" options={allRanks.map(r=>({value:r.post_id,label:`${r.username||''} #${r.post_id} · ${r.body_preview||''}`}))} onChange={value=>value?choose(value):onUpdate({focus_post_id:null})}/>{trending.truncated&&<Tag>明细 {allRanks.length}/{trending.total_posts}</Tag>}</div>
   <Plot title="热门排名分布" info="每格为该时段实际观测的最佳名次，空白表示没有观测。按所选排序展示前 12 篇，保留当前帖子。">
    {ranked.length?<Chart ariaLabel="热门排名热力图" height={Math.max(280,ranked.length*26+110)} option={rankDistribution} onEvents={{click:e=>choose(e.data?.post_id)}}/>:<Empty description="暂无热榜观测"/>}
   </Plot>
   <div className="deep-stats"><Stat label={selectedRank?'最佳排名':'热榜帖子'} value={selectedRank?`#${selectedRank.best_rank}`:fmt(trending.summary?.posts)}/><Stat label={selectedRank?'首末排名变化':'重复观测帖子'} value={selectedRank?selectedRank.observations>1?`${selectedRank.first_rank-selectedRank.last_rank>0?'+':''}${selectedRank.first_rank-selectedRank.last_rank} 位`:'—':fmt(trending.summary?.repeated_posts)}/><Stat label={selectedRank?'首次观测 → 峰值':'观测次数'} value={selectedRank?toPeak!==null&&Number.isFinite(toPeak)?`${toPeak.toFixed(0)} 分钟`:'—':fmt(trending.summary?.observations)}/></div>
   <Plot title="热门排名轨迹" info="仅连接相隔不超过 15 分钟的实际观测，缺采轮次断开。名次越小越靠上。" extra={focus&&!selectedRank?<Tag>所选帖无热榜观测</Tag>:<span className="muted">{selectedRank?`#${selectedRank.post_id}`:'排序前 5 篇'}</span>}>
    {curveRows.length?<Chart ariaLabel="热门排名轨迹" height={300} option={rankCurve} onEvents={{click:e=>choose(e.data?.post_id),datazoom:onZoom}}/>:<Empty/>}
    <div className="deep-legend">{curveRows.map((r,i)=><button key={r.post_id} onClick={()=>choose(r.post_id)}><i style={{background:['#26705b','#5789b7','#d49142','#8b73b3','#b86f79'][i]}}/>{r.username||'未知'} #{r.post_id}</button>)}</div>
   </Plot>
   {selectedRank&&<Interactions id={selectedRank.post_id} timezone={timezone} extent={extent} zoom={zoom} onZoom={onZoom}/>}
  </>}
  {view==='replies'&&<>
   <Plot title="对话规模与深度" info="圆点大小表示独立参与者数量，点击选择对话。" extra={replies.threads_truncated?<Tag>明细 {threads.length}/{replies.threads_total}</Tag>:null}><Chart ariaLabel="对话规模与深度散点图" height={250} option={threadScatter(threads,replies.selected_root_id)} onEvents={{click:e=>choose(e.data?.post_id)}}/></Plot>
   <div className="deep-controls"><Select aria-label="选择回复对话" showSearch optionFilterProp="label" value={replies.selected_root_id||undefined} placeholder="选择对话" options={threads.map(t=>({value:t.root_id,label:`${t.username||'未知'} #${t.root_id} · ${t.posts} 篇 / ${t.max_depth} 层`}))} onChange={choose}/><Select aria-label="定位回复节点" showSearch optionFilterProp="label" value={allNodes.some(n=>n.post_id===focus)?focus:undefined} placeholder="定位回复节点" options={allNodes.map(n=>({value:n.post_id,label:`#${n.post_id} ${n.username||'未采集'} · ${n.body_preview||''}`}))} onChange={choose}/><Button disabled={!focus||!allNodes.some(n=>n.post_id===focus)} onClick={()=>{setBranch(focus);setDepth(3);}}>聚焦所选分支</Button>{branch&&<Tag closable onClose={()=>setBranch(null)}>分支 #{branch}</Tag>}{replies.nodes_truncated&&<Tag>已返回 {allNodes.length}/{replies.total_nodes} 节点</Tag>}</div>
   <div className="deep-stats"><Stat label="参与者" value={stats.participants}/><Stat label="回复深度" value={stats.max_depth}/><Stat label="最大分支占比" value={percent(stats.largest_branch,stats.reply_count)}/><Stat label="回复间隔中位数" value={stats.median_gap===null?'—':`${stats.median_gap.toFixed(1)} 分钟`}/></div>
   <Plot title="回复传播树" info="空心节点为未采集父帖。结构指标基于已返回的范围内节点；回复间隔按父帖与回复的发布时间计算。" extra={<Space size={4}><span className="muted">{treeNodes.length} / {scopedNodes.length}</span><Button size="small" disabled={depth>=stats.max_depth} onClick={()=>setDepth(d=>d+2)}>展开两层</Button><Button size="small" disabled={depth===3} onClick={()=>setDepth(3)}>收起</Button></Space>}>
    {treeNodes.length?<Chart key={treeNodes.map(n=>`${n.post_id}:${n.parent_id}`).join('|')} ariaLabel="回复传播树" height={Math.min(680,Math.max(360,treeNodes.length*14))} option={replyTreeOption(treeNodes,focus)} onEvents={{click:e=>{if(!e.data?.virtual)choose(e.data?.post_id);}}}/>:<Empty description="暂无回复关系"/>}
   </Plot>
   <div className="deep-grid"><Plot title="各层帖子数"><Chart ariaLabel="回复层级分布" height={220} option={bars(stats.depths)}/></Plot><Plot title="回复时间分布"><Chart ariaLabel="回复时间分布" height={220} option={bars(stats.hours.map((r:Row)=>({...r,label:stamp(r.at,timezone)})),'#5789b7')}/></Plot></div>
   {stats.branches.length>0&&<Plot title="主要回复分支"><Chart ariaLabel="回复分支规模" height={220} option={bars(stats.branches.slice(0,12).map((r:Row)=>({...r,label:`#${r.post_id}`})))} onEvents={{click:e=>{choose(e.data?.post_id);setBranch(e.data?.post_id);setDepth(3);}}}/></Plot>}
  </>}
  {view==='coverage'&&<>
   <div className="deep-stats">{summary.map(s=><Stat key={s.minutes} label={`${s.minutes} 分钟覆盖 · ${s.eligible}/${s.mature}`} value={percent(s.eligible,s.mature)}/>)}</div>
   <Plot title="采样窗口覆盖" info="按发布时间分批；覆盖率 = 可用窗口 / 已过完整采样期限的帖子。灰格表示尚无到期样本。三个窗口容差分别为 ±6、±12、±36 分钟。点击筛选批次和窗口。">
    {cells.length?<Chart ariaLabel="采样窗口覆盖热力图" height={260} option={coverageHeatmap(cells,timezone)} onEvents={{click:selectCell}}/>:<Empty description="暂无采样批次"/>}
   </Plot>
   <Plot title="采样覆盖漏斗" info="绿色为通过，彩色为本步流失或等待。通过率和流失率以前一步为分母。点击筛选明细。" extra={<Select aria-label="采样窗口" value={Number(params.window_minutes)} options={Array.from(new Set([30,60,180,Number(params.window_minutes)])).map(n=>({value:n,label:`${n} 分钟`}))} onChange={value=>onUpdate({window_minutes:value,tolerance_minutes:value/5,sample_status:null,sample_stage:null})}/>}>
    <div className="deep-funnel"><Chart ariaLabel="采样覆盖漏斗" height={340} option={samplingOption(stages,statuses)} onEvents={{click:sampleClick}}/><div className="deep-stage-rates"><div><span>阶段</span><span>通过率</span><span>流失率</span></div>{stages.map((s,i)=>{const previous=i?stages[i-1].count:s.count;return <button key={s.key} onClick={()=>onUpdate({sample_stage:s.key==='total'?null:s.key,sample_status:null})}><span>{s.label}</span><strong>{percent(s.count,previous)}</strong><span>{percent(previous-s.count,previous)}</span></button>;})}</div></div>
    <div className="deep-statuses">{statuses.map(s=><Tag.CheckableTag key={s.key} checked={params.sample_status===s.key} onChange={checked=>onUpdate({sample_status:checked?s.key:null,sample_stage:null})}>{samplingLabels[s.key]} {fmt(s.count)}</Tag.CheckableTag>)}</div>
   </Plot>
   <div className="deep-grid"><Plot title="基线采集延迟" info="每档 2 分钟，包含全部有效早期基线。"><Chart ariaLabel="基线延迟分布" height={220} option={bars((sampling.coverage?.baseline_delays||[]).map((r:Row)=>({label:`${r.minutes}–${r.minutes+2}分`,count:r.count})))}/></Plot><Plot title="端点时间偏差" info="实际端点减目标时间，按分钟四舍五入；负数表示提前。"><Chart ariaLabel="端点偏差分布" height={220} option={bars((sampling.coverage?.endpoint_offsets||[]).map((r:Row)=>({label:`${r.minutes>0?'+':''}${r.minutes}分`,count:r.count})),'#5789b7')}/></Plot></div>
   <Plot title="采样明细" extra={<Space size={4}>{params.sample_batch&&<Tag closable onClose={()=>onUpdate({sample_batch:null})}>{stamp(params.sample_batch,timezone)}</Tag>}{(params.sample_status||params.sample_stage)&&<Tag closable onClose={()=>onUpdate({sample_status:null,sample_stage:null})}>{samplingLabels[params.sample_status]||samplingStageLabels[params.sample_stage]}</Tag>}{(params.sample_batch||params.sample_status||params.sample_stage)&&<Button size="small" onClick={()=>onUpdate({sample_status:null,sample_stage:null,sample_batch:null})}>清除采样筛选</Button>}<span className="muted">{sampleRows.length}/{fmt(sampling.posts_total)}</span></Space>}>
    <Table<Row> size="small" rowKey="post_id" dataSource={sampleRows} rowClassName={r=>r.post_id===focus?'analysis-selected-row':''} pagination={{current:samplePage,onChange:setSamplePage,pageSize:8,showSizeChanger:false,hideOnSinglePage:true}} scroll={{x:850}} columns={[{title:'帖子',dataIndex:'post_id',width:280,render:(id:number,r:Row)=><button className="analysis-post-cell" onClick={()=>choose(id)}><strong>{r.username||'未知'} #{id}</strong><span>{r.body_preview}</span></button>},{title:'状态',dataIndex:'status',render:(s:string)=>samplingLabels[s]||s},...[30,60,180].map(n=>({title:`${n} 分钟`,key:String(n),render:(_:unknown,r:Row)=>{const w=r.windows?.find((w:Row)=>w.minutes===n);return w?<Tooltip title={samplingLabels[w.status]}><span className={w.status==='eligible'?'deep-ok':'muted'}>{w.status==='eligible'?'✓':!w.mature?'待到期':'—'}</span></Tooltip>:'—';}})),{title:'基线',dataIndex:'baseline_at',render:time},{title:'端点',dataIndex:'endpoint_at',render:time}]}/>
   </Plot>
  </>}
  <Drawer title={`原帖 · #${focus||''}`} open={drawer&&focus!==null} onClose={()=>setDrawer(false)} size={540} destroyOnHidden>{drawer&&focus&&<PostEvidence id={focus} onSelect={choose}/>}</Drawer>
 </div>;
}
