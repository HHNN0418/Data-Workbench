import {Link} from 'react-router-dom';
import {Button,Space,Table,Tag} from 'antd';
import {useApi} from '../api';
import {Panel,QueryState,time} from '../common';

const endpoint='/agents/reading-selection';
type Selection={status?:string;candidate_count?:number;selected_count?:number;cache_hit?:boolean;cache_mode?:string;reason?:unknown;error?:unknown;settings_revision?:number|null};
type Context={included_count?:number;background_count?:number;missing_count?:number;deferred_count?:number};
const hasReadingContext=(context:Context|null|undefined)=>!!context&&[context.included_count,context.background_count,context.missing_count,context.deferred_count].some(value=>typeof value==='number');
type Role={id:string;name:string;enabled:boolean;mode:string;last_selection_at?:string|null;selection?:Selection|null;source_count?:number|null;context?:Context|null};
type Call=Selection&{id:string;at:string;agent_id:string;agent_name:string;provider_id?:string;provider_name?:string;model?:string};
export type ReadingSelectionState={settings:{provider_id:string;revision:number;updated_at?:string};provider:{id:string;name:string;model:string;protocol:string;base_url?:string}|null;provider_error?:string|null;limits:{max_candidates:number;max_selected:number;cache_seconds:number};roles:Role[];recent_calls:Call[]};
const statusNames:Record<string,string>={selected:'已选中资料',empty:'本轮未选中',fallback:'筛选失败，已回退',deferred:'调用暂缓',error:'调用失败',cancelled:'已取消'};
const safeText=(value:unknown)=>typeof value==='string'?value:'';
const selectedCountLabel=(selection:Selection)=>selection.status==='fallback'?'回退选用':'筛选选中';
function SelectionTags({selection,revision}:{selection:Selection;revision:number}){return <Space wrap size={4}><Tag color={selection.status==='fallback'||selection.status==='error'?'orange':selection.status==='selected'?'green':'default'}>{statusNames[selection.status||'']||'尚无筛选结果'}</Tag>{selection.cache_hit&&<Tag>{selection.cache_mode==='handoff'?'沿用待决策资料':'复用筛选缓存'}</Tag>}{selection.settings_revision==null?<Tag>历史记录 · 未记录筛选配置</Tag>:selection.settings_revision!==revision&&<Tag>旧配置 · v{selection.settings_revision}</Tag>}</Space>;}
function Explanation({selection}:{selection:Selection}){const reason=safeText(selection.reason),error=safeText(selection.error);return <>{reason&&<div>{reason}</div>}{error&&error!==reason&&<div className="muted">{error}</div>}</>;}

export default function ReadingSelection(){
 const query=useApi<ReadingSelectionState>(endpoint);
 return <div className="stack"><div className="panel-head"><div><h2>资料筛选</h2><p className="muted">所有角色按规则选读，不调用模型：先看通知，再看回复机会队列，再补充其他资料。</p></div><Space wrap><Link to="/ai/agents">管理 Agent ↗</Link><Button loading={query.isFetching} onClick={()=>void query.refetch()}>刷新记录</Button></Space></div>
  <Panel title="阅读流程"><p>规则从已采集的目录里挑出通知、回复机会和其他资料；再读取原文，并补充可获得的父帖、根帖背景，交给角色自己的 AI 决策与写作。</p><p className="muted">按模型选读的方式已经移除（D67），这一步没有可调的设置，也不调用任何模型。</p></Panel>
  <QueryState query={query}>{state=><>
   <Panel title="各角色最近一次阅读"><Table<Role> rowKey="id" size="small" scroll={{x:950}} dataSource={state.roles||[]} pagination={false} locale={{emptyText:'尚无角色，先在 Agent 中创建角色。'}} columns={[
    {title:'角色',key:'role',width:150,render:(_,role)=><><Link to={`/ai/agents/${role.id}`}>{role.name}</Link><div className="muted">{role.enabled?'已启用':'已暂停'} · {role.mode==='auto'?'自动执行':'审阅模式'}</div></>},
    {title:'最近筛选',key:'at',width:165,render:(_,role)=>time(role.last_selection_at)},
    {title:'筛选结果',key:'selection',width:230,render:(_,role)=>role.selection?<SelectionTags selection={role.selection} revision={state.settings.revision}/>:<span className="muted">尚无筛选记录</span>},
    {title:'资料数量',key:'counts',width:180,render:(_,role)=><>{role.selection&&<><div>候选目录 {role.selection.candidate_count??'—'} 条</div><div>{selectedCountLabel(role.selection)} {role.selection.selected_count??'—'} 条</div></>}{role.source_count!=null&&<div>深读来源 {role.source_count} 条</div>}</>},
    {title:'阅读说明',key:'reason',render:(_,role)=><>{role.selection&&<Explanation selection={role.selection}/>} {hasReadingContext(role.context)?<div className="muted">纳入目标 {role.context?.included_count??'—'} 条 · 父帖或根帖背景 {role.context?.background_count??'—'} 条 · 缺失上下文 {role.context?.missing_count??'—'} 处 · 延后目标 {role.context?.deferred_count??'—'} 条</div>:<div className="muted">尚无本轮深读记录</div>}</>},
   ]}/></Panel>
   <Panel title="最近筛选记录"><p className="muted">每轮规则选读留下一条记录，可以看到候选和选中的条数。</p><Table<Call> rowKey="id" size="small" scroll={{x:1100}} dataSource={state.recent_calls||[]} pagination={{pageSize:10,showSizeChanger:false}} locale={{emptyText:'尚无筛选记录。角色开始阅读后会在这里留下记录。'}} columns={[
    {title:'时间',dataIndex:'at',width:165,render:value=>time(value)},
    {title:'角色',key:'role',width:140,render:(_,call)=><Link to={`/ai/agents/${call.agent_id}`}>{call.agent_name||call.agent_id}</Link>},
    {title:'调用结果',key:'status',width:230,render:(_,call)=><SelectionTags selection={call} revision={state.settings.revision}/>},
    {title:'候选与选中',key:'counts',width:150,render:(_,call)=><>候选目录 {call.candidate_count??'—'} 条<br/>{selectedCountLabel(call)} {call.selected_count??'—'} 条</>},
    {title:'说明',key:'reason',render:(_,call)=><Explanation selection={call}/>},
   ]}/></Panel>
  </>}</QueryState>
 </div>;
}
