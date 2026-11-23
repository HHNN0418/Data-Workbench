import {useState} from 'react';
import {Link} from 'react-router-dom';
import {useQueryClient} from '@tanstack/react-query';
import {Alert,Button,Empty,InputNumber,Modal,Radio,Space,Table,Tag} from 'antd';
import {useAction,useApi} from '../api';
import {QueryState,time} from '../common';
import './agentQuotas.css';

type Bucket={bucket:string;actions?:string[];default_limit:number;server_limit:number|null;server_period_seconds?:number|null;server_remaining?:number|null;configured_cap:number|null;effective_limit:number;remaining:number|null;available:boolean;retry_after:number;observed_at?:string|null;last_attempt_at?:string|null;usage_last_minute:number;pending_actions:number;auto_pending_actions?:number};
type AgentUsage={agent_id:string;name:string;enabled:boolean;mode:string;account_id?:number|null;last_attempt_at?:string|null;usage_last_minute:number;usage_by_bucket?:Record<string,number>;pending_actions:number;auto_pending_actions?:number;review_pending_actions?:number;manual_pending_actions?:number;blocked_pending_actions?:number};
export type QuotaSnapshot={allocation:string;scope:string;usage_window_seconds:number;observed_at?:string|null;policy?:{limits?:Record<string,number>;updated_at?:string|null};buckets:Bucket[];agents:AgentUsage[]};
const labels:Record<string,string>={post:'发帖、回复与转发',like:'点赞',unlike:'取消点赞',follow:'关注',unfollow:'取消关注',delete_post:'删除帖子',feed:'首页动态',read:'资料查询',auth:'账号认证',notification:'获取通知',resolve:'确认通知','read:get_post':'帖子详情','read:get_account':'账号资料','read:me':'本人账号资料','read:my_likes':'核对点赞关系','read:my_reposts':'核对转发关系','read:following':'关注列表','read:account_posts':'账号帖子','read:account_likes':'账号点赞列表','auth:login':'登录账号','auth:register':'注册账号'};
function routeLabel(bucket:string):string{if(labels[bucket])return labels[bucket];if(bucket.startsWith('failed:'))return `${routeLabel(bucket.slice(7))} · 失败请求`;const [base,...detail]=bucket.split(':');return `${labels[base]||'平台请求'}${detail.length?` · ${detail.join(':')}`:''}`;}
function editable(bucket:string){return Object.hasOwn(labels,bucket)&&!bucket.includes(':');}
function number(value:number|null|undefined){return value==null||!Number.isFinite(value)?'未知':value.toLocaleString('zh-CN',{maximumFractionDigits:2});}
function serverPerMinute(row:Bucket){return row.server_limit==null?row.default_limit:row.server_limit*60/(row.server_period_seconds||60);}
function stamp(value:unknown){return typeof value==='number'?new Date(value*1000).toISOString():value;}

