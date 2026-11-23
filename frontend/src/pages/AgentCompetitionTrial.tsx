import {useEffect,useState} from 'react';
import {Link} from 'react-router-dom';
import {useQueryClient} from '@tanstack/react-query';
import {Alert,Button,Collapse,Empty,InputNumber,Select,Skeleton,Space,Switch,Table,Tag} from 'antd';
import {useAction,useApi} from '../api';
import {time} from '../common';
import {providerReady,type Settings as ProviderSettings} from './aiProviders';
import './agentCompetitionTrial.css';

type Mode='off'|'observe'|'resources'|'models';
type TrialAgent={id:string;name:string;enabled:boolean;mode:'review'|'auto';provider_id?:string;account_id?:number|null;account_state?:string;allowed_actions?:string[]};
type CompetitionSettings={mode:Mode;enhanced_provider_id:string|null;premium_provider_id?:string|null;enhanced_hourly_calls:number;enhanced_max_output_tokens?:number};
type CompetitionResult={
 agent_id:string;name?:string;account_id?:number|null;status:string;reasons?:string[];rank?:number|null;
 score:number|null;known_component_subtotal?:number|null;eligible_posts:number;due_posts:number;coverage:number|null;qualified:boolean;
 round_id?:string|null;
};
type CompetitionAgent=CompetitionResult&{
 first_ready_at?:number|null;protected_until?:number|null;protection_remaining_seconds?:number;
 share:number|null;target_share?:number|null;model_tier:number;enhanced_ratio:number;
};
type Round={round_id:string;start_at:number;end_at:number;sealed_at:number;revision:number;results:CompetitionResult[]};
export type CompetitionSnapshot={
 kind:'agent_competition';revision:number;settings:CompetitionSettings;enabled:boolean;generated_at:number;
 data_updated_at?:number|null;last_settled_at:number|null;allocation_epoch:number|null;agents:CompetitionAgent[];rounds:Round[];
 daily_views:{date:string;observed_views:number;credited_views:number;cap:number}[];
 notes:string[];metric_weights:Record<string,number>;excluded_metrics:string[];
};
const modes:Record<Mode,string>={off:'关闭竞赛',observe:'只观察计分',resources:'分配发文资源',models:'分配资源与模型'};
const statuses:Record<string,string>={off:'竞赛已关闭',protected:'新人保护中',observing:'样本积累中',eligible:'参与本轮排名',no_output:'本轮无产出',insufficient_data:'采样不足',inactive:'未参与自动运行',duplicate_account:'账号重复绑定',awaiting_model:'等待可用模型'};
const statusHints:Record<string,string>={protected:'保护期间不参与模型升降，保留基础和探索机会。',observing:'近期成熟样本不足，继续积累；缺失不计零分。',no_output:'本轮没有到期成果，不视为采集失败。',insufficient_data:'本轮观测不完整，暂不能把成绩用于排名。',awaiting_model:'先配置可用的角色文字模型。'};
const tierNames=['标准','奖励','加强'],connectionTiers:Record<string,string>={standard:'标准',enhanced:'增强',premium:'顶级'};
function numeric(value:unknown):value is number{return typeof value==='number'&&Number.isFinite(value);}
function record(value:unknown):value is Record<string,unknown>{return !!value&&typeof value==='object'&&!Array.isArray(value);}
function isSnapshot(value:unknown):value is CompetitionSnapshot{
 if(!record(value)||value.kind!=='agent_competition'||!Number.isInteger(value.revision)||!record(value.settings)
  ||!Object.hasOwn(modes,String(value.settings.mode))||typeof value.enabled!=='boolean'||!numeric(value.generated_at)
  ||!Array.isArray(value.agents)||!Array.isArray(value.rounds)||!Array.isArray(value.notes))return false;
 if(!(value.settings.enhanced_provider_id===null||typeof value.settings.enhanced_provider_id==='string')
  ||!numeric(value.settings.enhanced_hourly_calls))return false;
 const validResult=(row:unknown)=>record(row)&&typeof row.agent_id==='string'&&typeof row.status==='string'
  &&(row.score===null||numeric(row.score))
  &&numeric(row.eligible_posts)&&numeric(row.due_posts)&&(row.coverage===null||numeric(row.coverage))
  &&typeof row.qualified==='boolean'
  &&(row.reasons===undefined||Array.isArray(row.reasons)&&row.reasons.every(item=>typeof item==='string'));
 const validCurrentRow=(row:unknown)=>validResult(row)&&record(row)&&(row.share===null||numeric(row.share))
  &&numeric(row.model_tier)&&numeric(row.enhanced_ratio);
 return value.agents.every(validCurrentRow)&&value.rounds.every(round=>record(round)&&typeof round.round_id==='string'
  &&numeric(round.start_at)&&numeric(round.end_at)&&Array.isArray(round.results)&&round.results.every(validResult))
  &&value.notes.every(note=>typeof note==='string');
}
function at(value:unknown){return numeric(value)&&value>0?time(new Date(value*1000).toISOString()):'—';}
function number(value:unknown){return numeric(value)?value.toLocaleString('zh-CN',{maximumFractionDigits:2}):'—';}
function percent(value:unknown){return numeric(value)?`${(value*100).toLocaleString('zh-CN',{maximumFractionDigits:1})}%`:'—';}
function duration(seconds:number){const remaining=Math.max(0,Math.ceil(seconds));return `${Math.floor(remaining/60)} 分 ${remaining%60} 秒`;}
function preparation(agent:TrialAgent,teamEnabled:boolean){
 const reasons:string[]=[];
 if(!teamEnabled)reasons.push('队伍持续运行已暂停');
 if(!agent.enabled)reasons.push('角色已暂停');
 if(agent.mode!=='auto')reasons.push('当前为审阅模式');
 if(!agent.account_id)reasons.push('尚未连接账号');
 else if(agent.account_state!=='ready')reasons.push('账号就绪状态待确认');
 if(agent.allowed_actions&&!agent.allowed_actions.some(action=>['post','reply','repost'].includes(action)))reasons.push('未开启发文动作');
 return reasons.length?reasons.join('；'):'账号与动作条件就绪，AI 服务需核对';
}

