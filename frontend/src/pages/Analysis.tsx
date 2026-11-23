import {useEffect, useRef, useState} from 'react';
import {Link, useSearchParams} from 'react-router-dom';
import {Alert, Button, Collapse, Descriptions, Form, Input, InputNumber, Select, Space, Table, Tabs, Tag, Typography, message} from 'antd';
import {useApi, useAction, useAnalysisRefresh, request, queryString, type Row} from '../api';
import {Panel, DataTable, Details, Metrics, QueryState, Chart, time, fmt} from '../common';
import {analysisDefaults, analysisFullRequestParams, analysisRequestParams, readAnalysisParams, updateAnalysisSearch} from './analysisModel';
import './analysis.css';
import DeepAnalysis from './DeepAnalysis';
import {LegacyRedirect} from '../nav/redirects';

function download(data:Row) {const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='ctn-analysis.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);}

export function PostEvidence({id,onSelect}:{id:number;onSelect:(id:number)=>void}) {
 const query=useApi<Row>(`/posts/${id}`,{},false);
 return <QueryState query={query}>{data=>{
  const post=data.post||{},counters:Row[]=data.counters||[],latest=counters.at(-1);
  return <div className="stack analysis-evidence">
   <Descriptions size="small" column={1} items={[{key:'author',label:'作者',children:post.username||`账号 ${post.author_id??'—'}`},{key:'created',label:'发布时间',children:time(post.created_at)},{key:'last',label:'最近采到',children:time(latest?.observed_at||post.last_seen)}]}/>
   <Typography.Paragraph className="analysis-evidence-body">{post.body||'此帖无文本内容'}</Typography.Paragraph>
   <div className="analysis-counter-grid">{[['like_count','点赞'],['reply_count','回复'],['repost_count','转发'],['view_count','浏览']].map(([key,label])=><div key={key}><span>{label}</span><strong>{fmt(latest?.[key])}</strong></div>)}</div>

   {(data.feed_observations||[]).length>0&&<div><h3>实际榜单观测</h3><Table<Row> size="small" rowKey={row=>`${row.observed_at}-${row.source}-${row.rank_position}`} pagination={{pageSize:6,hideOnSinglePage:true}} dataSource={data.feed_observations} columns={[{title:'采集时间',dataIndex:'observed_at',render:time},{title:'榜单',dataIndex:'source'},{title:'名次',dataIndex:'rank_position',render:fmt}]}/></div>}
   {counters.length>0&&<div><h3>采样证据</h3><Table<Row> size="small" rowKey={(row,index)=>String(row.id??index)} scroll={{x:480}} pagination={{pageSize:6,hideOnSinglePage:true}} dataSource={counters} columns={[{title:'采集时间',dataIndex:'observed_at',render:time},{title:'点赞',dataIndex:'like_count',render:fmt},{title:'回复',dataIndex:'reply_count',render:fmt},{title:'转发',dataIndex:'repost_count',render:fmt},{title:'浏览',dataIndex:'view_count',render:fmt}]}/></div>}
   {['reply_to_post_id','root_post_id'].filter(key=>post[key]&&post[key]!==id).map(key=><div key={key}>{key==='root_post_id'?'对话根帖':'回复父帖'} <Button type="link" onClick={()=>onSelect(post[key])}>#{post[key]}</Button></div>)}
   <Link to={`/posts?post=${id}`}>在帖子库查看完整记录 →</Link>
  </div>;
 }}</QueryState>;
}

export function AnalysisResults(props:{data:Row;params:Row;onUpdate:(values:Row)=>void;updating?:boolean}) {
 return <LegacyResults {...props}/>;
}

export default function Analysis() {
 const [search]=useSearchParams();
 return search.get('analysis_tab')==='ai'?<LegacyRedirect to="/ai/analysis" dropAnalysisTab/>:<StandardAnalysis/>;
}

function StandardAnalysis() {
 const [search,setSearch]=useSearchParams(),params=readAnalysisParams(search),[form]=Form.useForm(),[error,setError]=useState('');
 const mounted=useRef(true),latestSearch=useRef(search);
 latestSearch.current=search;
 useEffect(()=>{mounted.current=true;return()=>{mounted.current=false;};},[]);
 const apiParams=analysisRequestParams(params);
 const query=useApi<Row>('/analysis',apiParams,undefined,true),refresh=useAnalysisRefresh(),save=useAction<Row>('/analysis/run'),[notice,context]=message.useMessage();
 useEffect(()=>{form.setFieldsValue(readAnalysisParams(new URLSearchParams(search)));},[search,form]);
 const update=(values:Row)=>setSearch(updateAnalysisSearch(search,values));
 async function apply(persist=false) {try {
  const values={...analysisDefaults,...await form.validateFields()};
  if(values.tolerance_minutes>=values.window_minutes)throw new Error('窗口容差必须小于观察窗口');
  for(const field of ['since','until'])if(values[field]) {const date=new Date(values[field]);if(Number.isNaN(date.valueOf()))throw new Error('请填写有效的 ISO 时间，例如 2026-10-19T00:00:00+11:00');values[field]=date.toISOString();}
  if(values.since&&values.until&&values.since>values.until)throw new Error('起点不能晚于截止时间');
  const next=updateAnalysisSearch(search,values);setError('');
  if(persist){setSearch(next);await save.mutateAsync(analysisFullRequestParams(readAnalysisParams(next)));notice.success('分析快照已保存');}
  else {
   await refresh.mutateAsync(analysisRequestParams(readAnalysisParams(next)));
   if(mounted.current)setSearch(updateAnalysisSearch(latestSearch.current,values));
  }
 }catch(e){setError(e instanceof Error?e.message:'请检查筛选参数');}}
 return <div className="stack analysis-page">{context}<Panel title="分析范围" extra={<Button size="small" href="/api/export?table=analysis">已保存快照 CSV</Button>}>
  <Form form={form} layout="vertical" initialValues={params} onFinish={()=>void apply()} className="analysis-filter-form">
   <div className="analysis-filters"><Form.Item name="window_minutes" label="观察窗口 / 分钟" rules={[{required:true}]}><InputNumber min={1} max={20160} precision={0}/></Form.Item><Form.Item name="own" label="账号范围"><Select options={[{value:'all',label:'全部账号'},{value:'own',label:'已登记本队'},{value:'unknown',label:'归属未知'}]}/></Form.Item><Form.Item name="author_id" label="作者 ID"><Input placeholder="全部作者"/></Form.Item><div className="analysis-filter-actions"><Button type="primary" htmlType="submit" loading={query.isFetching||refresh.isPending}>更新分析</Button><Button onClick={()=>void apply(true)} loading={save.isPending} disabled={refresh.isPending}>保存快照</Button></div></div>
   <Collapse ghost items={[{key:'advanced',label:'时间范围与窗口口径',forceRender:true,children:<div className="analysis-advanced"><Form.Item name="since" label="发布起点 / ISO 时间"><Input placeholder="2026-10-19T00:00:00+11:00"/></Form.Item><Form.Item name="until" label="发布截止 / ISO 时间"><Input placeholder="留空为不限"/></Form.Item><Form.Item name="tolerance_minutes" label="端点容差 / 分钟" rules={[{required:true}]}><InputNumber min={0} max={20159} precision={0}/></Form.Item><Form.Item name="max_baseline_delay_minutes" label="最大基线延迟 / 分钟" rules={[{required:true}]}><InputNumber min={0} max={20160} precision={0}/></Form.Item><Form.Item name="bucket" label="趋势汇总单位"><Select options={[{value:'hour',label:'按小时'},{value:'day',label:'按日'}]}/></Form.Item><Form.Item name="bucket_timezone" label="显示 / 分桶时区"><Select options={[{value:'Australia/Sydney',label:'Australia/Sydney'},{value:'UTC',label:'UTC'}]}/></Form.Item><Form.Item name="detail_limit" label="返回明细上限" rules={[{required:true}]}><InputNumber min={1} max={10000} precision={0}/></Form.Item><Space wrap><Button size="small" href="/api/export?table=posts">帖子 CSV</Button><Button size="small" href="/api/export?table=counters">互动 CSV</Button></Space></div>}]} />
  </Form><p className="muted">页面每分钟自动更新分析；点击「更新分析」可立即纳入最新数据。</p>{error&&<Alert type="error" title={error} showIcon/>}
 </Panel>{query.isError&&query.data!==undefined&&<Alert type="warning" showIcon title="自动更新失败，暂时显示上次分析结果" description={query.error.message} action={<Button onClick={()=>void query.refetch()}>重试</Button>}/>}<QueryState query={{...query,isError:query.isError&&query.data===undefined}}>{data=><AnalysisResults data={data} params={params} onUpdate={update} updating={query.isFetching||refresh.isPending}/>}</QueryState></div>;
}
function LegacyResults({data,params,onUpdate,updating}:{data:Row;params:Row;onUpdate:(values:Row)=>void;updating?:boolean}) {
 const [exporting,setExporting]=useState(false),[exportError,setExportError]=useState('');
 async function exportResults(){setExporting(true);setExportError('');try{download(await request<Row>('/analysis?'+queryString(analysisFullRequestParams(params))));}catch(error){setExportError(error instanceof Error?error.message:'导出失败');}finally{setExporting(false);}}
 const series:Row[]=data.timeseries||[], growth:Row[]=data.growth||[], network=data.network||{};
 const stamp=(value:string)=>new Date(value).toLocaleString('zh-CN',{timeZone:data.parameters.bucket_timezone,month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
 const trend={tooltip:{trigger:'axis'},legend:{bottom:0},grid:{left:48,right:20,top:24,bottom:64},xAxis:{type:'category',data:series.map(row=>stamp(row.bucket_at))},yAxis:{type:'value',minInterval:1},series:[{name:'发布帖子',type:'bar',data:series.map(row=>row.posts_published),itemStyle:{color:'#0f766e'}},{name:'互动快照',type:'line',data:series.map(row=>row.snapshots),itemStyle:{color:'#64748b'}}]};
 const names:Record<string,string>={like_count:'点赞',reply_count:'回复',repost_count:'转发',view_count:'浏览'};
 const changes={...trend,series:Object.entries(names).map(([key,name])=>({name,type:'line',connectNulls:false,data:series.map(row=>row.counter_deltas?.[key]??null)}))};
 return <div className="stack">
  <Metrics items={[{label:'采样帖子',value:data.summary.posts},{label:'范围内账号',value:data.summary.accounts_in_scope},{label:'互动快照',value:data.summary.counter_snapshots},{label:'合资格窗口',value:data.summary.growth_sample_count}]}/>
  <Panel title="分析结果" extra={<Space wrap><span className="muted">分析更新于 {time(data.generated_at)}</span>{data.quality?.last_counter_observed_at&&<span className="muted">最近采集于 {time(data.quality.last_counter_observed_at)}</span>}{updating&&<span className="muted" role="status">正在更新分析…</span>}<Button loading={exporting} onClick={()=>void exportResults()}>导出结果 JSON</Button></Space>}>
   {exportError&&<Alert type="error" title={exportError} showIcon/>}
   {updating&&data.included_sections&&!data.included_sections.includes(analysisRequestParams(params).sections)&&<span role="status">正在加载分析明细…</span>}
   <Tabs activeKey={['trend','cohorts','growth','correlation','keywords','network','quality','deep'].includes(params.analysis_tab)?params.analysis_tab:'trend'} onChange={value=>onUpdate({analysis_tab:value})} destroyOnHidden items={[
    {key:'trend',label:'趋势',children:<div className="stack"><p className="muted">{data.parameters.bucket_timezone} · {data.parameters.bucket==='day'?'按日':'按小时'}汇总</p><Chart option={trend}/><h3>连续快照的计数变化</h3><Chart option={changes}/><Details title="时间序列与配对样本量" data={series}/></div>},
    {key:'cohorts',label:'账号对比',children:<><p className="muted">按发帖数展示 {fmt(data.cohorts?.length??0)} / {fmt(data.cohorts_total??data.cohorts?.length??0)} 个账号。各帖最新采集时刻可能不同；结合覆盖量与窗口样本量比较。{data.cohorts_truncated&&<Button type="link" size="small" onClick={()=>onUpdate({cohort_limit:10000})}>显示全部账号</Button>}</p><DataTable rows={data.cohorts||[]} columns={['author_id','username','team_label','posts','posts_with_counters','window_sample_count','interaction_window_sample_count','median_window_interactions','mean_window_interactions','latest_observed_totals','metric_coverage_posts']}/></>},
    {key:'growth',label:'窗口增长',children:<><p className="muted">展示 {growth.length} / {data.growth_total} 条。{data.growth_truncated?'明细已截断；汇总仍含全部样本。':''}缺失值不补零。</p><DataTable rows={growth.map(row=>({...row,...row.deltas}))} columns={['post_id','username','baseline_at','endpoint_at','baseline_delay_minutes','elapsed_minutes','interaction_delta','like_count','reply_count','repost_count','view_count','has_negative_delta']}/></>},
    {key:'correlation',label:'相关性',children:<><p className="muted">使用全部合资格样本，排除计数下降窗口。相关不代表因果。</p><DataTable rows={data.correlation||[]}/></>},
    {key:'keywords',label:'词频',children:<><p className="muted">Unicode 词元计数；中文连续文本未进行专业分词。</p><Chart option={{tooltip:{trigger:'axis'},grid:{left:120,right:24,bottom:36,top:12},xAxis:{type:'value',minInterval:1},yAxis:{type:'category',inverse:true,data:(data.keywords||[]).slice(0,15).map((r:Row)=>r.term)},series:[{type:'bar',data:(data.keywords||[]).slice(0,15).map((r:Row)=>r.count),itemStyle:{color:'#0f766e'}}]}} height={420}/><DataTable rows={data.keywords||[]} columns={['term','count','post_count']}/></>},
    {key:'network',label:'互动关系',children:<div className="stack"><p className="muted">帖子关系 {network.edges?.length??0} / {network.total_edges??0}，账号关系 {network.account_edges?.length??0} / {network.total_account_edges??0}。{network.truncated||network.account_edges_truncated?'明细已截断。':''}仅表示公开回复与转发关系。</p><h3>账号关系</h3><DataTable rows={network.account_edges||[]}/><h3>帖子关系</h3><DataTable rows={network.edges||[]}/></div>},
    {key:'quality',label:'数据质量',children:<div className="stack"><DataTable rows={Object.entries(data.quality||{}).filter(([,v])=>typeof v!=='object').map(([metric,value])=>({metric,value}))}/><Details title="缺失指标、排除原因与异常示例" data={data.quality}/><Collapse items={[{key:'limits',label:'分析口径与限制',children:<ul>{(data.caveats||[]).map((text:string)=><li key={text}>{text}</li>)}</ul>}]} /></div>},
    {key:'deep',label:'深度分析',children:<DeepAnalysis data={data} params={params} onUpdate={onUpdate} updating={updating}/>}
   ]}/>
   <Details title="本次参数与概要" data={{parameters:data.parameters,summary:data.summary}}/>
  </Panel>
 </div>;
}