export default function AgentQuotas(){
 const query=useApi<QuotaSnapshot>('/agents/quotas',{},5000),action=useAction<QuotaSnapshot>('/agents/quotas'),client=useQueryClient();
 const [editing,setEditing]=useState<Bucket|null>(null),[mode,setMode]=useState<'official'|'custom'>('official'),[cap,setCap]=useState<number|null>(null),[error,setError]=useState(''),[notice,setNotice]=useState('');
 const rows=(query.data?.buckets||[]).filter(row=>!row.bucket.startsWith('llm:')),agents=query.data?.agents||[];
 const current=rows.find(row=>row.bucket===editing?.bucket)||editing;
 function open(row:Bucket){setEditing(row);setMode(row.configured_cap==null?'official':'custom');setCap(row.configured_cap??Math.floor(serverPerMinute(row)));setError('');setNotice('');}
 async function save(row:Bucket,value:number|null){
  if(action.isPending)return;
  setError('');setNotice('');
  if(value!==null&&(!Number.isInteger(value)||value<1)){setError('请输入大于 0 的整数，或选择跟随官方。');return;}
  try{const result=await action.mutateAsync({bucket:row.bucket,cap:value});await client.cancelQueries({queryKey:['/agents/quotas']});client.setQueryData(['/agents/quotas',{}],result);setEditing(null);const effective=result.buckets.find(item=>item.bucket===row.bucket)?.effective_limit;setNotice(value===null?`${routeLabel(row.bucket)}已恢复跟随官方。`:`${routeLabel(row.bucket)}的期望上限已设为每分钟 ${value} 次，当前实际 ${number(effective)} 次/分钟。`);}
  catch(error){setError(error instanceof Error?error.message:'额度设置保存失败，请重试。');}
 }
 return <section className="agent-quotas stack" aria-label="全队额度管理">
  <div className="agents-section-head"><div><h3>赛事平台额度与全队调度</h3><p className="muted">角色按需共用可用额度，由全队统一调度；空闲额度可供其他角色使用。</p></div><Button loading={query.isFetching} onClick={()=>void query.refetch()}>刷新额度</Button></div>
  {notice&&<Alert type="success" showIcon title={notice}/>}{error&&!editing&&<Alert type="error" showIcon title={error}/>}
  <QueryState query={query}>{data=><>
   <div className="agent-quotas-summary"><div><span>平台额度池</span><strong>全队共享</strong><small>竞赛调整使用机会，不增加平台总额度</small></div><div><span>自定义类别</span><strong>{rows.filter(row=>editable(row.bucket)&&row.configured_cap!=null).length}</strong><small>其余类别跟随官方</small></div><div><span>自动待执行</span><strong>{agents.reduce((total,agent)=>total+(agent.auto_pending_actions||0),0)}</strong><small>按当前运行方式与账号状态统计</small></div></div>
   <div className="agent-quotas-panel"><div className="agents-section-head"><div><h3>路由额度</h3><p className="muted">默认跟随官方。可设置全队期望上限，实际始终服从官方额度与冷却，并随服务器调整自动适配。</p></div><span className="muted">更新于 {time(stamp(data.observed_at))}</span></div>
    <Table<Bucket> rowKey="bucket" size="small" dataSource={rows} pagination={false} scroll={{x:1260}} locale={{emptyText:<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无平台额度资料"/>}} columns={[
     {title:'请求类别 / 路由',key:'route',width:200,fixed:'left',render:(_,row)=><div className="agent-quotas-route"><strong>{routeLabel(row.bucket)}</strong>{row.bucket==='post'?<small>发帖、回复、转发合计共用</small>:row.bucket==='auth'||row.bucket.startsWith('auth:')?<small>登录、注册各路由按 IP 限流</small>:row.bucket==='read'?<small>分别应用到每条独立读取路由</small>:!editable(row.bucket)?<small>独立路由 · 继承类别设置</small>:<small>全队共享</small>}</div>},
     {title:'官方默认',dataIndex:'default_limit',width:105,render:value=>`${number(value)} / 分钟`},
     {title:'服务器回执',key:'server',width:140,render:(_,row)=>row.server_limit==null?<span className="muted">尚未返回</span>:<div className="agent-quotas-values"><span>{number(row.server_limit)} / {row.server_period_seconds&&row.server_period_seconds!==60?`${number(row.server_period_seconds)} 秒`:'分钟'}</span><small>上次剩余 {number(row.server_remaining)}</small><small>{time(stamp(row.observed_at))}</small></div>},
     {title:'自定义期望上限',dataIndex:'configured_cap',width:120,render:value=>value==null?<Tag>跟随官方</Tag>:`${number(value)} / 分钟`},
     {title:'有效上限',dataIndex:'effective_limit',width:110,render:value=>`${number(value)} / 分钟`},
     {title:'本地可用估算',key:'available',width:125,render:(_,row)=><div className="agent-quotas-values"><strong>{number(row.remaining)}</strong><small>{row.retry_after>0?`约 ${Math.ceil(row.retry_after)} 秒后可重试`:row.available?'可调度':'等待额度'}</small></div>},
     {title:`本程序近 ${data.usage_window_seconds||60} 秒请求尝试`,dataIndex:'usage_last_minute',width:145,render:value=>number(value)},
     {title:'自动待执行',dataIndex:'auto_pending_actions',width:100,render:value=>number(value??0)},
     {title:'设置',key:'settings',width:155,render:(_,row)=>editable(row.bucket)?<Space orientation="vertical" size={2}><Button size="small" aria-label={`设置${routeLabel(row.bucket)}上限`} disabled={action.isPending} onClick={()=>open(row)}>调整上限</Button>{row.configured_cap!=null&&<Button type="link" size="small" aria-label={`恢复${routeLabel(row.bucket)}为官方`} disabled={action.isPending} onClick={()=>void save(row,null)}>恢复官方</Button>}</Space>:<span className="muted">继承类别设置</span>}
    ]}/>
    <p className="agent-quotas-note">可用额度按服务器回执和本地续补估算；服务器剩余值是上次回执快照。请求尝试包含失败与结果未确认的请求，仅统计本程序，其他客户端使用情况未知。登录、注册各路由按 IP 限流；读取路由各自独立。保存配置保留已有消耗与等待时间。</p>
   </div>
   <div className="agent-quotas-panel"><div className="agents-section-head"><div><h3>各角色需求与使用</h3><p className="muted">自动待执行、待审阅、手动待执行与暂停中的操作分别展示；未使用的额度持续留在共享池中。</p></div></div>
    <Table<AgentUsage> rowKey="agent_id" size="small" dataSource={agents} pagination={false} scroll={{x:840}} columns={[
     {title:'角色',key:'agent',render:(_,agent)=><div className="agent-quotas-route"><Link to={`/ai/agents/${encodeURIComponent(agent.agent_id)}`}>{agent.name}</Link><small>{agent.mode==='auto'?'自动执行':'审阅后执行'} · {agent.enabled?'已启用':'已暂停'}</small></div>},
     {title:'自动待执行',dataIndex:'auto_pending_actions',render:value=>number(value??0)},
     {title:'待审阅',dataIndex:'review_pending_actions',render:value=>number(value??0)},
     {title:'手动待执行',dataIndex:'manual_pending_actions',render:value=>number(value??0)},
     {title:'暂停 / 待核对等',dataIndex:'blocked_pending_actions',render:value=>number(value??0)},
     {title:`近 ${data.usage_window_seconds||60} 秒请求尝试`,dataIndex:'usage_last_minute',render:value=>number(value)},
     {title:'最近请求',dataIndex:'last_attempt_at',render:value=>time(stamp(value))}
    ]}/>
   </div>
  </>}</QueryState>
  <Modal title={editing?`调整${routeLabel(editing.bucket)}额度`:'调整额度'} open={!!editing} onCancel={()=>{if(!action.isPending)setEditing(null);}} onOk={()=>{if(current)void save(current,mode==='official'?null:cap??NaN);}} okText="保存设置" cancelText="取消" confirmLoading={action.isPending} cancelButtonProps={{disabled:action.isPending}} closable={!action.isPending} mask={{closable:!action.isPending}} destroyOnHidden>
   {current&&<div className="agent-quotas-editor"><p>此设置由全队所有角色共享，当前官方额度为 {number(serverPerMinute(current))} 次/分钟。</p><Radio.Group aria-label="额度管理方式" value={mode} disabled={action.isPending} onChange={event=>{setMode(event.target.value);setError('');}} options={[{value:'official',label:'跟随官方'},{value:'custom',label:'自定义全队期望上限'}]}/>{mode==='custom'&&<label className="agent-quotas-cap">全队期望每分钟最多请求次数<InputNumber aria-label="全队期望每分钟最多请求次数" min={1} precision={0} value={cap} disabled={action.isPending} onChange={value=>{setCap(typeof value==='number'?value:null);setError('');}}/></label>}<p className="muted">超出当前官方时，实际仍以官方为准；服务器调整后自动适配。保存保留当前消耗和冷却。</p>{mode==='custom'&&cap!=null&&cap>0&&<p>当前实际有效上限：{number(Math.min(cap,serverPerMinute(current)))} 次/分钟</p>}{error&&<Alert type="error" showIcon title={error}/>}</div>}
  </Modal>
 </section>;
}
