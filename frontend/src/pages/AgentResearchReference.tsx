import {useState} from 'react';
import {Link} from 'react-router-dom';
import {Alert, Button, Collapse, Empty, Skeleton, Space, Tag} from 'antd';
import {useApi} from '../api';
import {time} from '../common';
import {referenceFailureDetails} from './researchReferenceFailure';
import './agentCompetitionTrial.css';

type Brief = {
 brief_id:string;generated_at:string;expires_at:string|null;instruction:string;report:string;
 summary?:unknown;model?:string;provider_id?:string;sources:unknown[];scope?:unknown;
 active:boolean;expired:boolean;can_activate:boolean;citation_status:'valid'|'invalid';
};
type Reference = {
 agent_id:string;brief_id:string;provided_at:string;status:'provided';call_outcome?:'succeeded'|'failed';draft_id?:string;
 error?:string;error_code?:string;diagnostics?:Record<string,unknown>;
};
type Research = {kind:'content_research';revision:number;active_brief_id:string|null;briefs:Brief[];references:Reference[]};
function record(value:unknown):value is Record<string,unknown>{return !!value&&typeof value==='object'&&!Array.isArray(value);}
function stamp(value:unknown):value is string{return typeof value==='string'&&Number.isFinite(Date.parse(value));}
export function isResearch(value:unknown):value is Research {
 if(!record(value)||value.kind!=='content_research'||!Number.isInteger(value.revision)||typeof value.revision!=='number'||value.revision<0
  ||!(value.active_brief_id===null||typeof value.active_brief_id==='string')||!Array.isArray(value.briefs)||!Array.isArray(value.references))return false;
 const validBriefs=value.briefs.every(brief=>record(brief)&&typeof brief.brief_id==='string'&&!!brief.brief_id&&stamp(brief.generated_at)
  &&(brief.expires_at===null||stamp(brief.expires_at))&&typeof brief.instruction==='string'&&typeof brief.report==='string'&&Array.isArray(brief.sources)
  &&typeof brief.active==='boolean'&&typeof brief.expired==='boolean'&&typeof brief.can_activate==='boolean'&&['valid','invalid'].includes(String(brief.citation_status)));
 const validReferences=value.references.every(reference=>record(reference)&&typeof reference.agent_id==='string'&&typeof reference.brief_id==='string'
  &&stamp(reference.provided_at)&&reference.status==='provided'&&(reference.call_outcome===undefined||['succeeded','failed'].includes(String(reference.call_outcome)))
  &&(reference.draft_id===undefined||typeof reference.draft_id==='string')
  &&(reference.error===undefined||typeof reference.error==='string')
  &&(reference.error_code===undefined||typeof reference.error_code==='string')
  &&(reference.diagnostics===undefined||record(reference.diagnostics)));
 return validBriefs&&validReferences;
}