export default function AgentCompetitionTrial({agents,teamEnabled}:{agents:TrialAgent[];teamEnabled:boolean}){
 const query=useApi<unknown>('/agents/competition',{},5000),providersQuery=useApi<ProviderSettings>('/ai/providers');
 const action=useAction<unknown>('/agents/competition'),client=useQueryClient();
 const [error,setError]=useState(''),[notice,setNotice]=useState(''),[selectedRound,setSelectedRound]=useState('');
 const [editor,setEditor]=useState<{expected_revision:number;settings:CompetitionSettings}|null>(null),[now,setNow]=useState(Date.now());
 useEffect(()=>{const timer=window.setInterval(()=>setNow(Date.now()),1000);return ()=>window.clearInterval(timer);},[]);
 const data=isSnapshot(query.data)?query.data:null;
 const providers=Array.isArray(providersQuery.data?.providers)?providersQuery.data.providers:[];
 const choices=providers.filter(provider=>provider.protocol==='openai_chat'&&providerReady(provider));
 const draft=editor?.settings??data?.settings;
 const enhancedAvailable=!!draft?.enhanced_provider_id&&choices.some(provider=>provider.id===draft.enhanced_provider_id);
 const premiumAvailable=!!draft?.premium_provider_id&&choices.some(provider=>provider.id===draft.premium_provider_id);
 const busy=action.isPending;
 function edit(patch:Partial<CompetitionSettings>){
  if(!data)return;
  setError('');setNotice('');
  setEditor(current=>({expected_revision:current?.expected_revision??data.revision,settings:{...(current?.settings??data.settings),...patch}}));
 }
 async function save(payload:Record<string,unknown>,success:string){
  if(busy)return;
  setError('');setNotice('');
  try{
   const result=await action.mutateAsync(payload);
   if(!isSnapshot(result))throw new Error('服务器未返回有效的竞赛状态，请刷新核对是否已保存。');
   await client.cancelQueries({queryKey:['/agents/competition']});
   client.setQueryData(['/agents/competition',{}],result);
   void client.invalidateQueries({queryKey:['/agents/quotas']});
   setEditor(null);setNotice(success);
  }catch(cause){setError(cause instanceof Error?cause.message:'竞赛设置保存失败，请刷新后重试。');}
 }
 function toggle(enabled:boolean){
  if(!data)return;
  void save({expected_revision:data.revision,mode:enabled?(data.settings.enhanced_provider_id?'models':'resources'):'off'},enabled?'竞赛设置已保存，角色运行开关保持原状。':'已关闭竞赛，恢复按需共享调度。');
 }
 function saveEditor(){
  if(!data||!draft)return;
  if(draft.mode==='models'&&!enhancedAvailable){setError('分配资源与模型需要选择一个可用的增强文字模型连接。');return;}
  if(draft.premium_provider_id&&!premiumAvailable){setError('请选择一个可用的顶级文字模型连接，或留空沿用增强连接。');return;}
  if(!Number.isInteger(draft.enhanced_hourly_calls)||draft.enhanced_hourly_calls<0||draft.enhanced_hourly_calls>1000){setError('增强模型每小时调用上限须为 0–1000 的整数。');return;}
  const {mode,enhanced_provider_id,premium_provider_id,enhanced_hourly_calls}=draft;
  void save({expected_revision:editor?.expected_revision??data.revision,mode,enhanced_provider_id,premium_provider_id,enhanced_hourly_calls},'竞赛设置已保存。后续任务使用生效配置，已开始的任务继续原有模型。');
 }
 const rounds=[...(data?.rounds??[])].sort((a,b)=>b.start_at-a.start_at);
 const latestRound=rounds[0],round=rounds.find(item=>item.round_id===selectedRound)??latestRound;
 const currentById=new Map(data?.agents.map(agent=>[agent.agent_id,agent]));
 const results=round?.results??data?.agents??[];
 const rowIds=[...new Set([...results.map(row=>row.agent_id),...(data?.agents??[]).map(row=>row.agent_id),...agents.map(agent=>agent.id)])];
 const rows=rowIds.map(agentId=>({agentId,profile:agents.find(agent=>agent.id===agentId),result:results.find(row=>row.agent_id===agentId),current:currentById.get(agentId)})).sort((a,b)=>(a.result?.rank??Infinity)-(b.result?.rank??Infinity)||a.agentId.localeCompare(b.agentId));
 const allocationsActive=!!data&&['resources','models'].includes(data.settings.mode);
 function providerLabel(providerId:string|undefined|null){const provider=providers.find(item=>item.id===providerId);return provider?`${provider.name} · ${provider.model}`:providerId?'连接不可用或未读取':'沿用角色模型';}
 function baseModel(profile?:TrialAgent){return providerLabel(profile?.provider_id||providersQuery.data?.default_provider_id);}
 function connectionOptions(selected?:string|null,emptyLabel='不指定，保持角色原模型'){
  return [{value:'',label:emptyLabel},...choices.map(provider=>({value:provider.id,label:`[${connectionTiers[(provider as typeof provider&{competition_tier?:string}).competition_tier||'standard']||'标准'}] ${provider.name} · ${provider.model}`})),...(selected&&!choices.some(provider=>provider.id===selected)?[{value:selected,label:'当前连接不可用',disabled:true}]:[])];
 }
 return <section className="agent-competition-trial stack" aria-label="发文竞赛与资源">
  <div className="agents-section-head"><div><h3>发文竞赛与资源</h3><p className="muted">按小时成绩分配自动发文机会；模型奖励需连续两轮领先。</p></div><Space wrap><Button loading={query.isFetching} onClick={()=>void query.refetch()}>刷新竞赛</Button><Switch aria-label="开启发文竞赛" checked={data?.enabled??false} disabled={!data||busy||query.isError} loading={busy} checkedChildren="开启" unCheckedChildren="关闭" onChange={toggle}/></Space></div>
  {notice&&<Alert type="success" showIcon title={notice}/>}
  {error&&<Alert type="error" showIcon title="竞赛设置未确认保存" description={error}/>}
  {query.isPending?<Skeleton active paragraph={{rows:3}}/>:query.isError?<Alert type="error" showIcon title="竞赛状态读取失败" description={query.error.message}/>:!data?<Alert type="warning" showIcon title="竞赛状态暂不可用" description="服务器尚未返回有效的竞赛记录。请刷新核对。"/>:<>
   {!teamEnabled&&<Alert type="info" showIcon title="队伍持续运行已暂停" description="竞赛开关只控制计分与资源策略。队伍和角色的运行开关仍由你分别控制。"/>}
   <div className="agent-competition-facts">
    <div><span>当前竞赛模式</span><strong>{modes[data.settings.mode]}</strong><small>{data.settings.mode==='off'?'计分与奖励已关闭':data.settings.mode==='observe'?'记录成绩，继续按需共享资源':'空闲份额可借用，平台总额度保持不变'}</small></div>
    <div><span>当前发文资源分配</span><strong>{allocationsActive?'按竞赛份额调度':'按需共享'}</strong><small>{data.allocation_epoch?`采用 ${at(data.allocation_epoch)} 开始的小时结果`:'尚无已结算的分配轮次'}</small></div>
    <div><span>最近小时结算</span><strong>{at(data.last_settled_at)}</strong><small>状态截至 {at(data.generated_at)}</small>{data.data_updated_at!=null&&<small>观测更新于 {at(data.data_updated_at)}</small>}</div>
   </div>
   <div className="agents-section-head"><div><h4>{round?'小时成绩与当前资源':'等待首次小时结算'}</h4><p className="muted">成绩为合资格样本的内部关注度分；当前份额与模型配置单独展示。</p></div>{rounds.length>0&&<Select aria-label="选择竞赛小时" value={round?.round_id} style={{minWidth:280}} options={rounds.map(item=>({value:item.round_id,label:`${at(item.start_at)} — ${at(item.end_at)}`}))} onChange={setSelectedRound}/>}</div>
   <Table rowKey="agentId" size="small" dataSource={rows} pagination={false} scroll={{x:1260}} locale={{emptyText:<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚未配置角色"/>}} columns={[
    {title:'角色',key:'agent',fixed:'left',width:130,render:(_,row)=><div className="agent-competition-values"><Link to={`/ai/agents/${encodeURIComponent(row.agentId)}`}>{row.profile?.name||row.current?.name||row.result?.name||'历史角色'}</Link>{row.profile?.account_id&&<Link to={`/analysis?author_id=${row.profile.account_id}&analysis_tab=growth`}>账号窗口增长 ↗</Link>}</div>},
    {title:'小时成绩',key:'score',width:130,render:(_,row)=><div className="agent-competition-values"><strong>{number(row.result?.score)}</strong>{row.result?.rank!=null&&<small>名次 {number(row.result.rank)}</small>}{row.result?.score==null&&<small>待取得完整观测</small>}</div>},
    {title:'合格 / 到期样本',key:'samples',width:140,render:(_,row)=>row.result?<div className="agent-competition-values"><span>{row.result.eligible_posts} / {row.result.due_posts}</span><small>覆盖 {percent(row.result.coverage)}</small><small>排除 {Math.max(0,row.result.due_posts-row.result.eligible_posts)} 篇</small></div>:<span className="muted">—</span>},
    {title:'本轮状态与原因',key:'status',width:230,render:(_,row)=><div className="agent-competition-values"><Tag>{row.result?statuses[row.result.status]||row.result.status:'等待观测'}</Tag>{(row.result?.reasons?.length?row.result.reasons:[row.result&&statusHints[row.result.status],row.profile&&preparation(row.profile,teamEnabled)]).filter(Boolean).map((reason,index)=><small key={index}>{reason}</small>)}</div>},
    {title:'当前分配份额',key:'share',width:130,render:(_,row)=><div className="agent-competition-values"><strong>{allocationsActive?percent(row.current?.share):'按需共享'}</strong>{allocationsActive&&numeric(row.current?.target_share)&&<small>原始目标 {percent(row.current.target_share)}</small>}</div>},
    {title:'当前模型分配',key:'model',width:240,render:(_,row)=><div className="agent-competition-values"><span>{data.settings.mode==='models'?tierNames[row.current?.model_tier??0]||'标准':'角色原模型'}{data.settings.mode==='models'&&numeric(row.current?.enhanced_ratio)?` · 奖励调用 ${percent(row.current.enhanced_ratio)}`:''}</span><small>基础：{baseModel(row.profile)}</small>{data.settings.mode==='models'&&data.settings.enhanced_provider_id&&<small>{row.current?.model_tier===2&&data.settings.premium_provider_id?'顶级':'增强'}：{providerLabel(row.current?.model_tier===2&&data.settings.premium_provider_id?data.settings.premium_provider_id:data.settings.enhanced_provider_id)}</small>}</div>},
    {title:'新人保护',key:'protection',width:180,render:(_,row)=>row.current?.protected_until?<div className="agent-competition-values"><span>{numeric(row.current.protection_remaining_seconds)&&row.current.protection_remaining_seconds>0?`剩余 ${duration(row.current.protection_remaining_seconds-Math.max(0,now-query.dataUpdatedAt)/1000)}`:'保护已结束'}</span><small>截至 {at(row.current.protected_until)}</small></div>:<span className="muted">尚未开始</span>},
   ]}/>
   <p className="agent-competition-caption">份额是发布机会的目标权重，不是强制发帖任务或实际使用率。基础模型沿用角色完整连接；任务开始后固定其模型，配置不会改写人设。</p>
   <Collapse size="small" items={[
    {key:'settings',label:'竞赛模式与模型奖励设置',children:draft&&<div className="agent-competition-editor stack">
     <label>运行模式<Select aria-label="竞赛运行模式" value={draft.mode} disabled={busy} options={Object.entries(modes).map(([value,label])=>({value,label}))} onChange={mode=>edit({mode})}/></label>
     <p className="muted">只观察计分保持共享调度；分配发文资源启用 40% 保底、40% 竞技、20% 探索；分配资源与模型还会按连续两轮的成绩调整奖励比例。</p>
     {providersQuery.isError&&<Alert type="error" showIcon title="文字模型连接读取失败" description={providersQuery.error.message} action={<Button onClick={()=>void providersQuery.refetch()}>重试</Button>}/>}
     <label>增强文字模型连接<Select aria-label="增强文字模型连接" value={draft.enhanced_provider_id||''} disabled={busy||providersQuery.isPending||providersQuery.isError} options={connectionOptions(draft.enhanced_provider_id)} onChange={value=>edit({enhanced_provider_id:value||null})}/></label>
     <label>顶级文字模型连接<Select aria-label="顶级文字模型连接" value={draft.premium_provider_id||''} disabled={busy||providersQuery.isPending||providersQuery.isError} options={connectionOptions(draft.premium_provider_id,'未指定，最高奖励档沿用增强连接')} onChange={value=>edit({premium_provider_id:value||null})}/></label>
     <p className="muted">连接等级由你在 AI 配置中指定，不按模型名字或价格判断质量。标准、奖励、加强档分别以 0%、50%、80% 的比例使用奖励连接；最高档有顶级连接时使用顶级，未配置时沿用增强。预算用完时回基础模型。</p>
     <div className="agent-competition-editor-grid"><label>奖励模型每小时调用上限<InputNumber aria-label="增强模型每小时调用上限" min={0} max={1000} precision={0} value={draft.enhanced_hourly_calls} disabled={busy} onChange={value=>edit({enhanced_hourly_calls:typeof value==='number'?value:NaN})}/></label></div>
     <Space wrap><Button type="primary" loading={busy} disabled={!editor||busy} onClick={saveEditor}>保存竞赛设置</Button><Button disabled={!editor||busy} onClick={()=>{setEditor(null);setError('');}}>放弃修改</Button><Link to="/ai/settings">管理模型连接 ↗</Link></Space>
    </div>},
    {key:'rules',label:'计分口径与调度规则',children:<div className="agent-competition-plan"><p>内部关注度分采用官方互动权重，不等同于可归因的官方实得积分。</p><Space size={[8,8]} wrap>{[['浏览','+1'],['点赞','+5'],['转发','+10'],['回复','+15'],['关注','+20'],['取消关注','−20']].map(([label,value])=><Tag key={label}>{label} {value}</Tag>)}</Space><p>首次自动就绪后保护 1 小时，暂停与重启不重复领取。资源池为 40% 保底、40% 竞技、20% 探索；新人和数据不足者保留基本机会，未知观测不补零。</p>{data.excluded_metrics?.some(metric=>metric==='follows'||metric==='unfollows')&&<p className="muted">关注、取消关注尚无可核实事件，不使用关注净数换算这两项得分。</p>}{data.notes.map((note,index)=><p className="muted" key={index}>{note}</p>)}
     {!!data.daily_views?.length&&<Table rowKey="date" size="small" dataSource={data.daily_views} pagination={false} columns={[{title:'悉尼日期',dataIndex:'date'},{title:'本队已观测新增浏览',dataIndex:'observed_views',render:number},{title:'已纳入内部计分',dataIndex:'credited_views',render:number},{title:'每日浏览上限',dataIndex:'cap',render:number}]}/>}
    </div>},
   ]}/>
  </>}
  <p><Link to="/ai/analysis?ai_view=research">查看内容研究 ↗</Link></p>
 </section>;
}