function ReferenceDetails({agentId}:{agentId:string}){
 const query=useApi<unknown>('/ai/research',{},30000);
 if(query.isPending)return <Skeleton active paragraph={{rows:3}}/>;
 if(query.isError)return <Alert type="error" showIcon title="研究参考读取失败" description={query.error.message} action={<Button onClick={()=>void query.refetch()}>重试</Button>}/>;
 if(!isResearch(query.data))return <Alert type="warning" showIcon title="研究参考数据暂不可用" description="返回内容缺少有效的研究记录，请刷新后重试。" action={<Button onClick={()=>void query.refetch()}>重新读取</Button>}/>;
 const data=query.data,active=data.briefs.find(brief=>brief.brief_id===data.active_brief_id);
 const references=data.references.filter(reference=>reference.agent_id===agentId).sort((a,b)=>Date.parse(b.provided_at)-Date.parse(a.provided_at));
 const available=!!active&&active.active&&active.can_activate&&!active.expired&&active.citation_status==='valid'&&(!active.expires_at||Date.parse(active.expires_at)>Date.now());
 return <div className="agent-research-reference-body stack">
  <div className="agents-section-head"><span className="muted">截至 {time(new Date(query.dataUpdatedAt).toISOString())} · 版本 {data.revision}</span><Button size="small" loading={query.isFetching} onClick={()=>void query.refetch()}>刷新参考</Button></div>
  <section><h4>当前启用的研究参考</h4>{available&&active?<article className="agent-research-brief">
   <Space wrap><Tag color="green">可供决策参考</Tag><span>{time(active.generated_at)} 的研究参考</span>{active.model&&<Tag>{active.model}</Tag>}</Space>
   {typeof active.summary==='string'&&active.summary&&<p className="agents-prose">{active.summary}</p>}
   {active.report&&<p className="agents-prose">{active.report.slice(0,1200)}{active.report.length>1200?'…':''}</p>}
   <p className="muted">生成于 {time(active.generated_at)} · {active.expires_at?`有效期至 ${time(active.expires_at)}`:'未设置到期时间'} · {active.sources.length} 条来源</p>
   <details><summary>参考编号</summary><code>{active.brief_id}</code></details>
  </article>:data.active_brief_id?<Alert type="info" showIcon title="当前启用记录暂不可作为有效参考" description={active?.expired||active?.expires_at&&Date.parse(active.expires_at)<=Date.now()?'这份参考已过期，请到内容研究查看。':'启用记录缺失、来源不可用或引用未通过校验，请到内容研究核对。'}/>:<p className="muted">尚未启用研究参考。</p>}</section>
  <section><h4>已向这个角色提供的参考</h4><p className="muted">“已提供参考”表示已放入该次模型请求；是否采纳、是否改善表现仍需验证。</p>
   {!references.length?<Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="尚无向这个角色提供研究参考的记录"/>:<div className="agent-research-reference-list">{references.map((reference,index)=>{
    const brief=data.briefs.find(item=>item.brief_id===reference.brief_id);
    const failure=reference.call_outcome==='failed'?referenceFailureDetails(reference):null;
    return <article key={`${reference.brief_id}-${reference.provided_at}-${index}`}>
    <Space wrap><Tag>已提供参考</Tag><span>{brief?`${time(brief.generated_at)} 的研究参考`:'较早的研究参考'}</span>{brief?.model&&<Tag>{brief.model}</Tag>}{reference.call_outcome&&<Tag color={reference.call_outcome==='failed'?'orange':'default'}>{reference.call_outcome==='succeeded'?'模型调用成功':'模型调用失败'}</Tag>}</Space>
    {failure&&<p className="agents-prose">{reference.error_code==='output_truncated'&&<Tag color="orange">输出被截断</Tag>}{reference.error_code==='reasoning_only'&&<Tag color="orange">只返回推理、没有正文</Tag>}{failure.message}{failure.notice&&<><br/><span className="muted">{failure.notice}</span></>}</p>}
    <p className="muted">提供于 {time(reference.provided_at)}</p>
    {reference.draft_id&&<p><Link to={`/ai/agents/${encodeURIComponent(agentId)}?tab=drafts&draft=${encodeURIComponent(reference.draft_id)}`}>查看关联操作 ↗</Link></p>}
    <details><summary>参考编号</summary><code>{reference.brief_id}</code></details>
   </article>;})}</div>}
  </section>
 </div>;
}

export default function AgentResearchReference({agentId}:{agentId:string}){
 const [open,setOpen]=useState(false);
 return <section className="agent-research-reference" aria-label="角色研究参考">
  <Collapse activeKey={open?['research']:[]} onChange={keys=>setOpen(keys.includes('research'))} items={[{key:'research',label:'研究参考与提供记录',children:open?<div className="stack"><p><Link to="/ai/analysis?ai_view=research">查看与维护内容研究 ↗</Link></p><ReferenceDetails key={agentId} agentId={agentId}/></div>:null}]}/>
 </section>;
}
